"""Transactional document, queue, and batch lifecycle operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from pdf_bridge.models import (
    AuditEvent,
    BatchState,
    Document,
    DocumentState,
    JobBatch,
    OperationState,
    OperationType,
    QueueOperation,
    ScanState,
    utc_now,
)
from pdf_bridge.scanner import Scanner, ScanResult
from pdf_bridge.schemas import (
    BatchResultsRequest,
    HistoricalImportItemResult,
    HistoricalImportManifest,
    HistoricalImportResponse,
)
from pdf_bridge.storage import (
    PromotedFile,
    StagedFile,
    StorageLayout,
    copy_source_to_temporary,
    normalize_filename,
    promote_staged_file,
    validate_pdf_filename,
    validate_source_path,
)

ACTIVE_DOCUMENT_STATES = (
    DocumentState.QUEUED,
    DocumentState.CLAIMED,
    DocumentState.STAGED,
    DocumentState.INGESTED,
    DocumentState.INGEST_FAILED,
    DocumentState.DELETE_QUEUED,
    DocumentState.DELETE_CLAIMED,
    DocumentState.DELETE_CLEANUP,
    DocumentState.DELETE_FAILED,
    DocumentState.CANCEL_CLEANUP,
)


class LifecycleError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class DuplicateDocumentError(LifecycleError):
    def __init__(self, document: Document) -> None:
        super().__init__(
            "An active document has identical file contents.",
            code="exact-duplicate",
            status=409,
        )
        self.document = document


class PossibleDuplicateError(LifecycleError):
    def __init__(self, documents: list[Document]) -> None:
        super().__init__(
            "A document with the same normalized filename and size already exists.",
            code="possible-duplicate-confirmation-required",
            status=409,
        )
        self.documents = documents


@dataclass(frozen=True, slots=True)
class UploadRegistration:
    document: Document
    operation: QueueOperation
    promoted: PromotedFile | None
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class BatchClaim:
    batch: JobBatch
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class BatchStage:
    batch: JobBatch
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class DeletionCleanup:
    document_id: uuid.UUID
    storage_key: str


@dataclass(frozen=True, slots=True)
class BatchReport:
    batch: JobBatch
    succeeded: int
    failed: int
    cleanup_items: tuple[DeletionCleanup, ...] = ()
    idempotent_replay: bool = False


def _audit(
    session: Session,
    *,
    event_type: str,
    actor_type: str,
    actor_id: str,
    document: Document | None = None,
    operation: QueueOperation | None = None,
    batch: JobBatch | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        document=document,
        document_id=document.id if document else None,
        operation_id=operation.id if operation else None,
        batch_id=batch.id if batch else None,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        details=details or {},
    )
    session.add(event)
    return event


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def find_preflight_duplicates(
    session: Session, *, normalized_filename: str, size_bytes: int
) -> list[Document]:
    return list(
        session.scalars(
            select(Document)
            .where(
                Document.normalized_filename == normalized_filename,
                Document.size_bytes == size_bytes,
                Document.state.in_(ACTIVE_DOCUMENT_STATES),
            )
            .order_by(Document.uploaded_at.desc())
            .limit(100)
        ).all()
    )


def find_active_checksum_duplicate(session: Session, sha256: str) -> Document | None:
    return session.scalar(
        select(Document)
        .where(Document.sha256 == sha256, Document.state.in_(ACTIVE_DOCUMENT_STATES))
        .order_by(Document.uploaded_at.desc())
        .limit(1)
    )


def register_staged_upload(
    session: Session,
    *,
    staged: StagedFile,
    layout: StorageLayout,
    filename: str,
    idempotency_key: str,
    actor_type: str,
    actor_id: str,
    scan_result: ScanResult,
    allow_possible_duplicate: bool,
) -> UploadRegistration:
    existing = (
        session.execute(
            select(Document)
            .where(Document.idempotency_key == idempotency_key)
            .options(joinedload(Document.operations))
        )
        .unique()
        .scalar_one_or_none()
    )
    if existing is not None:
        if (
            existing.sha256 != staged.sha256
            or existing.size_bytes != staged.size_bytes
            or existing.normalized_filename != normalize_filename(filename)
        ):
            raise LifecycleError(
                "The idempotency key was already used for a different file.",
                code="idempotency-key-conflict",
            )
        ingest_operations = [
            item for item in existing.operations if item.operation_type == OperationType.INGEST
        ]
        if not ingest_operations:
            raise LifecycleError(
                "The upload idempotency record has no ingestion operation.",
                code="catalog-inconsistent",
                status=500,
            )
        operation = max(ingest_operations, key=lambda item: (item.attempt, item.created_at))
        return UploadRegistration(existing, operation, None, idempotent_replay=True)

    display_filename = validate_pdf_filename(filename)
    normalized = normalize_filename(display_filename)
    duplicate = find_active_checksum_duplicate(session, staged.sha256)
    if duplicate is not None:
        raise DuplicateDocumentError(duplicate)
    possible = find_preflight_duplicates(
        session, normalized_filename=normalized, size_bytes=staged.size_bytes
    )
    if possible and not allow_possible_duplicate:
        raise PossibleDuplicateError(possible)
    if scan_result.state != ScanState.CLEAN:
        raise LifecycleError(
            "Only files reported clean by the configured scanner may be queued.",
            code="scan-not-clean",
            status=422,
        )

    document = Document(
        id=uuid.uuid4(),
        original_filename=display_filename,
        normalized_filename=normalized,
        size_bytes=staged.size_bytes,
        sha256=staged.sha256,
        idempotency_key=idempotency_key,
        state=DocumentState.QUEUED,
        scan_state=scan_result.state,
        scan_engine=scan_result.engine,
        scan_signature=scan_result.signature,
        scanned_at=scan_result.scanned_at,
        uploader_identity=actor_id,
    )
    operation = QueueOperation(
        document=document,
        operation_type=OperationType.INGEST,
        state=OperationState.QUEUED,
        attempt=1,
    )
    promoted: PromotedFile | None = None
    try:
        promoted = promote_staged_file(staged, layout, document.id)
        document.storage_key = promoted.storage_key
        session.add_all([document, operation])
        session.flush()
    except Exception:
        if promoted is not None:
            promoted.path.unlink(missing_ok=True)
        raise

    _audit(
        session,
        event_type="upload_received",
        actor_type=actor_type,
        actor_id=actor_id,
        document=document,
        operation=operation,
        details={"status": DocumentState.QUEUED.value, "detail": "PDF queued for ingestion."},
    )
    _audit(
        session,
        event_type="malware_scan_clean",
        actor_type="scanner",
        actor_id=scan_result.engine,
        document=document,
        operation=operation,
        details={"status": scan_result.state.value},
    )
    if possible:
        _audit(
            session,
            event_type="possible_duplicate_confirmed",
            actor_type=actor_type,
            actor_id=actor_id,
            document=document,
            operation=operation,
            details={"matched_document_ids": [str(item.id) for item in possible]},
        )
    return UploadRegistration(document, operation, promoted)


def cancel_queued_document(
    session: Session, *, operation_id: uuid.UUID, actor_type: str, actor_id: str
) -> tuple[Document, str | None]:
    operation = session.scalar(
        select(QueueOperation)
        .where(QueueOperation.id == operation_id)
        .options(joinedload(QueueOperation.document))
    )
    if operation is None:
        raise LifecycleError(
            "Queue operation was not found.", code="operation-not-found", status=404
        )
    document = operation.document
    if operation.state == OperationState.CANCELLED:
        if document.state == DocumentState.CANCEL_CLEANUP and document.storage_key:
            return document, document.storage_key
        if document.state == DocumentState.CANCELLED:
            return document, None
    if operation.state != OperationState.QUEUED or document.state != DocumentState.QUEUED:
        raise LifecycleError(
            "Only an unclaimed ingestion upload can be removed from the queue.",
            code="operation-already-claimed",
        )
    if not document.storage_key:
        raise LifecycleError(
            "The queued document has no canonical PDF storage key.",
            code="catalog-inconsistent",
            status=500,
        )
    operation.state = OperationState.CANCELLED
    operation.completed_at = utc_now()
    document.state = DocumentState.CANCEL_CLEANUP
    storage_key = document.storage_key
    _audit(
        session,
        event_type="queue_cleanup_pending",
        actor_type=actor_type,
        actor_id=actor_id,
        document=document,
        operation=operation,
        details={"status": DocumentState.CANCEL_CLEANUP.value},
    )
    session.flush()
    return document, storage_key


def finalize_cancelled_storage(
    session: Session,
    *,
    document_id: uuid.UUID,
    storage_key: str,
    actor_type: str,
    actor_id: str,
) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise LifecycleError("Document was not found.", code="document-not-found", status=404)
    if document.state == DocumentState.CANCELLED and document.storage_key is None:
        return document
    if document.state != DocumentState.CANCEL_CLEANUP or document.storage_key != storage_key:
        raise LifecycleError(
            "Cancellation cleanup no longer matches the catalog record.",
            code="cleanup-state-conflict",
        )
    document.storage_key = None
    document.state = DocumentState.CANCELLED
    document.cancelled_at = utc_now()
    _audit(
        session,
        event_type="queue_upload_cancelled",
        actor_type=actor_type,
        actor_id=actor_id,
        document=document,
        details={"status": DocumentState.CANCELLED.value},
    )
    session.flush()
    return document


def retry_failed_document(
    session: Session, *, operation_id: uuid.UUID, actor_type: str, actor_id: str
) -> QueueOperation:
    failed = session.scalar(
        select(QueueOperation)
        .where(QueueOperation.id == operation_id)
        .options(joinedload(QueueOperation.document))
    )
    if failed is None:
        raise LifecycleError(
            "Queue operation was not found.", code="operation-not-found", status=404
        )
    expected_document_state = (
        DocumentState.INGEST_FAILED
        if failed.operation_type == OperationType.INGEST
        else DocumentState.DELETE_FAILED
    )
    if failed.state != OperationState.FAILED or failed.document.state != expected_document_state:
        raise LifecycleError(
            "Only the current failed operation can be retried.", code="operation-not-retryable"
        )
    latest_attempt = (
        session.scalar(
            select(func.max(QueueOperation.attempt)).where(
                QueueOperation.document_id == failed.document_id,
                QueueOperation.operation_type == failed.operation_type,
            )
        )
        or 0
    )
    if failed.attempt != latest_attempt:
        raise LifecycleError(
            "A newer attempt supersedes this failed operation.",
            code="operation-superseded",
        )
    operation = QueueOperation(
        document=failed.document,
        operation_type=failed.operation_type,
        state=OperationState.QUEUED,
        attempt=latest_attempt + 1,
    )
    failed.document.state = (
        DocumentState.QUEUED
        if failed.operation_type == OperationType.INGEST
        else DocumentState.DELETE_QUEUED
    )
    failed.document.last_error = None
    session.add(operation)
    session.flush()
    _audit(
        session,
        event_type="operation_retried",
        actor_type=actor_type,
        actor_id=actor_id,
        document=failed.document,
        operation=operation,
        details={"previous_operation_id": str(failed.id), "attempt": operation.attempt},
    )
    return operation


def queue_document_deletion(
    session: Session,
    *,
    document_id: uuid.UUID,
    actor_type: str,
    actor_id: str,
    reason: str | None = None,
) -> QueueOperation:
    document = session.get(Document, document_id)
    if document is None:
        raise LifecycleError("Document was not found.", code="document-not-found", status=404)
    if document.state == DocumentState.DELETE_FAILED:
        failed = session.scalar(
            select(QueueOperation)
            .where(
                QueueOperation.document_id == document.id,
                QueueOperation.operation_type == OperationType.DELETE,
                QueueOperation.state == OperationState.FAILED,
            )
            .order_by(QueueOperation.attempt.desc())
            .limit(1)
        )
        if failed is None:
            raise LifecycleError("Failed deletion record is missing.", code="catalog-inconsistent")
        return retry_failed_document(
            session, operation_id=failed.id, actor_type=actor_type, actor_id=actor_id
        )
    if document.state != DocumentState.INGESTED:
        raise LifecycleError(
            "Only an ingested document can be queued for deletion.",
            code="document-not-deletable",
        )
    latest_attempt = (
        session.scalar(
            select(func.max(QueueOperation.attempt)).where(
                QueueOperation.document_id == document.id,
                QueueOperation.operation_type == OperationType.DELETE,
            )
        )
        or 0
    )
    operation = QueueOperation(
        document=document,
        operation_type=OperationType.DELETE,
        state=OperationState.QUEUED,
        attempt=latest_attempt + 1,
    )
    document.state = DocumentState.DELETE_QUEUED
    session.add(operation)
    session.flush()
    _audit(
        session,
        event_type="deletion_requested",
        actor_type=actor_type,
        actor_id=actor_id,
        document=document,
        operation=operation,
        details={
            "status": DocumentState.DELETE_QUEUED.value,
            "detail": reason or "Deletion queued for the pipeline.",
        },
    )
    return operation


def expire_claims(session: Session, *, now: Any | None = None) -> int:
    current_time = now or utc_now()
    batches = (
        session.scalars(
            select(JobBatch)
            .where(
                JobBatch.state == BatchState.CLAIMED,
                JobBatch.lease_expires_at <= current_time,
            )
            .options(joinedload(JobBatch.operations).joinedload(QueueOperation.document))
        )
        .unique()
        .all()
    )
    count = 0
    for batch in batches:
        batch.state = BatchState.EXPIRED
        for operation in batch.operations:
            if operation.state != OperationState.CLAIMED:
                continue
            operation.state = OperationState.QUEUED
            operation.claimed_at = None
            operation.lease_expires_at = None
            operation.document.state = (
                DocumentState.QUEUED
                if operation.operation_type == OperationType.INGEST
                else DocumentState.DELETE_QUEUED
            )
            _audit(
                session,
                event_type="claim_expired",
                actor_type="system",
                actor_id="lease-expiry",
                document=operation.document,
                operation=operation,
                batch=batch,
            )
            count += 1
    return count


def claim_batch(
    session: Session,
    *,
    request_id: str,
    limit: int,
    lease_minutes: int,
    actor_id: str = "jenkins",
) -> BatchClaim:
    # Expire old claims before resolving an idempotency key. Otherwise a replay
    # can keep an already-expired lease alive indefinitely.
    expire_claims(session)
    existing = (
        session.execute(
            select(JobBatch)
            .where(JobBatch.request_id == request_id)
            .options(joinedload(JobBatch.operations).joinedload(QueueOperation.document))
        )
        .unique()
        .scalar_one_or_none()
    )
    if existing is not None:
        if existing.state == BatchState.EXPIRED:
            raise LifecycleError(
                "This request ID belongs to an expired batch; start a new claim request.",
                code="batch-request-expired",
            )
        return BatchClaim(existing, idempotent_replay=True)
    operations = list(
        session.scalars(
            select(QueueOperation)
            .where(QueueOperation.state == OperationState.QUEUED)
            .order_by(QueueOperation.created_at, QueueOperation.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .options(joinedload(QueueOperation.document))
        ).all()
    )
    claimed_at = utc_now()
    if not operations:
        batch = JobBatch(
            request_id=request_id,
            state=BatchState.EMPTY,
            operation_count=0,
            claimed_at=claimed_at,
            lease_expires_at=claimed_at,
            completed_at=claimed_at,
        )
        session.add(batch)
        session.flush()
        return BatchClaim(batch)

    batch = JobBatch(
        request_id=request_id,
        state=BatchState.CLAIMED,
        operation_count=len(operations),
        claimed_at=claimed_at,
        lease_expires_at=claimed_at + timedelta(minutes=lease_minutes),
    )
    session.add(batch)
    session.flush()
    for operation in operations:
        operation.batch = batch
        operation.state = OperationState.CLAIMED
        operation.claimed_at = claimed_at
        operation.lease_expires_at = batch.lease_expires_at
        operation.document.state = (
            DocumentState.CLAIMED
            if operation.operation_type == OperationType.INGEST
            else DocumentState.DELETE_CLAIMED
        )
        _audit(
            session,
            event_type="operation_claimed",
            actor_type="service",
            actor_id=actor_id,
            document=operation.document,
            operation=operation,
            batch=batch,
            details={"status": operation.document.state.value},
        )
    session.flush()
    return BatchClaim(batch)


def acknowledge_staged_batch(
    session: Session, *, batch_id: uuid.UUID, operation_ids: list[uuid.UUID]
) -> BatchStage:
    batch = (
        session.execute(
            select(JobBatch)
            .where(JobBatch.id == batch_id)
            .options(joinedload(JobBatch.operations).joinedload(QueueOperation.document))
        )
        .unique()
        .scalar_one_or_none()
    )
    if batch is None:
        raise LifecycleError("Batch was not found.", code="batch-not-found", status=404)
    expected = {operation.id for operation in batch.operations}
    supplied = set(operation_ids)
    if supplied != expected or len(supplied) != len(operation_ids):
        raise LifecycleError(
            "The staged acknowledgement must contain every batch operation exactly once.",
            code="batch-operation-mismatch",
            status=422,
        )
    if batch.state in {
        BatchState.STAGED,
        BatchState.COMPLETED,
        BatchState.PARTIAL,
        BatchState.FAILED,
    }:
        return BatchStage(batch, idempotent_replay=True)
    if batch.state != BatchState.CLAIMED:
        raise LifecycleError(
            "Batch cannot be staged in its current state.", code="batch-not-claimable"
        )
    if _as_utc(batch.lease_expires_at) <= utc_now():
        expire_claims(session)
        raise LifecycleError(
            "The batch claim lease expired before staging.", code="batch-lease-expired"
        )

    staged_at = utc_now()
    for operation in batch.operations:
        if operation.state != OperationState.CLAIMED:
            raise LifecycleError(
                "A batch operation is no longer claimed.", code="batch-inconsistent"
            )
        operation.state = OperationState.STAGED
        operation.staged_at = staged_at
        if operation.operation_type == OperationType.INGEST:
            operation.document.state = DocumentState.STAGED
        _audit(
            session,
            event_type="operation_staged",
            actor_type="service",
            actor_id="jenkins",
            document=operation.document,
            operation=operation,
            batch=batch,
            details={"status": operation.document.state.value},
        )
    batch.state = BatchState.STAGED
    batch.staged_at = staged_at
    session.flush()
    return BatchStage(batch)


def _component_rows(result: Any) -> list[dict[str, Any]]:
    components = result.components.model_dump(mode="json")
    return [{"name": name, "status": status} for name, status in components.items()]


def report_batch_results(
    session: Session, *, batch_id: uuid.UUID, request: BatchResultsRequest
) -> BatchReport:
    batch = (
        session.execute(
            select(JobBatch)
            .where(JobBatch.id == batch_id)
            .options(joinedload(JobBatch.operations).joinedload(QueueOperation.document))
        )
        .unique()
        .scalar_one_or_none()
    )
    if batch is None:
        raise LifecycleError("Batch was not found.", code="batch-not-found", status=404)
    expected = {operation.id for operation in batch.operations}
    supplied = {result.operation_id for result in request.results}
    if supplied != expected or len(supplied) != len(request.results):
        raise LifecycleError(
            "Results must contain every batch operation exactly once.",
            code="batch-result-mismatch",
            status=422,
        )
    if batch.state in {BatchState.COMPLETED, BatchState.PARTIAL, BatchState.FAILED}:
        if batch.pipeline_run_id != request.pipeline_run_id:
            raise LifecycleError(
                "This batch was already reported by a different pipeline run.",
                code="batch-already-reported",
            )
        by_id = {operation.id: operation for operation in batch.operations}
        for result in request.results:
            operation = by_id[result.operation_id]
            component_values = result.components.model_dump(mode="json").values()
            effective_success = result.success and all(
                value == "succeeded" for value in component_values
            )
            expected_state = (
                OperationState.SUCCEEDED if effective_success else OperationState.FAILED
            )
            expected_error = (
                None
                if effective_success
                else result.error or "Not every required downstream component succeeded."
            )
            if (
                operation.state != expected_state
                or operation.chunk_count != result.chunk_count
                or operation.component_results != _component_rows(result)
                or operation.error != expected_error
            ):
                raise LifecycleError(
                    "This batch was already reported with a different result payload.",
                    code="batch-result-conflict",
                )
        succeeded = sum(op.state == OperationState.SUCCEEDED for op in batch.operations)
        cleanup_items = tuple(
            DeletionCleanup(operation.document.id, operation.document.storage_key)
            for operation in batch.operations
            if operation.document.state == DocumentState.DELETE_CLEANUP
            and operation.document.storage_key
        )
        return BatchReport(
            batch,
            succeeded=succeeded,
            failed=len(batch.operations) - succeeded,
            cleanup_items=cleanup_items,
            idempotent_replay=True,
        )
    if batch.state != BatchState.STAGED:
        raise LifecycleError("Only a staged batch can report results.", code="batch-not-staged")

    by_id = {operation.id: operation for operation in batch.operations}
    succeeded = 0
    failed = 0
    cleanup_items: list[DeletionCleanup] = []
    completed_at = utc_now()
    for result in request.results:
        operation = by_id[result.operation_id]
        document = operation.document
        component_rows = _component_rows(result)
        component_values = result.components.model_dump(mode="json").values()
        success = result.success and all(value == "succeeded" for value in component_values)

        operation.pipeline_run_id = request.pipeline_run_id
        operation.chunk_count = result.chunk_count
        operation.component_results = component_rows
        operation.completed_at = completed_at
        if success:
            operation.state = OperationState.SUCCEEDED
            operation.error = None
            document.last_error = None
            document.pipeline_run_id = request.pipeline_run_id
            if operation.operation_type == OperationType.INGEST:
                document.state = DocumentState.INGESTED
                document.ingested_at = completed_at
                document.chunk_count = result.chunk_count
                document.pipeline_metadata = {"components": component_rows}
                event_type = "ingestion_succeeded"
            else:
                document.state = DocumentState.DELETE_CLEANUP
                if not document.storage_key:
                    raise LifecycleError(
                        "The canonical PDF storage key is missing for this deletion.",
                        code="catalog-inconsistent",
                        status=500,
                    )
                cleanup_items.append(DeletionCleanup(document.id, document.storage_key))
                event_type = "deletion_cleanup_pending"
            succeeded += 1
        else:
            message = result.error or "Not every required downstream component succeeded."
            operation.state = OperationState.FAILED
            operation.error = message[:4000]
            document.last_error = message[:4000]
            document.state = (
                DocumentState.INGEST_FAILED
                if operation.operation_type == OperationType.INGEST
                else DocumentState.DELETE_FAILED
            )
            event_type = (
                "ingestion_failed"
                if operation.operation_type == OperationType.INGEST
                else "deletion_failed"
            )
            failed += 1
        _audit(
            session,
            event_type=event_type,
            actor_type="service",
            actor_id="jenkins",
            document=document,
            operation=operation,
            batch=batch,
            details={
                "status": document.state.value,
                "pipeline_run_id": request.pipeline_run_id,
                "detail": operation.error,
            },
        )

    batch.completed_at = completed_at
    batch.pipeline_run_id = request.pipeline_run_id
    batch.state = (
        BatchState.COMPLETED
        if failed == 0
        else BatchState.FAILED
        if succeeded == 0
        else BatchState.PARTIAL
    )
    session.flush()
    return BatchReport(batch, succeeded, failed, tuple(cleanup_items))


def finalize_deleted_storage(
    session: Session, *, document_id: uuid.UUID, storage_key: str
) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise LifecycleError("Document was not found.", code="document-not-found", status=404)
    if document.state == DocumentState.DELETED and document.storage_key is None:
        return document
    if document.state != DocumentState.DELETE_CLEANUP or document.storage_key != storage_key:
        raise LifecycleError(
            "Deletion cleanup no longer matches the catalog record.",
            code="cleanup-state-conflict",
        )
    document.storage_key = None
    document.state = DocumentState.DELETED
    document.deleted_at = utc_now()
    _audit(
        session,
        event_type="deletion_succeeded",
        actor_type="system",
        actor_id="canonical-storage-cleanup",
        document=document,
        details={"status": DocumentState.DELETED.value},
    )
    session.flush()
    return document


def can_serve_content(document: Document) -> bool:
    return (
        document.scan_state == ScanState.CLEAN
        and document.storage_key is not None
        and document.state
        not in {
            DocumentState.INGEST_FAILED,
            DocumentState.DELETE_FAILED,
            DocumentState.DELETE_CLEANUP,
            DocumentState.DELETED,
            DocumentState.CANCEL_CLEANUP,
            DocumentState.CANCELLED,
        }
    )


def import_historical_manifest(
    session: Session,
    *,
    manifest_path: Path,
    source_root: Path,
    layout: StorageLayout,
    scanner: Scanner,
    max_bytes: int,
    dry_run: bool,
    actor_id: str,
) -> HistoricalImportResponse:
    manifest = HistoricalImportManifest.model_validate_json(manifest_path.read_bytes())
    results: list[HistoricalImportItemResult] = []
    promoted: list[PromotedFile] = []
    seen_sources: set[Path] = set()
    seen_checksums: set[str] = set()
    try:
        for entry in manifest.documents:
            source = validate_source_path(source_root, entry.path)
            if source in seen_sources:
                raise LifecycleError(
                    "The historical manifest contains the same source path more than once.",
                    code="duplicate-manifest-entry",
                    status=422,
                )
            seen_sources.add(source)
            filename = validate_pdf_filename(entry.filename or source.name)
            staged = copy_source_to_temporary(source, layout, max_bytes=max_bytes)
            try:
                if staged.sha256 in seen_checksums:
                    raise LifecycleError(
                        "The historical manifest contains duplicate PDF contents.",
                        code="duplicate-manifest-content",
                        status=422,
                    )
                seen_checksums.add(staged.sha256)
                duplicate = find_active_checksum_duplicate(session, staged.sha256)
                if duplicate is not None:
                    raise DuplicateDocumentError(duplicate)
                scan_result = scanner(staged.path)
                if scan_result.state != ScanState.CLEAN:
                    raise LifecycleError(
                        "Historical source did not pass malware scanning.",
                        code="scan-not-clean",
                        status=422,
                    )
                if dry_run:
                    results.append(
                        HistoricalImportItemResult(
                            filename=filename,
                            sha256=staged.sha256,
                            size_bytes=staged.size_bytes,
                        )
                    )
                    continue

                document = Document(
                    id=uuid.uuid4(),
                    original_filename=filename,
                    normalized_filename=normalize_filename(filename),
                    size_bytes=staged.size_bytes,
                    sha256=staged.sha256,
                    idempotency_key=f"import:{uuid.uuid4()}",
                    state=DocumentState.INGESTED,
                    scan_state=ScanState.CLEAN,
                    scan_engine=scan_result.engine,
                    scan_signature=scan_result.signature,
                    scanned_at=scan_result.scanned_at,
                    uploader_identity=actor_id,
                    ingested_at=entry.ingested_at or utc_now(),
                    chunk_count=entry.chunk_count,
                    pipeline_run_id=entry.pipeline_run_id,
                )
                promoted_file = promote_staged_file(staged, layout, document.id)
                promoted.append(promoted_file)
                document.storage_key = promoted_file.storage_key
                operation = QueueOperation(
                    document=document,
                    operation_type=OperationType.INGEST,
                    state=OperationState.SUCCEEDED,
                    attempt=1,
                    completed_at=document.ingested_at,
                    pipeline_run_id=entry.pipeline_run_id,
                    chunk_count=entry.chunk_count,
                )
                session.add_all([document, operation])
                session.flush()
                _audit(
                    session,
                    event_type="historical_document_imported",
                    actor_type="operator",
                    actor_id=actor_id,
                    document=document,
                    operation=operation,
                    details={"status": DocumentState.INGESTED.value},
                )
                results.append(
                    HistoricalImportItemResult(
                        filename=filename,
                        sha256=staged.sha256,
                        size_bytes=staged.size_bytes,
                        document_id=document.id,
                    )
                )
            finally:
                staged.path.unlink(missing_ok=True)
    except Exception:
        for promoted_file in promoted:
            promoted_file.path.unlink(missing_ok=True)
        raise
    return HistoricalImportResponse(
        dry_run=dry_run, imported=0 if dry_run else len(results), items=results
    )
