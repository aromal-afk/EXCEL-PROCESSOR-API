
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.api_core.exceptions import GoogleAPICallError
from app.routes.jobs import router as jobs_router
from app.services.gcp import (
    storage_client,
    firestore_client,
    BUCKET_NAME
)
from app.services.error_report import (
    get_error_report_download_url,
)


app = FastAPI(
    title="Bulk Excel Processor API",
    description="Asynchronous Excel Upload Processing",
    version="1.0.0"
)

app.include_router(jobs_router)

# Allow React frontend to communicate with FastAPI
# CORS_ORIGINS is a comma-separated list of allowed origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv(
        "CORS_ORIGINS", "http://localhost:5173"
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {
        "message": "Bulk Excel Processor API is running"
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "bulk-excel-api"
    }

@app.get("/test-gcp")
def test_gcp_connection():
    try:
        # Verify Cloud Storage connection
        bucket = storage_client.get_bucket(BUCKET_NAME)

        # Verify Firestore connection
        documents = (
            firestore_client
            .collection("jobs")
            .limit(1)
            .get()
        )

        return {
            "storage": "connected",
            "bucket": bucket.name,
            "firestore": "connected",
            "jobs_found": len(documents)
        }

    except GoogleAPICallError as error:
        raise HTTPException(
            status_code=503,
            detail=f"GCP connection failed: {type(error).__name__}"
        )

@app.get("/jobs/{job_id}/error-report")
def download_error_report(job_id: str):
    try:
        signed_url, error = (
            get_error_report_download_url(job_id)
        )

        if error == "job_not_found":
            raise HTTPException(
                status_code=404,
                detail="Job not found",
            )

        if error == "report_not_available":
            raise HTTPException(
                status_code=404,
                detail="This job has no error report",
            )

        if error == "report_not_found":
            raise HTTPException(
                status_code=404,
                detail="Error report file not found",
            )

        return {
            "job_id": job_id,
            "download_url": signed_url,
            "expires_in_seconds": 900,
        }

    except HTTPException:
        raise

    except Exception as exc:
        print("Error report download failed:", exc)

        raise HTTPException(
            status_code=500,
            detail="Unable to generate download URL",
        )
