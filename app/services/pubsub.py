
import json
import os

from dotenv import load_dotenv
from google.cloud import pubsub_v1

load_dotenv()

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
TOPIC_ID = os.environ["PUBSUB_TOPIC"]

publisher = pubsub_v1.PublisherClient()

TOPIC_PATH = publisher.topic_path(
    PROJECT_ID,
    TOPIC_ID
)


def publish_processing_job(job_id: str, object_name: str) -> str:
    message = {
        "job_id": job_id,
        "object_name": object_name,
    }

    message_bytes = json.dumps(message).encode("utf-8")

    future = publisher.publish(
        TOPIC_PATH,
        data=message_bytes,
    )

    # Wait for Pub/Sub to acknowledge publication,
    # not for Excel processing to finish.
    message_id = future.result(timeout=15)

    return message_id
