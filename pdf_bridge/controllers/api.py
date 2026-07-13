"""Human-facing typed JSON HTTP controller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from litestar import Request, Router, delete, get, post
from litestar.datastructures import UploadFile
from litestar.di import NamedDependency, Provide
from litestar.enums import RequestEncodingType
from litestar.openapi.datastructures import ResponseSpec
from litestar.openapi.spec import Operation, RequestBody
from litestar.params import (
    Body,
    FromPath,
    FromQuery,
    HeaderParameter,
    JSONBody,
    MultipartBody,
    QueryParameter,
)
from litestar.response import File, Response
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    CollectionListResponse,
    DeleteDocumentRequest,
    DocumentDetail,
    DocumentListResponse,
    DocumentMutationResponse,
    HealthResponse,
    QueueListResponse,
    SearchRequest,
    SearchResponse,
    UploadPreflightRequest,
    UploadPreflightResponse,
    UploadResponse,
)
from pdf_bridge.http.problems import ProblemError, problem_responses
from pdf_bridge.http.security import Actor, get_actor, require_csrf
from pdf_bridge.managers import catalog, document, health, search
from pdf_bridge.persistence.models import DocumentState
from pdf_bridge.services.document import duplicate_error_extra
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.lifecycle import LifecycleError
from pdf_bridge.services.scanner import ScannerError, clamd_ping
from pdf_bridge.services.storage import (
    FileTooLargeError,
    InvalidPdfError,
    remove_storage_key,
)


@dataclass(slots=True)
class UploadForm:
    """Multipart upload contract with the public form field names."""

    file: UploadFile
    collection_key: str
    possible_duplicate_confirmed: bool = False
    idempotency_key: str | None = None


@dataclass
class OptionalRequestBodyOperation(Operation):
    """Correct Litestar 2.24's always-required OpenAPI request-body flag."""

    def __post_init__(self) -> None:
        if isinstance(self.request_body, RequestBody):
            self.request_body.required = False


_BROWSER_ACTOR_DEPENDENCIES = {"_actor": Provide(get_actor, sync_to_thread=False)}
_CSRF_ACTOR_DEPENDENCIES = {"actor": Provide(require_csrf)}
_CSRF_CHECK_DEPENDENCIES = {"_actor": Provide(require_csrf)}


def _lifecycle_problem(exc: LifecycleError) -> ProblemError:
    return ProblemError(
        status=exc.status,
        code=exc.code,
        title="Document operation was rejected",
        detail=str(exc),
        extra=duplicate_error_extra(exc),
    )


def _service_problem(exc: ServiceError) -> ProblemError:
    return ProblemError(
        status=exc.status,
        code=exc.code,
        title=exc.title,
        detail=str(exc),
        extra=exc.extra,
    )


@get(
    "/collections",
    dependencies=_BROWSER_ACTOR_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def list_collections(
    request: Request,
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> CollectionListResponse:
    """List configured collections with live catalog counts."""

    return catalog.list_collections(
        db,
        request.app.state.settings.collections,
    )


@get(
    "/documents",
    dependencies=_BROWSER_ACTOR_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def list_documents(
    request: Request,
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    document_scope: Annotated[
        Literal["library", "queue", "all"], QueryParameter(name="scope")
    ] = "all",
    document_state: Annotated[DocumentState | None, QueryParameter(name="state")] = None,
    collection_key: FromQuery[str | None] = None,
    page: Annotated[int, QueryParameter(ge=1)] = 1,
    page_size: Annotated[int, QueryParameter(ge=1, le=100)] = 25,
) -> DocumentListResponse:
    """List documents filtered by lifecycle scope and collection."""

    try:
        return catalog.list_documents(
            db,
            definitions=request.app.state.settings.collections,
            document_scope=document_scope,
            document_state=document_state,
            collection_key=collection_key,
            page=page,
            page_size=page_size,
        )
    except ServiceError as exc:
        raise _service_problem(exc) from exc


@get(
    "/documents/{document_id:uuid}",
    dependencies=_BROWSER_ACTOR_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def get_document(
    document_id: FromPath[UUID],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> DocumentDetail:
    """Return one document with its operation and audit history."""

    try:
        return catalog.get_document(db, document_id)
    except ServiceError as exc:
        raise _service_problem(exc) from exc


@get(
    "/documents/{document_id:uuid}/content",
    dependencies=_BROWSER_ACTOR_DEPENDENCIES,
    media_type="application/pdf",
    responses=problem_responses(),
    sync_to_thread=True,
)
def document_content(
    request: Request,
    document_id: FromPath[UUID],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> File:
    """Serve a clean, available PDF inline from canonical storage."""

    try:
        result = document.content(
            db,
            document_id=document_id,
            storage_root=request.app.state.settings.storage_root,
        )
    except ServiceError as exc:
        raise _service_problem(exc) from exc
    return File(
        result.path,
        media_type="application/pdf",
        filename=result.filename,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'self'",
        },
    )


@post(
    "/uploads/preflight",
    dependencies=_CSRF_CHECK_DEPENDENCIES,
    status_code=200,
    responses=problem_responses(),
    sync_to_thread=True,
)
def upload_preflight(
    request: Request,
    data: JSONBody[UploadPreflightRequest],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> UploadPreflightResponse:
    """Validate upload metadata and surface possible duplicates."""

    try:
        return document.preflight_upload(
            db,
            definitions=request.app.state.settings.collections,
            filename=data.filename,
            size_bytes=data.size_bytes,
            collection_key=data.collection_key,
        )
    except ServiceError as exc:
        raise _service_problem(exc) from exc


@post(
    "/uploads",
    dependencies=_CSRF_ACTOR_DEPENDENCIES,
    status_code=201,
    responses=problem_responses(),
    sync_to_thread=True,
)
def upload_document(
    request: Request,
    data: MultipartBody[UploadForm],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    header_idempotency_key: Annotated[str | None, HeaderParameter(name="Idempotency-Key")] = None,
) -> UploadResponse:
    """Scan, register, and queue a multipart PDF upload."""

    try:
        # Litestar rewinds the spooled multipart part before the handler runs,
        # so its plain synchronous file object streams from the beginning.
        return document.upload_document(
            db,
            settings=request.app.state.settings,
            scanner=request.app.state.scanner,
            transition_lock=request.app.state.transition_lock,
            file=data.file.file,
            filename=data.file.filename or "",
            content_type=data.file.content_type,
            collection_key=data.collection_key,
            possible_duplicate_confirmed=data.possible_duplicate_confirmed,
            header_idempotency_key=header_idempotency_key,
            form_idempotency_key=data.idempotency_key,
            actor_type=actor.kind,
            actor_id=actor.identifier,
        )
    except (FileTooLargeError, InvalidPdfError) as exc:
        raise ProblemError(
            status=413 if isinstance(exc, FileTooLargeError) else 422,
            code="upload-too-large" if isinstance(exc, FileTooLargeError) else "invalid-pdf",
            title="PDF upload was rejected",
            detail=str(exc),
        ) from exc
    except ScannerError as exc:
        raise ProblemError(
            status=503,
            code="scanner-unavailable",
            title="Malware scan could not be completed",
            detail="The upload was not queued. Retry after ClamAV is healthy.",
        ) from exc
    except LifecycleError as exc:
        raise _lifecycle_problem(exc) from exc
    except ServiceError as exc:
        raise _service_problem(exc) from exc
    finally:
        data.file.file.close()


@get(
    "/queue",
    dependencies=_BROWSER_ACTOR_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def list_queue(
    request: Request,
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    collection_key: FromQuery[str | None] = None,
    page: Annotated[int, QueryParameter(ge=1)] = 1,
    page_size: Annotated[int, QueryParameter(ge=1, le=100)] = 25,
) -> QueueListResponse:
    """List current queue operations by collection."""

    try:
        return catalog.list_queue(
            db,
            definitions=request.app.state.settings.collections,
            collection_key=collection_key,
            page=page,
            page_size=page_size,
        )
    except ServiceError as exc:
        raise _service_problem(exc) from exc


@delete(
    "/queue/{operation_id:uuid}",
    dependencies=_CSRF_ACTOR_DEPENDENCIES,
    status_code=200,
    responses=problem_responses(),
    sync_to_thread=True,
)
def cancel_queue_item(
    request: Request,
    operation_id: FromPath[UUID],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> DocumentMutationResponse:
    """Cancel a queued operation and clean up its stored PDF."""

    try:
        return document.cancel_queue_item(
            db,
            transition_lock=request.app.state.transition_lock,
            storage_root=request.app.state.settings.storage_root,
            operation_id=operation_id,
            actor_type=actor.kind,
            actor_id=actor.identifier,
            remove_file=remove_storage_key,
        )
    except LifecycleError as exc:
        raise _lifecycle_problem(exc) from exc
    except ServiceError as exc:
        raise _service_problem(exc) from exc


@post(
    "/queue/{operation_id:uuid}/retry",
    dependencies=_CSRF_ACTOR_DEPENDENCIES,
    status_code=200,
    responses=problem_responses(),
    sync_to_thread=True,
)
def retry_queue_item(
    request: Request,
    operation_id: FromPath[UUID],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> DocumentMutationResponse:
    """Queue a new attempt for a retryable failed operation."""

    try:
        return document.retry_queue_item(
            db,
            transition_lock=request.app.state.transition_lock,
            operation_id=operation_id,
            actor_type=actor.kind,
            actor_id=actor.identifier,
        )
    except LifecycleError as exc:
        raise _lifecycle_problem(exc) from exc


@post(
    "/documents/{document_id:uuid}/deletion",
    dependencies=_CSRF_ACTOR_DEPENDENCIES,
    status_code=200,
    responses=problem_responses(),
    sync_to_thread=True,
    operation_class=OptionalRequestBodyOperation,
)
def request_document_deletion(
    request: Request,
    document_id: FromPath[UUID],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    data: Annotated[
        DeleteDocumentRequest | None,
        Body(media_type=RequestEncodingType.JSON),
    ] = None,
) -> DocumentMutationResponse:
    """Queue deletion of an eligible catalog document."""

    try:
        return document.request_deletion(
            db,
            transition_lock=request.app.state.transition_lock,
            document_id=document_id,
            actor_type=actor.kind,
            actor_id=actor.identifier,
            reason=data.reason if data else None,
        )
    except LifecycleError as exc:
        raise _lifecycle_problem(exc) from exc


@post(
    "/search",
    dependencies=_CSRF_CHECK_DEPENDENCIES,
    status_code=200,
    responses=problem_responses(),
    sync_to_thread=True,
)
def search_documents(
    request: Request,
    data: JSONBody[SearchRequest],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> SearchResponse:
    """Search configured collections through the external retrieval service.

    Bridge search is an operator workspace feature; chatbot authorization and
    answer generation happen outside PDF Bridge against retrieval directly.
    """

    try:
        return search.search_documents(
            db,
            settings=request.app.state.settings,
            definitions=request.app.state.settings.collections,
            request=data,
            client=request.app.state.search_http_client,
        )
    except ServiceError as exc:
        raise _service_problem(exc) from exc


@get(
    "/health/live",
    responses=problem_responses(),
    sync_to_thread=False,
)
def live() -> HealthResponse:
    """Report that the application process is running."""

    return HealthResponse(status="ok", checks={"process": "ok"})


def _dependency_checks(request: Request, db: Session) -> dict[str, str]:
    return health.check_dependencies(
        request.app.state.settings,
        db,
        scanner_probe=clamd_ping,
    )


def _health_response(checks: dict[str, str]) -> Response[HealthResponse]:
    healthy = all(value == "ok" for value in checks.values())
    body = HealthResponse(status="ok" if healthy else "degraded", checks=checks)
    return Response(
        content=body,
        status_code=200 if healthy else 503,
        headers={"Cache-Control": "no-store"},
    )


def _health_responses(description: str) -> dict[int, ResponseSpec]:
    return {
        **problem_responses(),
        503: ResponseSpec(
            data_container=HealthResponse,
            description=description,
            generate_examples=False,
        ),
    }


@get(
    "/health/ready",
    responses=_health_responses("A dependency is not ready"),
    sync_to_thread=True,
)
def ready(request: Request, db: NamedDependency[Session]) -> Response[HealthResponse]:
    """Report whether dependencies are ready to serve application traffic."""

    return _health_response(_dependency_checks(request, db))


@get(
    "/health/dependencies",
    responses=_health_responses("A dependency is unavailable"),
    sync_to_thread=True,
)
def dependencies(request: Request, db: NamedDependency[Session]) -> Response[HealthResponse]:
    """Report detailed availability for each required dependency."""

    return _health_response(_dependency_checks(request, db))


_API_ROUTE_HANDLERS = (
    list_collections,
    list_documents,
    get_document,
    document_content,
    upload_preflight,
    list_queue,
    cancel_queue_item,
    retry_queue_item,
    request_document_deletion,
    search_documents,
    live,
    ready,
    dependencies,
)


def create_api_routers(upload_request_max_body_size: int) -> list[Router]:
    """Build the API routers, isolating uploads behind their envelope limit."""

    if upload_request_max_body_size <= 0:
        raise ValueError("upload_request_max_body_size must be positive")
    return [
        Router(
            path="/api/v1",
            route_handlers=list(_API_ROUTE_HANDLERS),
            tags=["PDF Bridge"],
        ),
        Router(
            path="/api/v1",
            route_handlers=[upload_document],
            request_max_body_size=upload_request_max_body_size,
            tags=["PDF Bridge"],
        ),
    ]
