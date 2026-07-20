from __future__ import annotations

import json
import os
from typing import Any, Optional

from azure.storage.blob import BlobServiceClient, ContentSettings

# Azure Functions provisions AzureWebJobsStorage automatically for the runtime
# itself; reused here for our own state (OAuth tokens, pending-authorization
# markers) instead of the function instance's local disk, which is ephemeral
# and not shared across instances/cold starts.
CONTAINER_NAME = os.getenv("JOBBER_STATE_CONTAINER", "jobber-monitor-state")

_container_client = None


def _get_container():
    global _container_client
    if _container_client is not None:
        return _container_client

    conn_str = os.getenv("AzureWebJobsStorage")
    if not conn_str:
        raise RuntimeError(
            "AzureWebJobsStorage is not set. Azure provisions this automatically for "
            "deployed Function Apps. For local runs, set it in local.settings.json "
            "(e.g. to an Azurite connection string, or a real storage account)."
        )

    service = BlobServiceClient.from_connection_string(conn_str)
    container = service.get_container_client(CONTAINER_NAME)
    if not container.exists():
        container.create_container()

    _container_client = container
    return _container_client


def load_json(blob_name: str) -> Optional[Any]:
    blob = _get_container().get_blob_client(blob_name)
    if not blob.exists():
        return None
    try:
        data = blob.download_blob().readall()
        return json.loads(data)
    except Exception as exc:
        print(f"STATE_LOAD_FAILED blob={blob_name}: {exc}")
        return None


def save_json(blob_name: str, payload: Any) -> None:
    blob = _get_container().get_blob_client(blob_name)
    body = json.dumps(payload, indent=2, sort_keys=True, default=str)
    blob.upload_blob(
        body,
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )


def delete_blob(blob_name: str) -> None:
    blob = _get_container().get_blob_client(blob_name)
    if blob.exists():
        blob.delete_blob()
