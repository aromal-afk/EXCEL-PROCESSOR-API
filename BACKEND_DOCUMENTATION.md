# Backend Architecture, File Structure, and Function Reference

## 1. Overview

The `backend/` directory contains a **FastAPI** application (`Bulk Excel Processor API`) responsible for orchestrating asynchronous Excel (`.xlsx`) file uploads and queuing them for background processing using **Google Cloud Platform (GCP)** services:

1. **Google Cloud Storage (GCS):** Stores uploaded `.xlsx` files directly from the client using V4 Signed URLs (bypassing backend payload limits).
2. **Google Cloud Firestore:** Tracks job metadata and state transitions (`AwaitingUpload` -> `Queueing` -> `Queued` -> `Processing` -> `Completed`).
3. **Google Cloud Pub/Sub:** Dispatches asynchronous processing jobs (`job_id` and `object_name`) to downstream worker services.

---

## 2. Directory Structure

```text
backend/
├── .env                        # Environment variables (GCP project, bucket, Firestore, IAM, Pub/Sub)
├── requirements.txt            # Python package dependencies
├── BACKEND_DOCUMENTATION.md    # Technical documentation of the backend
└── app/
    ├── main.py                 # FastAPI app initialization, CORS middleware, health & GCP test routes
    ├── routes/
    │   └── jobs.py             # "/jobs" API endpoints (create, get, status, confirm upload)
    └── services/
        ├── gcp.py              # Shared GCP Storage and Firestore client singletons
        ├── signed_urls.py      # IAM-based GCS V4 Signed URL generation for direct uploads
        └── pubsub.py           # GCP Pub/Sub client and job publishing logic
``` 

---

## 3. Configuration & Environment Files

### 3.1 `backend/.env`
Defines the runtime environment variables loaded via `python-dotenv`:
- `GCP_PROJECT_ID=bulk-excel-processor`: The GCP project identifier.
- `GCS_BUCKET_NAME=bulk-excel-processor`: Target GCS bucket where `.xlsx` files are uploaded.
- `FIRESTORE_DATABASE=(default)`: Target Firestore database instance name.
- `GCP_SIGNER_SERVICE_ACCOUNT=excel-upload-signer@bulk-excel-processor.iam.gserviceaccount.com`: Service account used via IAM Credentials API to sign GCS upload URLs without requiring a local private key file.
- `PUBSUB_TOPIC=excel-processing-jobs`: The Pub/Sub topic ID where confirmed jobs are published.

### 3.2 `backend/requirements.txt`
Lists pinned Python dependencies, centered around:
- **Web Framework & Server:** `fastapi`, `starlette`, `uvicorn`, `pydantic`
- **GCP SDKs:** `google-cloud-storage`, `google-cloud-firestore`, `google-cloud-pubsub`, `google-auth`, `grpc-google-iam-v1`
- **Utilities:** `python-dotenv`, `openpyxl`

---

## 4. Detailed File & Function Breakdown

### 4.1 `backend/app/services/gcp.py`
**Purpose:** Centralizes initialization of Google Cloud Storage and Firestore clients so they can be imported and reused across routes.

#### Execution & Module-Level Structure:
1. Calls `load_dotenv()` to load variables from `backend/.env`.
2. Reads `PROJECT_ID` (`GCP_PROJECT_ID`) and `BUCKET_NAME` (`GCS_BUCKET_NAME`) from `os.environ`.
3. Initializes singletons:
   - `storage_client = storage.Client(project=PROJECT_ID)`
   - `firestore_client = firestore.Client(project=PROJECT_ID, database=os.getenv("FIRESTORE_DATABASE", "(default)"))`

---

### 4.2 `backend/app/services/signed_urls.py`
**Purpose:** Generates time-limited **GCS V4 Signed URLs** (`PUT`) using IAM remote signing (`iam.Signer`). This allows local Application Default Credentials (ADC) or Cloud Run service accounts to sign URLs via the IAM API without needing a downloaded JSON private key.

#### Constants & Configuration:
- `BUCKET_NAME`: Loaded from `os.environ["GCS_BUCKET_NAME"]`.
- `SIGNER_EMAIL`: Loaded from `os.environ["GCP_SIGNER_SERVICE_ACCOUNT"]`.
- `CONTENT_TYPE`: `"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"` (strictly enforces `.xlsx` MIME type during upload).

#### Classes & Functions:

1. **`class IAMSigningCredentials(Signing)`**
   - **Purpose:** Custom credential wrapper implementing `google.auth.credentials.Signing` so `blob.generate_signed_url()` can sign bytes remotely via the GCP IAM `signBlob` API.
   - **`__init__(self, credentials)`**: Stores base ADC `credentials` and initializes `self._signer = iam.Signer(Request(), credentials, SIGNER_EMAIL)`.
   - **`signer` (property)**: Returns `self._signer`.
   - **`signer_email` (property)**: Returns `SIGNER_EMAIL`.
   - **`universe_domain` (property)**: Returns `"googleapis.com"`.
   - **`sign_bytes(self, message: bytes) -> bytes`**: Delegates byte-signing to `self._signer.sign(message)`.

2. **`generate_upload_url(object_name: str) -> str`**
   - **Input:** `object_name` (e.g., `"uploads/<job_id>/input.xlsx"`).
   - **Working:**
     1. Fetches Application Default Credentials via `google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])`.
     2. Checks `if not credentials.valid:` and refreshes token via `credentials.refresh(Request())`.
     3. Wraps credentials in `IAMSigningCredentials(credentials)`.
     4. Instantiates a `storage.Client` and gets a reference to `blob = storage_client.bucket(BUCKET_NAME).blob(object_name)`.
     5. Calls and returns `blob.generate_signed_url(...)` with:
        - `version="v4"`
        - `expiration=timedelta(minutes=15)` (URL valid for 15 minutes)
        - `method="PUT"`
        - `content_type=CONTENT_TYPE`
        - `credentials=signing_credentials`
        - `query_parameters={"ifGenerationMatch": "0"}` (ensures object cannot overwrite an existing file; write succeeds only if the object does not yet exist).

---

### 4.3 `backend/app/services/pubsub.py`
**Purpose:** Publishes job notifications to Google Cloud Pub/Sub once an Excel file upload is confirmed.

#### Module-Level Setup:
- Loads `.env` and reads `PROJECT_ID` (`GCP_PROJECT_ID`) and `TOPIC_ID` (`PUBSUB_TOPIC`).
- Creates `publisher = pubsub_v1.PublisherClient()`.
- Computes the full topic resource path: `TOPIC_PATH = publisher.topic_path(PROJECT_ID, TOPIC_ID)` (`projects/<PROJECT_ID>/topics/<TOPIC_ID>`).

#### Functions:
1. **`publish_processing_job(job_id: str, object_name: str) -> str`**
   - **Input:** `job_id` (UUID string) and `object_name` (GCS path string).
   - **Working:**
     1. Builds dictionary `{"job_id": job_id, "object_name": object_name}`.
     2. Serializes to JSON and encodes to UTF-8 bytes (`message_bytes`).
     3. Publishes asynchronously via `future = publisher.publish(TOPIC_PATH, data=message_bytes)`.
     4. Blocks up to 15 seconds via `message_id = future.result(timeout=15)` waiting for Pub/Sub server acknowledgement.
     5. Returns the published `message_id`.

---

### 4.4 `backend/app/routes/jobs.py`
**Purpose:** Defines the `/jobs` REST API router that manages the full lifecycle of an upload job before worker processing begins.

#### Models:
- **`class CreateJobRequest(BaseModel)`**
  - Schema for `POST /jobs` request body.
  - Field: `filename: str = Field(min_length=1)`.

#### Route Functions:

1. **`create_job(request: CreateJobRequest)` — `POST /jobs` (HTTP 201)**
   - **Purpose:** Validates the requested file extension, creates a new job record in Firestore, and generates a GCS Signed URL for direct client upload.
   - **Exact Working:**
     1. Extracts `filename = request.filename`.
     2. Validates file suffix: `if Path(filename).suffix.lower() != ".xlsx":` raises `HTTPException(400, "Only .xlsx files are supported")`.
     3. Generates a unique UUIDv4 `job_id = str(uuid4())`.
     4. Constructs the target GCS key: `object_name = f"uploads/{job_id}/input.xlsx"`.
     5. Prepares `job_data` dict with `job_id`, `filename`, `object_name`, `status="AwaitingUpload"`, and `created_at=datetime.now(timezone.utc)`.
     6. Calls `generate_upload_url(object_name)` to obtain the V4 Signed PUT URL.
     7. Writes `job_data` to Firestore at `jobs/{job_id}` via `.set(job_data)`.
     8. Returns JSON containing `job_id`, `filename`, `status`, `object_name`, `upload_url`, `upload_method` (`"PUT"`), and `upload_content_type`.

2. **`get_job(job_id: str)` — `GET /jobs/{job_id}` (HTTP 200 / 404)**
   - **Purpose:** Fetches full job metadata document from Firestore.
   - **Exact Working:**
     1. Queries `firestore_client.collection("jobs").document(job_id).get()`.
     2. If `not job.exists`, raises `HTTPException(404, detail="Job not found")`.
     3. Returns `job.to_dict()`.

3. **`get_job_status(job_id: str)` — `GET /jobs/{job_id}/status` (HTTP 200 / 404)**
   - **Purpose:** Lightweight endpoint returning only the `status` string of a job.
   - **Exact Working:**
     1. Queries `firestore_client.collection("jobs").document(job_id).get()`.
     2. If `not job.exists`, raises `HTTPException(404, detail="Job not found")`.
     3. Returns `job.to_dict()["status"]`.

4. **`confirm_upload(job_id: str)` — `POST /jobs/{job_id}/confirm` (HTTP 200 / 400 / 404 / 409 / 503)**
   - **Purpose:** Verifies that the client actually uploaded the `.xlsx` file to GCS, atomically transitions the job status using a Firestore transaction to prevent duplicate queuing, publishes the job to Pub/Sub, and marks the job as `Queued`.
   - **Nested Helper Function — `claim_for_queueing(transaction)` (decorated with `@firestore.transactional`):**
     1. Reads `job_ref.get(transaction=transaction)` inside a Firestore atomic transaction.
     2. Returns `("NotFound", None)` if the document does not exist.
     3. Reads `current_status = job.get("status")`:
        - If in `("Queued", "Processing", "Completed")`, returns `(current_status, job)` (idempotent no-op).
        - If `"Queueing"`, returns `("Queueing", job)` (another request is currently queueing it).
        - If not in `("AwaitingUpload", "Uploaded")`, returns `("Invalid", job)`.
     4. Atomically updates status to `"Queueing"` via `transaction.update(job_ref, {"status": "Queueing"})` and returns `("Claimed", job)`.
   - **Exact Working of `confirm_upload`:**
     1. Performs an initial pre-check read (`snapshot = job_ref.get()`):
        - Raises `404` if missing.
        - Returns early with current status if already in `("Queued", "Processing", "Completed")` or `"Queueing"` (idempotent response).
        - Raises `409 Conflict` (`"Job is not eligible for queueing"`) if status is not in `("AwaitingUpload", "Uploaded", "Queueing")`.
     2. Verifies GCS object existence via `blob = storage_client.bucket(BUCKET_NAME).blob(job["object_name"])` and `blob.exists()`:
        - If `not blob.exists()`, raises `400 Bad Request` (`"Excel file has not been uploaded to GCS"`).
     3. Starts a Firestore transaction and calls `claim_for_queueing(transaction)`:
        - Handles race conditions: raises `404` on `"NotFound"`, `409` on `"Invalid"`, or returns early if another concurrent call already moved it to `"Queueing"` / `"Queued"`.
     4. Calls `publish_processing_job(job_id, claimed_job["object_name"])`:
        - If an exception occurs, raises `503 Service Unavailable` (`"Queue publication could not be confirmed. Job requires reconciliation."`) and intentionally leaves the job in `"Queueing"` state rather than resetting to `"Uploaded"`, because Pub/Sub publication might have succeeded even if the network acknowledgement timed out.
     5. Updates `jobs/{job_id}` in Firestore with:
        - `status`: `"Queued"`
        - `uploaded_at`: `firestore.SERVER_TIMESTAMP`
        - `file_size`: `blob.size`
        - `pubsub_message_id`: `message_id`
        - `queued_at`: `firestore.SERVER_TIMESTAMP`
     6. Returns `{"job_id": job_id, "status": "Queued", "message": "Excel processing job submitted successfully"}`.

---

### 4.5 `backend/app/main.py`
**Purpose:** Entry point for the FastAPI application, registering routers, CORS middleware, and diagnostic endpoints.

#### Module-Level Setup:
1. Instantiates `app = FastAPI(title="Bulk Excel Processor API", description="Asynchronous Excel Upload Processing", version="1.0.0")`.
2. Mounts `jobs_router` via `app.include_router(jobs_router)`.
3. Configures `CORSMiddleware` allowing origin `"http://localhost:5173"` (the Vite React frontend), all methods (`"*"`), and all headers (`"*"`).

#### Route Functions:
1. **`root()` — `GET /`**
   - Async endpoint returning `{"message": "Bulk Excel Processor API is running"}`.
2. **`health_check()` — `GET /health`**
   - Async health probe returning `{"status": "healthy", "service": "bulk-excel-api"}`.
3. **`test_gcp_connection()` — `GET /test-gcp`**
   - Diagnostic endpoint that verifies connectivity to both GCS and Firestore:
     1. Calls `storage_client.get_bucket(BUCKET_NAME)`.
     2. Calls `firestore_client.collection("jobs").limit(1).get()`.
     3. Returns connection status, bucket name, and count of sample jobs found (`0` or `1`).
     4. Catches `GoogleAPICallError` and raises `HTTPException(503, detail="GCP connection failed: <ErrorType>")`.

---

## 5. Function Call Hierarchy

### 5.1 Application Startup & Module Import Hierarchy

```text
uvicorn app.main:app
└── backend/app/main.py
    ├── FastAPI()                                     [Initializes ASGI application]
    ├── app.include_router(jobs_router)               [Mounts /jobs endpoints]
    │   └── backend/app/routes/jobs.py
    │       ├── backend/app/services/gcp.py
    │       │   ├── load_dotenv()
    │       │   ├── storage.Client(project=PROJECT_ID)
    │       │   └── firestore.Client(project=PROJECT_ID, database=...)
    │       ├── backend/app/services/signed_urls.py
    │       │   └── load_dotenv()
    │       └── backend/app/services/pubsub.py
    │           ├── load_dotenv()
    │           ├── pubsub_v1.PublisherClient()
    │           └── publisher.topic_path(PROJECT_ID, TOPIC_ID)
    └── app.add_middleware(CORSMiddleware, ...)       [Enables CORS for localhost:5173]
```

---

### 5.2 Endpoint-by-Endpoint Runtime Call Hierarchy

#### A. `POST /jobs` (Create Job & Generate Signed Upload URL)
```text
Client Request: POST /jobs {"filename": "data.xlsx"}
└── jobs.create_job(request: CreateJobRequest)                [app/routes/jobs.py]
    ├── Path(filename).suffix.lower()                         [pathlib]
    ├── uuid4()                                               [uuid]
    ├── datetime.now(timezone.utc)                            [datetime]
    ├── signed_urls.generate_upload_url(object_name)          [app/services/signed_urls.py]
    │   ├── google.auth.default(scopes=[...])                 [google.auth]
    │   ├── credentials.refresh(Request())                    [google.auth - if token expired]
    │   ├── IAMSigningCredentials(credentials)                [app/services/signed_urls.py]
    │   │   └── iam.Signer(Request(), credentials, SIGNER_EMAIL)
    │   ├── storage.Client(project=..., credentials=...)      [google.cloud.storage]
    │   ├── storage_client.bucket(BUCKET_NAME).blob(object_name)
    │   └── blob.generate_signed_url(...)                     [google.cloud.storage]
    │       └── IAMSigningCredentials.sign_bytes(message)     [app/services/signed_urls.py]
    │           └── iam.Signer.sign(message)                  [GCP IAM Credentials API]
    └── firestore_client.collection("jobs").document(job_id).set(job_data) [GCP Firestore]
```

#### B. `POST /jobs/{job_id}/confirm` (Confirm Upload & Queue Job)
```text
Client Request: POST /jobs/{job_id}/confirm
└── jobs.confirm_upload(job_id: str)                          [app/routes/jobs.py]
    ├── firestore_client.collection("jobs").document(job_id)  [Reference: job_ref]
    ├── job_ref.get()                                         [Pre-check Firestore document]
    │   └── snapshot.to_dict()
    ├── storage_client.bucket(BUCKET_NAME).blob(object_name)  [Reference: GCS blob]
    ├── blob.exists()                                         [Verify file uploaded to GCS]
    ├── firestore_client.transaction()                        [Create Firestore transaction]
    ├── claim_for_queueing(transaction)                       [Transactional state transition]
    │   ├── job_ref.get(transaction=transaction)              [Transactional read]
    │   ├── snapshot.to_dict()
    │   └── transaction.update(job_ref, {"status": "Queueing"}) [Atomic lock]
    ├── pubsub.publish_processing_job(job_id, object_name)    [app/services/pubsub.py]
    │   ├── json.dumps(message).encode("utf-8")
    │   ├── publisher.publish(TOPIC_PATH, data=message_bytes) [GCP Pub/Sub async publish]
    │   └── future.result(timeout=15)                         [Wait for Pub/Sub ack]
    └── job_ref.update({"status": "Queued", ...})             [Finalize Firestore state]
```

#### C. `GET /jobs/{job_id}` & `GET /jobs/{job_id}/status` (Job Inspection)
```text
Client Request: GET /jobs/{job_id}
└── jobs.get_job(job_id: str)                                 [app/routes/jobs.py]
    ├── firestore_client.collection("jobs").document(job_id).get()
    └── job.to_dict()

Client Request: GET /jobs/{job_id}/status
└── jobs.get_job_status(job_id: str)                          [app/routes/jobs.py]
    ├── firestore_client.collection("jobs").document(job_id).get()
    └── job.to_dict()["status"]
```

#### D. `GET /`, `GET /health`, `GET /test-gcp` (System & Diagnostics)
```text
Client Request: GET /
└── main.root()                                               [app/main.py]

Client Request: GET /health
└── main.health_check()                                       [app/main.py]

Client Request: GET /test-gcp
└── main.test_gcp_connection()                                [app/main.py]
    ├── storage_client.get_bucket(BUCKET_NAME)                [GCP Cloud Storage check]
    └── firestore_client.collection("jobs").limit(1).get()    [GCP Firestore check]
```

---

## 6. Job State Machine Lifecycle (Backend Perspective)

```text
[POST /jobs]
     │
     ▼
┌────────────────┐
│ AwaitingUpload │ ──(Client PUTs .xlsx directly to GCS via Signed URL)──┐
└────────────────┘                                                       │
                                                                         ▼
                                                       [POST /jobs/{job_id}/confirm]
                                                                         │
                                              (1. Verify blob.exists() in GCS)
                                              (2. Firestore Transaction locks job)
                                                                         │
                                                                         ▼
                                                                  ┌────────────┐
                                                                  │  Queueing  │
                                                                  └────────────┘
                                                                         │
                                         ┌───────────────────────────────┴───────────────────────────────┐
                                         │ (Pub/Sub ack succeeds)                                        │ (Pub/Sub timeout/error)
                                         ▼                                                               ▼
                                  ┌────────────┐                                                  ┌────────────┐
                                  │   Queued   │                                                  │  Queueing  │
                                  └────────────┘                                                  └────────────┘
                                         │                                                   (HTTP 503: Held for
                         (Downstream Worker consumes Pub/Sub)                                   reconciliation)
                                         │
                                         ▼
                              Processing / Completed
```
