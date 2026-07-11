"""Least-privilege Jenkins batch API."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from pdf_bridge.db import get_db
from pdf_bridge.lifecycle import (
    LifecycleError,
    acknowledge_staged_batch,
    can_serve_content,
    claim_batch,
    expire_claims,
    finalize_deleted_storage,
    report_batch_results,
)
from pdf_bridge.models import BatchState, JobBatch, OperationState, OperationType, QueueOperation
from pdf_bridge.problems import ProblemError
from pdf_bridge.schemas import (
    BatchClaimRequest,
    BatchClaimResponse,
    BatchManifestItem,
    BatchManifestResponse,
    BatchResultsRequest,
    BatchResultsResponse,
    BatchStageRequest,
    BatchStageResponse,
    problem_responses,
)
from pdf_bridge.security import Actor, require_job_token
from pdf_bridge.storage import (
    StorageLayout,
    remove_storage_key,
    resolve_storage_key,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["Jenkins"], responses=problem_responses())


def _problem(exc: LifecycleError) -> ProblemError:
    return ProblemError(
        status=exc.status,
        code=exc.code,
        title="Batch operation was rejected",
        detail=str(exc),
    )


def _load_batch(db: Session, batch_id: UUID) -> JobBatch:
    batch = (
        db.execute(
            select(JobBatch)
            .where(JobBatch.id == batch_id)
            .options(joinedload(JobBatch.operations).joinedload(QueueOperation.document))
        )
        .unique()
        .scalar_one_or_none()
    )
    if batch is None:
        raise ProblemError(
            status=404,
            code="batch-not-found",
            title="Batch not found",
            detail="No job batch exists for this ID.",
        )
    return batch


def _manifest_metadata(
    operation: QueueOperation, *, configured_collection_keys: set[str]
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
        raise ProblemError(
            status=500,
            code="invalid-batch-document-metadata",
            title="Batch document metadata is invalid",
            detail="A queued document has no safe configured collection key.",
        )

    language = getattr(document.language, "value", document.language)
    if language not in {"und", "en", "fr"}:
        raise ProblemError(
            status=500,
            code="invalid-batch-document-metadata",
            title="Batch document metadata is invalid",
            detail="A queued document has an unsupported language value.",
        )

    classification_required = (
        operation.operation_type == OperationType.INGEST and language == "und"
    )
    relative_path = f"pdfs/{language}/{collection_key}/{document.id}.pdf"
    return collection_key, language, classification_required, relative_path


@router.post(
    "/batches/claim",
    response_model=BatchClaimResponse,
    responses={204: {"description": "No queued operations for this request ID"}},
)
def claim_job_batch(
    payload: BatchClaimRequest,
    request: Request,
    actor: Actor = Depends(require_job_token),
    db: Session = Depends(get_db),
):
    with request.app.state.transition_lock:
        try:
            result = claim_batch(
                db,
                request_id=payload.request_id,
                limit=payload.limit,
                lease_minutes=request.app.state.settings.claim_lease_minutes,
                actor_id=actor.identifier,
            )
            db.commit()
        except LifecycleError as exc:
            if exc.code == "batch-request-expired":
                # claim_batch expires leases before resolving idempotency. Keep
                # that requeue durable even though this request ID is rejected.
                db.commit()
            else:
                db.rollback()
            raise _problem(exc) from exc
    batch = result.batch
    if batch.state == BatchState.EMPTY:
        return Response(status_code=204)
    return BatchClaimResponse(
        batch_id=batch.id,
        request_id=batch.request_id,
        state=batch.state,
        claimed_at=batch.claimed_at,
        lease_expires_at=batch.lease_expires_at,
        operation_count=batch.operation_count,
        idempotent_replay=result.idempotent_replay,
    )


@router.get("/batches/{batch_id}/manifest", response_model=BatchManifestResponse)
def get_batch_manifest(
    request: Request,
    batch_id: UUID,
    _actor: Actor = Depends(require_job_token),
    db: Session = Depends(get_db),
) -> BatchManifestResponse:
    with request.app.state.transition_lock:
        expire_claims(db)
        db.commit()
        batch = _load_batch(db, batch_id)
    if batch.state == BatchState.EXPIRED:
        raise ProblemError(
            status=409,
            code="batch-lease-expired",
            title="Batch lease expired",
            detail="Claim this work again with a new request ID.",
        )
    configured_collection_keys = {
        collection.key for collection in request.app.state.settings.collections
    }
    operations = []
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


@router.get(
    "/batches/{batch_id}/operations/{operation_id}/content",
    response_class=FileResponse,
)
def download_batch_operation(
    request: Request,
    batch_id: UUID,
    operation_id: UUID,
    _actor: Actor = Depends(require_job_token),
    db: Session = Depends(get_db),
) -> FileResponse:
    with request.app.state.transition_lock:
        expire_claims(db)
        db.commit()
        operation = db.scalar(
            select(QueueOperation)
            .where(
                QueueOperation.id == operation_id,
                QueueOperation.batch_id == batch_id,
            )
            .options(joinedload(QueueOperation.document), joinedload(QueueOperation.batch))
        )
    if operation is None:
        raise ProblemError(
            status=404,
            code="batch-operation-not-found",
            title="Batch operation not found",
            detail="This operation does not belong to the requested batch.",
        )
    if operation.batch is not None and operation.batch.state == BatchState.EXPIRED:
        raise ProblemError(
            status=409,
            code="batch-lease-expired",
            title="Batch lease expired",
            detail="The expired batch can no longer download canonical PDFs.",
        )
    if operation.operation_type != OperationType.INGEST or operation.state not in {
        OperationState.CLAIMED,
        OperationState.STAGED,
    }:
        raise ProblemError(
            status=409,
            code="operation-content-unavailable",
            title="Operation content is not available",
            detail="Only claimed or staged ingestion operations contain downloadable PDFs.",
        )
    if not can_serve_content(operation.document):
        raise ProblemError(
            status=409,
            code="operation-content-unavailable",
            title="Operation content is not available",
            detail="The canonical PDF is missing or no longer eligible for export.",
        )
    layout = StorageLayout.from_root(request.app.state.settings.storage_root)
    path = resolve_storage_key(layout, operation.document.storage_key or "")
    if not path.is_file():
        raise ProblemError(
            status=500,
            code="stored-file-missing",
            title="Stored PDF is missing",
            detail="The catalog and canonical storage are inconsistent.",
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{operation.id}.pdf",
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/batches/{batch_id}/staged", response_model=BatchStageResponse)
def stage_job_batch(
    request: Request,
    batch_id: UUID,
    payload: BatchStageRequest,
    _actor: Actor = Depends(require_job_token),
    db: Session = Depends(get_db),
) -> BatchStageResponse:
    with request.app.state.transition_lock:
        try:
            result = acknowledge_staged_batch(
                db, batch_id=batch_id, operation_ids=payload.operation_ids
            )
            db.commit()
        except LifecycleError as exc:
            if exc.code == "batch-lease-expired":
                # No staging transition has begun; preserve the lease-expiry
                # requeue performed by acknowledge_staged_batch.
                db.commit()
            else:
                db.rollback()
            raise _problem(exc) from exc
    batch = result.batch
    return BatchStageResponse(
        batch_id=batch.id,
        state=batch.state,
        staged_at=batch.staged_at,
        operation_count=batch.operation_count,
        idempotent_replay=result.idempotent_replay,
    )


@router.post("/batches/{batch_id}/results", response_model=BatchResultsResponse)
def report_job_batch(
    request: Request,
    batch_id: UUID,
    payload: BatchResultsRequest,
    _actor: Actor = Depends(require_job_token),
    db: Session = Depends(get_db),
) -> BatchResultsResponse:
    with request.app.state.transition_lock:
        try:
            result = report_batch_results(db, batch_id=batch_id, request=payload)
            db.commit()
        except LifecycleError as exc:
            db.rollback()
            raise _problem(exc) from exc

    if result.cleanup_items:
        layout = StorageLayout.from_root(request.app.state.settings.storage_root)
        for cleanup in result.cleanup_items:
            try:
                remove_storage_key(layout, cleanup.storage_key, missing_ok=True)
            except OSError as exc:
                logger.exception(
                    "deleted document storage cleanup failed",
                    extra={"batch_id": str(batch_id), "outcome": "cleanup-failed"},
                )
                raise ProblemError(
                    status=500,
                    code="storage-cleanup-failed",
                    title="Pipeline results were recorded but cleanup is still pending",
                    detail="Replay the same result report after canonical storage is available.",
                ) from exc
            with request.app.state.transition_lock:
                try:
                    finalize_deleted_storage(
                        db,
                        document_id=cleanup.document_id,
                        storage_key=cleanup.storage_key,
                    )
                    db.commit()
                except LifecycleError as exc:
                    db.rollback()
                    raise _problem(exc) from exc
    return BatchResultsResponse(
        batch_id=result.batch.id,
        state=result.batch.state,
        completed_at=result.batch.completed_at,
        succeeded=result.succeeded,
        failed=result.failed,
        review_required=result.review_required,
        idempotent_replay=result.idempotent_replay,
    )
