
import os
from datetime import timedelta

import google.auth
from google.auth import iam
from google.auth.credentials import Signing
from google.auth.transport.requests import Request
from google.cloud import storage
from dotenv import load_dotenv

load_dotenv()

BUCKET_NAME = os.environ["GCS_BUCKET_NAME"]
SIGNER_EMAIL = os.environ["GCP_SIGNER_SERVICE_ACCOUNT"]

CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet"
)



class IAMSigningCredentials(Signing):
    def __init__(self, credentials):
        self._credentials = credentials
        self._signer = iam.Signer(
            Request(),
            credentials,
            SIGNER_EMAIL
        )

    @property
    def signer(self):
        return self._signer

    @property
    def signer_email(self):
        return SIGNER_EMAIL

    @property
    def universe_domain(self):
        return "googleapis.com"

    def sign_bytes(self, message: bytes) -> bytes:
        return self._signer.sign(message)



def generate_upload_url(object_name: str) -> str:
    credentials, project_id = google.auth.default(
        scopes=[
            "https://www.googleapis.com/auth/cloud-platform"
        ]
    )

    if not credentials.valid:
        credentials.refresh(Request())

    signing_credentials = IAMSigningCredentials(credentials)

    storage_client = storage.Client(
        project=project_id,
        credentials=credentials
    )

    blob = storage_client.bucket(
        BUCKET_NAME
    ).blob(object_name)

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=15),
        method="PUT",
        content_type=CONTENT_TYPE,
        credentials=signing_credentials,
        query_parameters={
        "ifGenerationMatch": "0"
        },
    )
