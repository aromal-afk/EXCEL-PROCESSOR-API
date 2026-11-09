
import os
from dotenv import load_dotenv
from google.cloud import storage, firestore

# Load environment variables from .env
load_dotenv()

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
BUCKET_NAME = os.environ["GCS_BUCKET_NAME"]

# Initialize Google Cloud Storage client
storage_client = storage.Client(project=PROJECT_ID)

# Initialize Firestore client
firestore_client = firestore.Client(
    project=PROJECT_ID,
    database=os.getenv("FIRESTORE_DATABASE", "(default)")
)
