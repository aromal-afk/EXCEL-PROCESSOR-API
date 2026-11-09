
import os
from datetime import timedelta

import google.auth
from google.auth.transport.requests import Request
from google.cloud import firestore, storage
from google.oauth2 import service_account

PROJECT_ID = os.getenv(
    "GCP_PROJECT_ID", "bulk-excel-processor"
)
BUCKET_NAME = os.getenv(
    "BUCKET_NAME", "bulk-excel-processor"
)
DATABASE_ID = os.getenv(
    "FIRESTORE_DATABASE", "(default)"
)

db = firestore.Client(
    project=PROJECT_ID,
    database=DATABASE_ID,
)
storage_client = storage.Client(project=PROJECT_ID)


def get_error_report_download_url(job_id: str):
    job_ref = db.collection("jobs").document(job_id)
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
    signer_email = os.getenv(
        "SIGNER_SERVICE_ACCOUNT",
        "excel-upload-signer@bulk-excel-processor.iam.gserviceaccount.com",
    )

    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=15),
        method="GET",
        service_account_email=signer_email,
        access_token=credentials.token,
        response_disposition='attachment; filename="errors.csv"',
    )

    return signed_url, None
