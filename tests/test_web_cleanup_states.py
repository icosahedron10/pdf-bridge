from __future__ import annotations

import uuid

import pytest

from pdf_bridge.persistence.models import (
    Document,
    DocumentState,
    OperationPhase,
    OperationState,
    OperationType,
    ScanState,
    WorkOperation,
    utc_now,
)
from pdf_bridge.presentation.view_models import document_view, operation_view
from pdf_bridge.services.web_page import build_queue_page


def _document(state: DocumentState, *, key: str) -> Document:
    return Document(
        id=uuid.uuid4(),
        original_filename=f"{key}.pdf",
        normalized_filename=f"{key}.pdf",
        storage_key=f"objects/{key}.pdf",
        size_bytes=100,
        sha256=(key[0] * 64)[:64],
        idempotency_key=f"idempotency-{key}",
        state=state,
        scan_state=ScanState.CLEAN,
        uploader_identity="test",
        collection_key="customer",
        ingested_at=utc_now() if state == DocumentState.INGESTED else None,
    )


@pytest.mark.parametrize(
    "state",
    [
        DocumentState.CLEANUP_PENDING,
        DocumentState.CLEANUP_FAILED,
        DocumentState.REJECTED,
        DocumentState.CANCELLED,
        DocumentState.DELETED,
    ],
)
def test_purging_and_tombstone_documents_are_never_previewable(state) -> None:
    assert document_view(_document(state, key=state.value.casefold()))["can_preview"] is False


def test_clean_retained_document_is_operator_previewable() -> None:
    assert document_view(_document(DocumentState.REVIEW_REQUIRED, key="review"))[
        "can_preview"
    ] is True


def test_operation_view_exposes_worker_phase_and_retry_metadata() -> None:
    document = _document(DocumentState.REPLACE_FAILED, key="replacement")
    operation = WorkOperation(
        document=document,
        operation_type=OperationType.INGEST,
        state=OperationState.FAILED,
        phase=OperationPhase.INGESTING,
        attempt=3,
        retryable=True,
        error="provider unavailable",
    )

    view = operation_view(operation)

    assert view["phase"] == "INGESTING"
    assert view["operation_status"] == "FAILED"
    assert view["attempt"] == 3
    assert view["retryable"] is True
    assert view["error"] == "provider unavailable"


def test_queue_page_uses_latest_operation_across_operation_types(
    settings, session_factory
) -> None:
    from pdf_bridge.services.web_page import WebRequestState

    with session_factory() as session:
        document = _document(DocumentState.CLEANUP_PENDING, key="latest")
        analyze = WorkOperation(
            document=document,
            operation_type=OperationType.ANALYZE,
            state=OperationState.SUCCEEDED,
            phase=OperationPhase.COMPLETE,
            attempt=1,
        )
        cleanup = WorkOperation(
            document=document,
            operation_type=OperationType.CLEANUP,
            state=OperationState.QUEUED,
            phase=OperationPhase.CLEANING_UP,
            attempt=1,
        )
        session.add_all([document, analyze, cleanup])
        session.commit()

        state = WebRequestState(
            request=None,
            settings=settings,
            csrf_token="token",
            actor_kind="anonymous",
            actor_identifier="test",
            app_version="test",
        )
        page = build_queue_page(
            state,
            session,
            status="all",
            collection="all",
            sort="created_at",
            order="asc",
            page=1,
        )

    assert page.context["operation_count"] == 1
    assert page.context["operations"][0]["operation_type"] == "CLEANUP"
