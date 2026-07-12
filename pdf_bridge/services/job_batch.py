"""Batch queries, manifest construction, and canonical-content decisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from pdf_bridge.contracts.schemas import (
    BatchManifestItem,
    BatchManifestResponse,
    BatchResultsRequest,
    BatchStageRequest,
)
from pdf_bridge.persistence.models import (
    BatchState,
    JobBatch,
    OperationState,
    OperationType,
    QueueOperation,
)
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.lifecycle import (
    BatchClaim,
    BatchReport,
    BatchStage,
    acknowledge_staged_batch,
    can_serve_content,
    claim_batch,
    expire_claims,
    finalize_deleted_storage,
    report_batch_results,
)
from pdf_bridge.services.storage import (
    StorageLayout,
    resolve_storage_key,
)


@dataclass(frozen=True, slots=True)
class BatchContent:
    """Validated canonical content that the HTTP controller may return as a file."""

    path: Path
    filename: str


def claim_batch_work(
    session: Session,
    *,
    request_id: str,
    limit: int,
    lease_minutes: int,
    actor_id: str,
) -> BatchClaim:
    """Delegate the lifecycle transition for a Jenkins batch claim."""

    return claim_batch(
        session,
        request_id=request_id,
        limit=limit,
        lease_minutes=lease_minutes,
        actor_id=actor_id,
    )


def expire_batch_claims(session: Session) -> int:
    """Expire stale batch leases before a read or staging transition."""

    return expire_claims(session)


def load_batch(session: Session, batch_id: UUID) -> JobBatch:
    """Load a batch and the document metadata needed by its manifest."""

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
        raise ServiceError(
            "No job batch exists for this ID.",
            status=404,
            code="batch-not-found",
            title="Batch not found",
        )
    return batch


def _manifest_metadata(
    operation: QueueOperation,
    *,
    configured_collection_keys: set[str],
) -> tuple[str, str, bool, str]:
    document = operation.document
    collection_key = document.collection_key
    if (
        not collection_key
        or len(collection_key) > 63
        or not collection_key[0].isalnum()
        or not collection_key[0].isascii()
        or collection_key != collection_key.casefold()
        or collection_key not in configured_collection_keys
        or any(
            not (character.isascii() and (character.isalnum() or character in {"-", "_"}))
            for character in collection_key
        )
    ):
        raise ServiceError(
            "A queued document has no safe configured collection key.",
            status=500,
            code="invalid-batch-document-metadata",
            title="Batch document metadata is invalid",
        )

    language = getattr(document.language, "value", document.language)
    if language not in {"und", "en", "fr"}:
        raise ServiceError(
            "A queued document has an unsupported language value.",
            status=500,
            code="invalid-batch-document-metadata",
            title="Batch document metadata is invalid",
        )

    classification_required = (
        operation.operation_type == OperationType.INGEST and language == "und"
    )
    relative_path = f"pdfs/{language}/{collection_key}/{document.id}.pdf"
    return collection_key, language, classification_required, relative_path


def build_batch_manifest(
    batch: JobBatch,
    *,
    configured_collection_keys: set[str],
) -> BatchManifestResponse:
    """Build the immutable versioned handoff manifest for a claimed batch."""

    if batch.state == BatchState.EXPIRED:
        raise ServiceError(
            "Claim this work again with a new request ID.",
            status=409,
            code="batch-lease-expired",
            title="Batch lease expired",
        )

    operations: list[BatchManifestItem] = []
    for operation in batch.operations:
        collection_key, language, classification_required, relative_path = _manifest_metadata(
            operation,
            configured_collection_keys=configured_collection_keys,
        )
        operations.append(
            BatchManifestItem(
                operation_id=operation.id,
                document_id=operation.document_id,
                operation_type=operation.operation_type,
                filename=operation.document.original_filename,
                size_bytes=operation.document.size_bytes,
                sha256=operation.document.sha256,
                collection_key=collection_key,
                language=language,
                classification_required=classification_required,
                relative_path=relative_path,
                download_url=(
                    f"/api/v1/jobs/batches/{batch.id}/operations/{operation.id}/content"
                    if operation.operation_type == OperationType.INGEST
                    else None
                ),
            )
        )
    return BatchManifestResponse(
        version=batch.manifest_version,
        batch_id=batch.id,
        request_id=batch.request_id,
        state=batch.state,
        claimed_at=batch.claimed_at,
        lease_expires_at=batch.lease_expires_at,
        operations=operations,
    )


def load_batch_operation(
    session: Session,
    *,
    batch_id: UUID,
    operation_id: UUID,
) -> QueueOperation:
    """Load one operation only when it belongs to the requested batch."""

    operation = session.scalar(
        select(QueueOperation)
        .where(
            QueueOperation.id == operation_id,
            QueueOperation.batch_id == batch_id,
        )
        .options(joinedload(QueueOperation.document), joinedload(QueueOperation.batch))
    )
    if operation is None:
        raise ServiceError(
            "This operation does not belong to the requested batch.",
            status=404,
            code="batch-operation-not-found",
            title="Batch operation not found",
        )
    return operation


def resolve_batch_content(
    operation: QueueOperation,
    *,
    storage_root: Path,
) -> BatchContent:
    """Validate download eligibility and resolve the canonical PDF path."""

    if operation.batch is not None and operation.batch.state == BatchState.EXPIRED:
        raise ServiceError(
            "The expired batch can no longer download canonical PDFs.",
            status=409,
            code="batch-lease-expired",
            title="Batch lease expired",
        )
    if operation.operation_type != OperationType.INGEST or operation.state not in {
        OperationState.CLAIMED,
        OperationState.STAGED,
    }:
        raise ServiceError(
            "Only claimed or staged ingestion operations contain downloadable PDFs.",
            status=409,
            code="operation-content-unavailable",
            title="Operation content is not available",
        )
    if not can_serve_content(operation.document):
        raise ServiceError(
            "The canonical PDF is missing or no longer eligible for export.",
            status=409,
            code="operation-content-unavailable",
            title="Operation content is not available",
        )

    layout = StorageLayout.from_root(storage_root)
    path = resolve_storage_key(layout, operation.document.storage_key or "")
    if not path.is_file():
        raise ServiceError(
            "The catalog and canonical storage are inconsistent.",
            status=500,
            code="stored-file-missing",
            title="Stored PDF is missing",
        )
    return BatchContent(path=path, filename=f"{operation.id}.pdf")


def acknowledge_batch_staging(
    session: Session,
    *,
    batch_id: UUID,
    request: BatchStageRequest,
) -> BatchStage:
    """Delegate the exact-operation staging transition."""

    return acknowledge_staged_batch(
        session,
        batch_id=batch_id,
        operation_ids=request.operation_ids,
    )


def record_batch_results(
    session: Session,
    *,
    batch_id: UUID,
    request: BatchResultsRequest,
) -> BatchReport:
    """Delegate validation and persistence of pipeline outcomes."""

    return report_batch_results(session, batch_id=batch_id, request=request)


def cleanup_storage_layout(storage_root: Path) -> StorageLayout:
    """Resolve the canonical storage layout once for a cleanup batch."""

    return StorageLayout.from_root(storage_root)


def finalize_deleted_content(
    session: Session,
    *,
    document_id: UUID,
    storage_key: str,
) -> None:
    """Finalize catalog cleanup after canonical bytes have been removed."""

    finalize_deleted_storage(
        session,
        document_id=document_id,
        storage_key=storage_key,
    )
