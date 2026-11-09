\
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from google.cloud import firestore

from app.services.gcp import (
    storage_client,
    firestore_client,
    BUCKET_NAME,
)
from app.services.signed_urls import (
    generate_upload_url,
    CONTENT_TYPE,
)
from app.services.pubsub import publish_processing_job


router = APIRouter(prefix="/jobs", tags=["Jobs"])

TERMINAL_STATUSES = ("Completed", "CompletedWithErrors")


class CreateJobRequest(BaseModel):
    filename: str = Field(min_length=1)


# --------------------------------------------------
# Create a new Excel upload job
# --------------------------------------------------

@router.post("", status_code=201)
def create_job(request: CreateJobRequest):
    filename = request.filename

    if Path(filename).suffix.lower() != ".xlsx":
        raise HTTPException(
            status_code=400,
            detail="Only .xlsx files are supported",
        )

    job_id = str(uuid4())
    object_name = f"uploads/{job_id}/input.xlsx"

    job_data = {
        "job_id": job_id,
        "filename": filename,
        "object_name": object_name,
        "status": "AwaitingUpload",
        "created_at": datetime.now(timezone.utc),
    }

    upload_url = generate_upload_url(object_name)

    firestore_client.collection("jobs").document(job_id).set(
        job_data
    )

    return {
        "job_id": job_id,
        "filename": filename,
        "status": "AwaitingUpload",
        "object_name": object_name,
        "upload_url": upload_url,
        "upload_method": "PUT",
        "upload_content_type": CONTENT_TYPE,
    }


# --------------------------------------------------
# Get complete job details
# --------------------------------------------------


# --------------------------------------------------
# Get recent jobs for the React dashboard
# --------------------------------------------------

@router.get("")
def list_jobs(limit: int = 50):
    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=400,
            detail="Limit must be between 1 and 100",
        )

    snapshots = (
        firestore_client
        .collection("jobs")
        .order_by(
            "created_at",
            direction=firestore.Query.DESCENDING,
        )
        .limit(limit)
        .stream()
    )

    jobs = []

    for snapshot in snapshots:
        job = snapshot.to_dict()
        job["job_id"] = snapshot.id
        jobs.append(job)

    return {"jobs": jobs}


@router.get("/{job_id}")
def get_job(job_id: str):
    snapshot = (
        firestore_client.collection("jobs")
        .document(job_id)
        .get()
    )

    if not snapshot.exists:
        raise HTTPException(
            status_code=404,
            detail="Job not found",
        )

    return snapshot.to_dict()


# --------------------------------------------------
# Get current job status
# --------------------------------------------------

@router.get("/{job_id}/status")
def get_job_status(job_id: str):
    snapshot = (
        firestore_client.collection("jobs")
        .document(job_id)
        .get()
    )

    if not snapshot.exists:
        raise HTTPException(
            status_code=404,
            detail="Job not found",
        )

    return snapshot.to_dict()["status"]


# --------------------------------------------------
# Transaction: claim job for Pub/Sub queueing
# --------------------------------------------------

@firestore.transactional
def _claim_for_queueing(transaction, job_ref):
    snapshot = job_ref.get(transaction=transaction)

    if not snapshot.exists:
        return "NotFound", None

    job = snapshot.to_dict()
    current_status = job.get("status")

    if current_status in (
        "Queued",
        "Processing",
        *TERMINAL_STATUSES,
    ):
        return current_status, job

    if current_status == "Queueing":
        return "Queueing", job

    if current_status not in (
        "AwaitingUpload",
        "Uploaded",
    ):
        return "Invalid", job

    transaction.update(
        job_ref,
        {
            "status": "Queueing",
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
    )

    return "Claimed", job


# --------------------------------------------------
# Transaction: safely mark job as Queued
# --------------------------------------------------

@firestore.transactional
def _mark_job_queued(
    transaction,
    job_ref,
    message_id,
    file_size,
):
    snapshot = job_ref.get(transaction=transaction)

    if not snapshot.exists:
        return False

    # Prevent overwriting Processing or Completed.
    if snapshot.get("status") != "Queueing":
        return False

    transaction.update(
        job_ref,
        {
            "status": "Queued",
            "uploaded_at": firestore.SERVER_TIMESTAMP,
            "file_size": file_size,
            "pubsub_message_id": message_id,
            "queued_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
    )

    return True


def mark_job_queued(job_ref, message_id, file_size):
    transaction = firestore_client.transaction()

    return _mark_job_queued(
        transaction,
        job_ref,
        message_id,
        file_size,
    )


# --------------------------------------------------
# Confirm upload and publish job to Pub/Sub
# --------------------------------------------------

@router.post("/{job_id}/confirm")
def confirm_upload(job_id: str):
    job_ref = (
        firestore_client.collection("jobs")
        .document(job_id)
    )

    snapshot = job_ref.get()

    if not snapshot.exists:
        raise HTTPException(
            status_code=404,
            detail="Job not found",
        )

    job = snapshot.to_dict()
    current_status = job.get("status")

    # Already queued, processing, or completed.
    if current_status in (
        "Queued",
        "Processing",
        *TERMINAL_STATUSES,
    ):
        return {
            "job_id": job_id,
            "status": current_status,
        }

    # Another request has already claimed this job.
    if current_status == "Queueing":
        return {
            "job_id": job_id,
            "status": "Queueing",
        }

    if current_status not in (
        "AwaitingUpload",
        "Uploaded",
    ):
        raise HTTPException(
            status_code=409,
            detail="Job is not eligible for queueing",
        )

    # Verify that the Excel file exists in GCS.
    blob = storage_client.bucket(BUCKET_NAME).blob(
        job["object_name"]
    )

    if not blob.exists():
        raise HTTPException(
            status_code=400,
            detail="Excel file has not been uploaded to GCS",
        )

    # Atomically claim the job for queueing.
    transaction = firestore_client.transaction()

    claim_status, claimed_job = _claim_for_queueing(
        transaction,
        job_ref,
    )

    if claim_status == "NotFound":
        raise HTTPException(
            status_code=404,
            detail="Job not found",
        )

    if claim_status == "Invalid":
        raise HTTPException(
            status_code=409,
            detail="Job is not eligible for queueing",
        )

    if claim_status != "Claimed":
        return {
            "job_id": job_id,
            "status": claim_status,
        }

    # Publish the processing job to Pub/Sub.
    try:
        message_id = publish_processing_job(
            job_id,
            claimed_job["object_name"],
        )

    except Exception as exc:
        # Publication may have succeeded even if its
        # acknowledgement was lost. Do not blindly
        # reset the job to Uploaded.
        raise HTTPException(
            status_code=503,
            detail=(
                "Queue publication could not be confirmed. "
                "Job requires reconciliation."
            ),
        ) from exc

    # Update status only if it is still Queueing.
    queued = mark_job_queued(
        job_ref=job_ref,
        message_id=message_id,
        file_size=blob.size,
    )

    # The worker may already have started processing.
    if queued:
        final_status = "Queued"
    else:
        latest_snapshot = job_ref.get()

        if not latest_snapshot.exists:
            raise HTTPException(
                status_code=404,
                detail="Job not found",
            )

        final_status = latest_snapshot.get("status")

    return {
        "job_id": job_id,
        "status": final_status,
        "message": "Excel processing job submitted successfully",
    }
