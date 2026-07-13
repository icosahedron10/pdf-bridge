"""Transaction and workflow orchestration for document use cases."""

from __future__ import annotations

from threading import RLock
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    DecisionRequest,
    DocumentMutationResponse,
    UploadAcceptedResponse,
)
from pdf_bridge.core.config import Settings
from pdf_bridge.managers.worker import AnalysisWorker
from pdf_bridge.persistence.models import DecisionAction
from pdf_bridge.presentation.api_serializers import document_summary
from pdf_bridge.services import document, intake
from pdf_bridge.services.scanner import Scanner
from pdf_bridge.services.storage import BinaryReadable

_DECISION_ACTIONS = {
    "keep": DecisionAction.KEEP,
    "replace": DecisionAction.REPLACE,
    "cancel": DecisionAction.CANCEL,
}


def upload_document(
    session: Session,
    *,
    settings: Settings,
    scanner: Scanner,
    transition_lock: RLock,
    worker: AnalysisWorker | None,
    file: BinaryReadable,
    filename: str,
    content_type: str | None,
    collection_key: str,
    header_idempotency_key: str | None,
    form_idempotency_key: str | None,
    actor_type: str,
    actor_id: str,
) -> UploadAcceptedResponse:
    """Stream, scan, register, and queue an upload for immediate analysis."""

    idempotency_key = document.validate_idempotency_key(
        header_value=header_idempotency_key,
        form_value=form_idempotency_key,
    )
    prepared = document.prepare_upload(
        settings=settings,
        scanner=scanner,
        file=file,
        filename=filename,
        content_type=content_type,
        collection_key=collection_key,
    )
    registration = None
    try:
        with transition_lock:
            try:
                registration = document.register_upload(
                    session,
                    prepared=prepared,
                    idempotency_key=idempotency_key,
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
                if registration.idempotent_replay:
                    prepared.staged.path.unlink(missing_ok=True)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                document.discard_promoted_upload(registration)
                return document.resolve_idempotency_conflict(
                    session,
                    prepared=prepared,
                    idempotency_key=idempotency_key,
                    cause=exc,
                )
            except Exception:
                session.rollback()
                document.discard_promoted_upload(registration)
                raise
    finally:
        prepared.staged.path.unlink(missing_ok=True)

    if registration is None:
        raise RuntimeError("upload registration unexpectedly missing")
    if worker is not None:
        worker.notify()
    return document.upload_accepted_response(registration)


def decide_upload(
    session: Session,
    *,
    transition_lock: RLock,
    worker: AnalysisWorker | None,
    upload_id: UUID,
    request: DecisionRequest,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
) -> DocumentMutationResponse:
    """Record a Keep, Replace, or Cancel decision and queue its work."""

    validated_key = document.validate_idempotency_key(
        header_value=idempotency_key, form_value=None
    )
    with transition_lock:
        try:
            record = intake.get_upload_document(session, upload_id)
            outcome = intake.record_decision(
                session,
                document=record,
                analysis_revision=request.analysis_revision,
                action=_DECISION_ACTIONS[request.action],
                target_document_id=request.target_document_id,
                idempotency_key=validated_key,
                actor_type=actor_type,
                actor_id=actor_id,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    if worker is not None:
        worker.notify()
    return DocumentMutationResponse(
        document=document_summary(outcome.document),
        operation_id=outcome.operation.id if outcome.operation else None,
        idempotent_replay=outcome.idempotent_replay,
    )


def retry_upload(
    session: Session,
    *,
    transition_lock: RLock,
    worker: AnalysisWorker | None,
    upload_id: UUID,
    actor_type: str,
    actor_id: str,
) -> DocumentMutationResponse:
    """Queue the next attempt for an upload whose last operation failed."""

    with transition_lock:
        try:
            record = intake.get_upload_document(session, upload_id)
            operation = intake.retry_upload(
                session, document=record, actor_type=actor_type, actor_id=actor_id
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    if worker is not None:
        worker.notify()
    return DocumentMutationResponse(
        document=document_summary(record), operation_id=operation.id
    )


def cancel_upload(
    session: Session,
    *,
    transition_lock: RLock,
    worker: AnalysisWorker | None,
    upload_id: UUID,
    actor_type: str,
    actor_id: str,
) -> DocumentMutationResponse:
    """Cancel unpublished work and queue cleanup of everything retained."""

    with transition_lock:
        try:
            record = intake.get_upload_document(session, upload_id)
            operation = intake.cancel_upload(
                session, document=record, actor_type=actor_type, actor_id=actor_id
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    if worker is not None:
        worker.notify()
    return DocumentMutationResponse(
        document=document_summary(record), operation_id=operation.id
    )


def request_deletion(
    session: Session,
    *,
    transition_lock: RLock,
    worker: AnalysisWorker | None,
    document_id: UUID,
    actor_type: str,
    actor_id: str,
    reason: str | None,
) -> DocumentMutationResponse:
    """Queue removal of an ingested document from retrieval and storage."""

    with transition_lock:
        try:
            record = intake.get_upload_document(session, document_id)
            operation = intake.request_deletion(
                session,
                document=record,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    if worker is not None:
        worker.notify()
    return DocumentMutationResponse(
        document=document_summary(record), operation_id=operation.id
    )
