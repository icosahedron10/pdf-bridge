from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.models import (
    Document,
    DocumentState,
    OperationState,
    OperationType,
    QueueOperation,
    ScanState,
    utc_now,
)
from pdf_bridge.view_models import document_view


def _cleanup_document(*, state: DocumentState, filename: str) -> Document:
    document_id = uuid.uuid4()
    return Document(
        id=document_id,
        original_filename=filename,
        normalized_filename=filename.casefold(),
        storage_key=f"objects/{document_id}.pdf",
        size_bytes=1_024,
        sha256=document_id.hex * 2,
        idempotency_key=f"cleanup-test:{document_id}",
        state=state,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="cleanup-test-user",
        uploaded_at=utc_now(),
        ingested_at=utc_now() if state == DocumentState.DELETE_CLEANUP else None,
    )


@pytest.mark.parametrize(
    "state",
    (
        DocumentState.INGEST_FAILED,
        DocumentState.DELETE_FAILED,
        DocumentState.DELETE_CLEANUP,
        DocumentState.CANCEL_CLEANUP,
    ),
)
def test_failed_and_cleanup_pending_documents_are_never_previewable(
    state: DocumentState,
) -> None:
    view = document_view(_cleanup_document(state=state, filename=f"{state.value}.pdf"))

    assert view["can_preview"] is False
    assert view["cleanup_pending"] is (
        state
        in {
            DocumentState.DELETE_CLEANUP,
            DocumentState.CANCEL_CLEANUP,
        }
    )


def test_ingested_document_remains_previewable() -> None:
    document = _cleanup_document(state=DocumentState.INGESTED, filename="ingested.pdf")

    view = document_view(document)

    assert view["cleanup_pending"] is False
    assert view["can_preview"] is True


def test_cleanup_states_are_visible_in_their_web_surfaces(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    deletion = _cleanup_document(
        state=DocumentState.DELETE_CLEANUP,
        filename="deletion-cleanup-pending.pdf",
    )
    cancellation = _cleanup_document(
        state=DocumentState.CANCEL_CLEANUP,
        filename="cancellation-cleanup-pending.pdf",
    )
    deletion_operation = QueueOperation(
        document=deletion,
        operation_type=OperationType.DELETE,
        state=OperationState.SUCCEEDED,
        attempt=1,
        created_at=utc_now(),
        completed_at=utc_now(),
    )
    cancellation_operation = QueueOperation(
        document=cancellation,
        operation_type=OperationType.INGEST,
        state=OperationState.CANCELLED,
        attempt=1,
        created_at=utc_now(),
        completed_at=utc_now(),
    )
    with session_factory() as session:
        session.add_all([deletion_operation, cancellation_operation])
        session.commit()

    library = client.get("/library")
    assert library.status_code == 200
    assert deletion.original_filename in library.text
    assert "Deletion cleanup" in library.text
    assert cancellation.original_filename not in library.text
    assert f"/api/v1/documents/{deletion.id}/content" not in library.text

    deletion_queue = client.get("/queue?status=delete_cleanup")
    assert deletion_queue.status_code == 200
    assert deletion.original_filename in deletion_queue.text
    assert cancellation.original_filename not in deletion_queue.text
    assert f"/api/v1/documents/{deletion.id}/content" not in deletion_queue.text

    cancellation_queue = client.get("/queue?status=cancel_cleanup")
    assert cancellation_queue.status_code == 200
    assert cancellation.original_filename in cancellation_queue.text
    assert deletion.original_filename not in cancellation_queue.text
    assert "Cancellation cleanup" in cancellation_queue.text
    assert f"/api/v1/documents/{cancellation.id}/content" not in cancellation_queue.text
    assert f'action="/api/v1/queue/{cancellation_operation.id}"' in cancellation_queue.text
    assert 'data-method="DELETE"' in cancellation_queue.text
    assert ">Retry cleanup</button>" in cancellation_queue.text

    deletion_detail = client.get(f"/documents/{deletion.id}")
    assert deletion_detail.status_code == 200
    assert "Downstream deletion is complete; canonical cleanup is pending" in deletion_detail.text
    assert f"/api/v1/documents/{deletion.id}/content" not in deletion_detail.text

    cancellation_detail = client.get(f"/documents/{cancellation.id}")
    assert cancellation_detail.status_code == 200
    assert "Queue removal is waiting for storage cleanup" in cancellation_detail.text
    assert f"/api/v1/documents/{cancellation.id}/content" not in cancellation_detail.text
    assert f'action="/api/v1/queue/{cancellation_operation.id}"' in cancellation_detail.text


def test_latest_operation_is_chronological_across_operation_types(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    document = _cleanup_document(
        state=DocumentState.DELETE_QUEUED,
        filename="retried-then-deleted.pdf",
    )
    ingest_operation = QueueOperation(
        document=document,
        operation_type=OperationType.INGEST,
        state=OperationState.SUCCEEDED,
        attempt=2,
        created_at=utc_now() - timedelta(hours=1),
        completed_at=utc_now() - timedelta(minutes=50),
    )
    delete_operation = QueueOperation(
        document=document,
        operation_type=OperationType.DELETE,
        state=OperationState.QUEUED,
        attempt=1,
        created_at=utc_now(),
    )
    with session_factory() as session:
        session.add_all([ingest_operation, delete_operation])
        session.commit()

    queue = client.get("/api/v1/queue").json()
    assert queue["items"][0]["id"] == str(delete_operation.id)
    assert queue["items"][0]["operation_type"] == "DELETE"

    detail = client.get(f"/documents/{document.id}")
    assert detail.status_code == 200
    assert str(delete_operation.id) in detail.text
    assert str(ingest_operation.id) not in detail.text
