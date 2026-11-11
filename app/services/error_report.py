
import os
from datetime import timedelta

import google.auth
from google.auth.transport.requests import Request

from app.services.gcp import (
    storage_client,
    firestore_client,
    BUCKET_NAME,
)

SIGNER_EMAIL = os.environ["GCP_SIGNER_SERVICE_ACCOUNT"]


def get_error_report_download_url(job_id: str):
    job_ref = firestore_client.collection("jobs").document(job_id)
    snapshot = job_ref.get()

    if not snapshot.exists:
        return None, "job_not_found"

    job = snapshot.to_dict()
    object_name = job.get("error_report_object_name")

    if not object_name:
        return None, "report_not_available"

    # Only allow the report belonging to this job.
    expected_name = f"reports/{job_id}/errors.csv"

    if object_name != expected_name:
        raise ValueError("Unexpected report object path")

    blob = storage_client.bucket(BUCKET_NAME).blob(
        object_name
    )

    if not blob.exists():
        return None, "report_not_found"

    credentials, _ = google.auth.default(
        scopes=[
            "https://www.googleapis.com/auth/cloud-platform"
        ]
    )

    credentials.refresh(Request())

    # Locally, ADC user credentials generally cannot
    # sign URLs directly. IAM signBlob can sign through
    # the dedicated service account.
    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=15),
        method="GET",
        service_account_email=SIGNER_EMAIL,
        access_token=credentials.token,
        response_disposition='attachment; filename="errors.csv"',
    )

    return signed_url, None
