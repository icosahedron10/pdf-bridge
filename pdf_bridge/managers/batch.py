"""Thin transaction and cleanup orchestration for Jenkins batches."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    BatchManifestResponse,
    BatchResultsRequest,
    BatchStageRequest,
)
from pdf_bridge.services import job_batch
from pdf_bridge.services.job_batch import BatchContent
from pdf_bridge.services.lifecycle import (
    BatchClaim,
    BatchReport,
    BatchStage,
    LifecycleError,
)

TransitionLock = AbstractContextManager[object]
StorageRemover = Callable[..., None]


class StorageCleanupError(RuntimeError):
    """Canonical bytes could not be removed after results were committed."""


def claim_batch_request(
    session: Session,
    transition_lock: TransitionLock,
    *,
    request_id: str,
    limit: int,
    lease_minutes: int,
    actor_id: str,
) -> BatchClaim:
    """Claim queued work while preserving lease-expiry commits on rejection."""

    with transition_lock:
        try:
            result = job_batch.claim_batch_work(
                session,
                request_id=request_id,
                limit=limit,
                lease_minutes=lease_minutes,
                actor_id=actor_id,
            )
            session.commit()
        except LifecycleError as exc:
            if exc.code == "batch-request-expired":
                session.commit()
            else:
                session.rollback()
            raise
    return result


def batch_manifest(
    session: Session,
    transition_lock: TransitionLock,
    *,
    batch_id: UUID,
    configured_collection_keys: set[str],
) -> BatchManifestResponse:
    """Durably expire leases, load the batch, and build its manifest."""

    with transition_lock:
        job_batch.expire_batch_claims(session)
        session.commit()
        batch = job_batch.load_batch(session, batch_id)
    return job_batch.build_batch_manifest(
        batch,
        configured_collection_keys=configured_collection_keys,
    )


def batch_operation_content(
    session: Session,
    transition_lock: TransitionLock,
    *,
    batch_id: UUID,
    operation_id: UUID,
    storage_root: Path,
) -> BatchContent:
    """Durably expire leases before resolving downloadable operation content."""

    with transition_lock:
        job_batch.expire_batch_claims(session)
        session.commit()
        operation = job_batch.load_batch_operation(
            session,
            batch_id=batch_id,
            operation_id=operation_id,
        )
    return job_batch.resolve_batch_content(
        operation,
        storage_root=storage_root,
    )


def stage_batch_request(
    session: Session,
    transition_lock: TransitionLock,
    *,
    batch_id: UUID,
    request: BatchStageRequest,
) -> BatchStage:
    """Acknowledge a complete staged batch with the required transaction semantics."""

    with transition_lock:
        try:
            result = job_batch.acknowledge_batch_staging(
                session,
                batch_id=batch_id,
                request=request,
            )
            session.commit()
        except LifecycleError as exc:
            if exc.code == "batch-lease-expired":
                session.commit()
            else:
                session.rollback()
            raise
    return result


def report_batch_request(
    session: Session,
    transition_lock: TransitionLock,
    *,
    batch_id: UUID,
    request: BatchResultsRequest,
    storage_root: Path,
    remove_storage: StorageRemover,
) -> BatchReport:
    """Record results, remove canonical bytes, and finalize cleanup records."""

    with transition_lock:
        try:
            result = job_batch.record_batch_results(
                session,
                batch_id=batch_id,
                request=request,
            )
            session.commit()
        except LifecycleError:
            session.rollback()
            raise

    if not result.cleanup_items:
        return result

    layout = job_batch.cleanup_storage_layout(storage_root)
    for cleanup in result.cleanup_items:
        try:
            remove_storage(layout, cleanup.storage_key, missing_ok=True)
        except OSError as exc:
            raise StorageCleanupError("canonical storage cleanup failed") from exc
        with transition_lock:
            try:
                job_batch.finalize_deleted_content(
                    session,
                    document_id=cleanup.document_id,
                    storage_key=cleanup.storage_key,
                )
                session.commit()
            except LifecycleError:
                session.rollback()
                raise
    return result
