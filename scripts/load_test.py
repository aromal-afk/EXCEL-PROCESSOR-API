"""
End-to-end concurrent upload/processing load test.

For each generated Workers-template file the script runs the same flow as
the React app (POST /jobs -> PUT to signed URL -> POST /jobs/{id}/confirm),
all files in parallel, then polls GET /jobs/{id} until every job reaches a
terminal status.

Timings reported per job:
  upload      client time for create + GCS PUT + confirm
  queue_wait  queued_at  -> started_at   (Pub/Sub delivery + worker pickup)
  processing  started_at -> completed_at (download, parse, validate, write)
  end_to_end  client start -> terminal status observed

Requires the API and a deployed worker behind a Pub/Sub push subscription
(Pub/Sub cannot push to localhost).

Usage:
    python scripts/load_test.py --api http://localhost:8000 --files 10 --rows 10000
"""

import argparse
import json
import random
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from openpyxl import Workbook

HEADERS = [
    "Name",
    "Description",
    "EmployeeID",
    "Title",
    "Employee Type(Contractor or Company Employee)",
    "Department",
    "Profile(System user, Operator, Worker)",
    "e-mail",
    "Equipment Models",
    "Device Types",
    "Role",
    "Shift",
    "Photo",
]

TERMINAL_STATUSES = {"Completed", "CompletedWithErrors", "Failed"}


def generate_workbook(path, rows, invalid_ratio, seed):
    rng = random.Random(seed)
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Template")
    sheet.append(HEADERS)

    for index in range(rows):
        invalid = rng.random() < invalid_ratio
        sheet.append([
            "" if invalid else f"Employee {index}",
            "Load test",
            f"LT{seed:03d}-{index:07d}",
            "Technician",
            rng.choice(["Contractor", "Company Employee"]),
            "Operations",
            rng.choice(["System user", "Operator", "Worker"]),
            "bad-email" if invalid else f"lt{seed}.{index}@example.com",
            "Model X",
            "Tablet",
            "Field",
            rng.choice(["Day", "Night"]),
            "",
        ])

    workbook.save(path)


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seconds_between(start, end):
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def submit(api, path):
    started = time.perf_counter()
    session = requests.Session()

    job = session.post(
        f"{api}/jobs", json={"filename": path.name}, timeout=30
    )
    job.raise_for_status()
    job = job.json()

    with open(path, "rb") as file:
        upload = session.put(
            job["upload_url"],
            data=file,
            headers={"Content-Type": job["upload_content_type"]},
            timeout=300,
        )
    upload.raise_for_status()

    confirm = session.post(
        f"{api}/jobs/{job['job_id']}/confirm", timeout=60
    )
    confirm.raise_for_status()

    return {
        "job_id": job["job_id"],
        "file": path.name,
        "client_started": started,
        "upload_s": time.perf_counter() - started,
    }


def wait_for_completion(api, jobs, poll_interval, timeout):
    pending = {job["job_id"]: job for job in jobs}
    deadline = time.perf_counter() + timeout

    while pending and time.perf_counter() < deadline:
        for job_id in list(pending):
            response = requests.get(f"{api}/jobs/{job_id}", timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get("status") in TERMINAL_STATUSES:
                job = pending.pop(job_id)
                job["status"] = data["status"]
                job["end_to_end_s"] = (
                    time.perf_counter() - job["client_started"]
                )
                job["valid_rows"] = data.get("valid_rows")
                job["invalid_rows"] = data.get("invalid_rows")
                job["attempts"] = data.get("attempt_count")
                job["queue_wait_s"] = seconds_between(
                    parse_time(data.get("queued_at")),
                    parse_time(data.get("started_at")),
                )
                job["processing_s"] = seconds_between(
                    parse_time(data.get("started_at")),
                    parse_time(data.get("completed_at")),
                )

        if pending:
            time.sleep(poll_interval)

    for job in pending.values():
        job["status"] = "TimedOut"


def summarize(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return "n/a"
    p95 = values[min(len(values) - 1, round(0.95 * (len(values) - 1)))]
    return (
        f"min {values[0]:7.2f}  median {statistics.median(values):7.2f}  "
        f"p95 {p95:7.2f}  max {values[-1]:7.2f}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--files", type=int, default=10)
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--invalid-ratio", type=float, default=0.05)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).parent / "results",
    )
    args = parser.parse_args()
    api = args.api.rstrip("/")

    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Generating {args.files} files x {args.rows:,} rows ...")
        paths = []
        for index in range(args.files):
            path = Path(temp_dir) / f"loadtest_{index:03d}.xlsx"
            generate_workbook(path, args.rows, args.invalid_ratio, index)
            paths.append(path)

        size_mb = paths[0].stat().st_size / 1_048_576
        print(f"Each file is {size_mb:.2f} MB. Uploading concurrently ...")

        wall_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.files) as pool:
            jobs = list(pool.map(lambda p: submit(api, p), paths))
        upload_wall = time.perf_counter() - wall_started

    print(f"All {len(jobs)} uploads confirmed in {upload_wall:.2f}s. Waiting ...")
    wait_for_completion(api, jobs, args.poll_interval, args.timeout)
    total_wall = time.perf_counter() - wall_started

    total_rows = args.files * args.rows
    statuses = {}
    for job in jobs:
        statuses[job["status"]] = statuses.get(job["status"], 0) + 1

    print()
    print(f"Files            : {args.files} x {args.rows:,} rows ({size_mb:.2f} MB each)")
    print(f"Statuses         : {statuses}")
    print(f"Upload phase wall: {upload_wall:.2f}s")
    print(f"Total wall time  : {total_wall:.2f}s")
    print(f"Throughput       : {total_rows / total_wall:,.0f} rows/s overall")
    print(f"upload     (s)   : {summarize(j['upload_s'] for j in jobs)}")
    print(f"queue_wait (s)   : {summarize(j.get('queue_wait_s') for j in jobs)}")
    print(f"processing (s)   : {summarize(j.get('processing_s') for j in jobs)}")
    print(f"end_to_end (s)   : {summarize(j.get('end_to_end_s') for j in jobs)}")

    args.output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_path = args.output / f"load_test_{args.files}x{args.rows}_{stamp}.json"
    result_path.write_text(json.dumps({
        "files": args.files,
        "rows_per_file": args.rows,
        "file_size_mb": round(size_mb, 2),
        "upload_wall_s": round(upload_wall, 2),
        "total_wall_s": round(total_wall, 2),
        "rows_per_second": round(total_rows / total_wall),
        "statuses": statuses,
        "jobs": [
            {k: v for k, v in job.items() if k != "client_started"}
            for job in jobs
        ],
    }, indent=2))
    print(f"\nSaved {result_path}")


if __name__ == "__main__":
    main()
