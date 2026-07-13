"""Least-privilege Jenkins batch HTTP controller."""

from __future__ import annotations

import logging
from uuid import UUID

from litestar import Request, Response, Router, get, post
from litestar.di import NamedDependency, Provide
from litestar.openapi.datastructures import ResponseSpec
from litestar.params import FromPath, JSONBody
from litestar.response import File
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    BatchClaimRequest,
    BatchClaimResponse,
    BatchManifestResponse,
    BatchResultsRequest,
    BatchResultsResponse,
    BatchStageRequest,
    BatchStageResponse,
)
from pdf_bridge.http.problems import ProblemError, problem_responses
from pdf_bridge.http.security import Actor, require_job_token
from pdf_bridge.managers.batch import (
    StorageCleanupError,
    batch_manifest,
    batch_operation_content,
    claim_batch_request,
    report_batch_request,
    stage_batch_request,
)
from pdf_bridge.persistence.models import BatchState
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.lifecycle import LifecycleError
from pdf_bridge.services.storage import remove_storage_key

logger = logging.getLogger(__name__)
_PROBLEM_RESPONSES = problem_responses()


def _problem(exc: LifecycleError) -> ProblemError:
    return ProblemError(
        status=exc.status,
        code=exc.code,
        title="Batch operation was rejected",
        detail=str(exc),
    )


def _service_problem(exc: ServiceError) -> ProblemError:
    return ProblemError(
        status=exc.status,
        code=exc.code,
        title=exc.title,
        detail=str(exc),
        extra=exc.extra,
    )


@post(
    "/batches/claim",
    status_code=200,
    responses={
        **_PROBLEM_RESPONSES,
        204: ResponseSpec(
            data_container=None,
            description="No queued operations for this request ID",
        ),
    },
    sync_to_thread=True,
)
def claim_job_batch(
    request: Request,
    data: JSONBody[BatchClaimRequest],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> BatchClaimResponse | Response[None]:
    """Lease queued operations to an idempotent Jenkins batch request."""

    try:
        result = claim_batch_request(
            db,
            request.app.state.transition_lock,
            request_id=data.request_id,
            limit=data.limit,
            lease_minutes=request.app.state.settings.claim_lease_minutes,
            actor_id=actor.identifier,
        )
    except LifecycleError as exc:
        raise _problem(exc) from exc

    batch = result.batch
    if batch.state == BatchState.EMPTY:
        return Response(content=None, status_code=204)
    return BatchClaimResponse(
        batch_id=batch.id,
        request_id=batch.request_id,
        state=batch.state,
        claimed_at=batch.claimed_at,
        lease_expires_at=batch.lease_expires_at,
        operation_count=batch.operation_count,
        idempotent_replay=result.idempotent_replay,
    )


@get(
    "/batches/{batch_id:uuid}/manifest",
    responses=_PROBLEM_RESPONSES,
    sync_to_thread=True,
)
def get_batch_manifest(
    request: Request,
    batch_id: FromPath[UUID],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> BatchManifestResponse:
    """Return the validated staging manifest for a claimed batch."""

    configured_collection_keys = {
        collection.key for collection in request.app.state.settings.collections
    }
    try:
        return batch_manifest(
            db,
            request.app.state.transition_lock,
            batch_id=batch_id,
            configured_collection_keys=configured_collection_keys,
        )
    except ServiceError as exc:
        raise _service_problem(exc) from exc


@get(
    "/batches/{batch_id:uuid}/operations/{operation_id:uuid}/content",
    media_type="application/pdf",
    responses=_PROBLEM_RESPONSES,
    sync_to_thread=True,
)
def download_batch_operation(
    request: Request,
    batch_id: FromPath[UUID],
    operation_id: FromPath[UUID],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> File:
    """Download canonical PDF content for an operation in the claimed batch."""

    try:
        content = batch_operation_content(
            db,
            request.app.state.transition_lock,
            batch_id=batch_id,
            operation_id=operation_id,
            storage_root=request.app.state.settings.storage_root,
        )
    except ServiceError as exc:
        raise _service_problem(exc) from exc
    return File(
        content.path,
        media_type="application/pdf",
        filename=content.filename,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@post(
    "/batches/{batch_id:uuid}/staged",
    status_code=200,
    responses=_PROBLEM_RESPONSES,
    sync_to_thread=True,
)
def stage_job_batch(
    request: Request,
    batch_id: FromPath[UUID],
    data: JSONBody[BatchStageRequest],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> BatchStageResponse:
    """Acknowledge that every operation in a batch was durably staged."""

    try:
        result = stage_batch_request(
            db,
            request.app.state.transition_lock,
            batch_id=batch_id,
            request=data,
        )
    except LifecycleError as exc:
        raise _problem(exc) from exc

    batch = result.batch
    return BatchStageResponse(
        batch_id=batch.id,
        state=batch.state,
        staged_at=batch.staged_at,
        operation_count=batch.operation_count,
        idempotent_replay=result.idempotent_replay,
    )


@post(
    "/batches/{batch_id:uuid}/results",
    status_code=200,
    responses=_PROBLEM_RESPONSES,
    sync_to_thread=True,
)
def report_job_batch(
    request: Request,
    batch_id: FromPath[UUID],
    data: JSONBody[BatchResultsRequest],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> BatchResultsResponse:
    """Record correlated pipeline results and finalize the batch."""

    try:
        result = report_batch_request(
            db,
            request.app.state.transition_lock,
            batch_id=batch_id,
            request=data,
            storage_root=request.app.state.settings.storage_root,
            # Pass cleanup explicitly so orchestration can retry after storage failures.
            remove_storage=remove_storage_key,
        )
    except LifecycleError as exc:
        raise _problem(exc) from exc
    except ServiceError as exc:
        raise _service_problem(exc) from exc
    except StorageCleanupError as exc:
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

    return BatchResultsResponse(
        batch_id=result.batch.id,
        state=result.batch.state,
        completed_at=result.batch.completed_at,
        succeeded=result.succeeded,
        failed=result.failed,
        idempotent_replay=result.idempotent_replay,
    )


jobs_router = Router(
    path="/api/v1/jobs",
    route_handlers=[
        claim_job_batch,
        get_batch_manifest,
        download_batch_operation,
        stage_job_batch,
        report_job_batch,
    ],
    dependencies={"actor": Provide(require_job_token, sync_to_thread=False)},
    tags=["Jenkins"],
)
router = jobs_router
