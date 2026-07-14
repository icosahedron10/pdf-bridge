"""Strict JSON and content transport for the sole API v2 surface."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated

from litestar import Request, Router, delete, get, post
from litestar.datastructures import UploadFile
from litestar.di import NamedDependency, Provide
from litestar.enums import RequestEncodingType
from litestar.openapi.datastructures import ResponseSpec
from litestar.openapi.spec import Operation, RequestBody
from litestar.params import Body, FromPath, FromQuery, JSONBody, MultipartBody, QueryParameter
from litestar.response import File, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    ChunkListResponse,
    CollectionDetail,
    CollectionListResponse,
    CollectionPhysicalTarget,
    DecisionRequest,
    DocumentDetail,
    DocumentListResponse,
    EventListResponse,
    HealthResponse,
    HistoryResponse,
    MarkdownDocument,
    MutationResponse,
    NameCheckRequest,
    NameCheckResponse,
    OperationDetail,
    OperationMetricsResponse,
    OperatorSearchRequest,
    OperatorSearchResponse,
    PreflightResponse,
    RetryRequest,
    SanitizedFailure,
    UploadAcceptedResponse,
)
from pdf_bridge.http.problems import ProblemError, problem_responses
from pdf_bridge.http.security import (
    Actor,
    csrf_token,
    get_actor,
    require_csrf,
    require_idempotency_key,
)
from pdf_bridge.managers import catalog, document, health, search
from pdf_bridge.persistence.models import (
    Document,
    DocumentState,
    TerminalDisposition,
    WorkOperation,
)
from pdf_bridge.presentation.api_serializers import SerializationError
from pdf_bridge.services import document as document_service
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.intake import LifecycleError
from pdf_bridge.services.scanner import ScannerError, clamd_ping
from pdf_bridge.services.storage import (
    FileTooLargeError,
    InvalidFilenameError,
    InvalidPdfError,
    StorageError,
)
from pdf_bridge.services.vector_index import (
    INDEX_SCHEMA_VERSION,
    VectorIndexSchemaError,
    VectorIndexUnavailableError,
    validate_collection_schema,
)


class UploadForm(BaseModel):
    """The upload route accepts exactly one multipart field named ``file``."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    file: UploadFile


@dataclass
class OptionalRequestBodyOperation(Operation):
    """Mark an optional strict JSON body correctly in generated OpenAPI."""

    def __post_init__(self) -> None:
        if isinstance(self.request_body, RequestBody):
            self.request_body.required = False


_READ_DEPENDENCIES = {"_actor": Provide(get_actor, sync_to_thread=False)}
_CSRF_DEPENDENCIES = {"actor": Provide(require_csrf)}
_MUTATION_DEPENDENCIES = {
    "actor": Provide(require_csrf),
    "idempotency_key": Provide(require_idempotency_key, sync_to_thread=False),
}


def _problem(exc: Exception) -> ProblemError:
    if isinstance(exc, LifecycleError):
        existing_document_id = (
            exc.extra.get("existing_document_id")
            if exc.code == "exact_duplicate"
            else None
        )
        return ProblemError(
            status=exc.status,
            code=exc.code,
            detail=exc.message,
            retryable=exc.retryable,
            existing_document_id=existing_document_id,
        )
    if isinstance(exc, ServiceError):
        return ProblemError(
            status=exc.status,
            code=exc.code,
            detail=str(exc),
            retryable=exc.status == 503,
        )
    if isinstance(exc, FileTooLargeError):
        return ProblemError(
            status=413,
            code="upload_too_large",
            detail="The PDF exceeds the configured upload size limit.",
        )
    if isinstance(exc, InvalidPdfError):
        return ProblemError(
            status=422,
            code="invalid_pdf",
            detail="The uploaded file is not a valid PDF.",
        )
    if isinstance(exc, InvalidFilenameError):
        return ProblemError(
            status=422,
            code="invalid_filename",
            detail="The filename is not a valid PDF display name.",
        )
    if isinstance(exc, ScannerError):
        return ProblemError(
            status=503,
            code="scanner_unavailable",
            detail="Malware screening could not be completed.",
            retryable=True,
        )
    if isinstance(exc, StorageError):
        return ProblemError(
            status=500,
            code="storage_failure",
            detail="Canonical storage could not complete the request.",
        )
    if isinstance(exc, SerializationError):
        return ProblemError(
            status=500,
            code="catalog_serialization_failed",
            detail="Catalog data could not be represented safely.",
        )
    raise TypeError("unsupported deliberate transport failure")


_TRANSPORT_ERRORS = (
    LifecycleError,
    ServiceError,
    ScannerError,
    StorageError,
    SerializationError,
)


def _qdrant_client(request: Request):
    providers = getattr(request.app.state, "worker_providers", None)
    if providers is None:
        worker = getattr(request.app.state, "worker", None)
        providers = getattr(worker, "providers", None)
    return getattr(providers, "qdrant", None)


def _require_visible_document(
    request: Request,
    session: Session,
    document_id: uuid.UUID,
) -> None:
    document = session.get(Document, document_id)
    if document is None:
        raise LifecycleError("document_not_found", "The document was not found.", status=404)
    try:
        document_service.configured_collection(
            list(request.app.state.settings.collections), document.collection_key
        )
    except LifecycleError as exc:
        raise LifecycleError(
            "document_not_found", "The document was not found.", status=404
        ) from exc


def _require_visible_operation(
    request: Request,
    session: Session,
    operation_id: uuid.UUID,
) -> None:
    operation = session.get(WorkOperation, operation_id)
    if operation is not None:
        _require_visible_document(request, session, operation.document_id)


def _collection_target(request: Request, key: str) -> CollectionPhysicalTarget:
    definition = document_service.configured_collection(
        list(request.app.state.settings.collections), key
    )
    client = _qdrant_client(request)
    failure: SanitizedFailure | None = None
    compatible = False
    if client is None:
        failure = SanitizedFailure(
            code="qdrant_unavailable",
            message="The fixed Qdrant target is unavailable.",
            retryable=True,
        )
    else:
        try:
            validate_collection_schema(client, definition.qdrant_collection_name)
            compatible = True
        except VectorIndexSchemaError:
            failure = SanitizedFailure(
                code="qdrant_schema_incompatible",
                message="The fixed Qdrant target has an incompatible schema.",
                retryable=False,
            )
        except VectorIndexUnavailableError:
            failure = SanitizedFailure(
                code="qdrant_unavailable",
                message="The fixed Qdrant target is unavailable.",
                retryable=True,
            )
    return CollectionPhysicalTarget(
        qdrant_collection_name=definition.qdrant_collection_name,
        schema_version=INDEX_SCHEMA_VERSION,
        schema_compatible=compatible,
        failure=failure,
    )


def _worker_checks(
    request: Request,
) -> Mapping[str, tuple[bool, str | None]] | None:
    worker = getattr(request.app.state, "worker", None)
    checker = getattr(worker, "readiness_checks", None)
    if checker is None:
        return None
    try:
        result = checker()
    except Exception:
        return {"worker": (False, "worker_readiness_failed")}
    if not isinstance(result, Mapping):
        return {"worker": (False, "worker_readiness_invalid")}
    return result


def _health_response(result: HealthResponse) -> Response[HealthResponse]:
    return Response(
        content=result,
        status_code=200 if result.status == "OK" else 503,
        headers={"Cache-Control": "no-store"},
    )


def _authenticated_response(request: Request, content):
    """Attach the stable browser CSRF token to every authenticated GET."""

    return Response(
        content=content,
        headers={
            "Cache-Control": "private, no-store",
            "X-CSRF-Token": csrf_token(request),
        },
    )


def _health_responses() -> dict[int, ResponseSpec]:
    return {
        **problem_responses(),
        503: ResponseSpec(
            data_container=HealthResponse,
            description="A required dependency is not ready",
            generate_examples=False,
        ),
    }


@get("/health/live", responses=problem_responses(), sync_to_thread=False)
def live() -> HealthResponse:
    """Report process liveness without probing dependencies."""

    return health.live()


@get("/health/ready", responses=_health_responses(), sync_to_thread=True)
def ready(request: Request, db: NamedDependency[Session]) -> Response[HealthResponse]:
    """Report catalog, storage, scanner, worker, model, and Qdrant readiness."""

    result = health.ready(
        request.app.state.settings,
        db,
        scanner_probe=clamd_ping,
        provider_checks=_worker_checks(request),
    )
    return _health_response(result)


@get(
    "/collections",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def list_collections(
    request: Request,
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    cursor: FromQuery[str | None] = None,
    limit: Annotated[int, QueryParameter(ge=1, le=100)] = 50,
) -> Response[CollectionListResponse]:
    """Initialize the browser session and list configured logical collections."""

    try:
        result = catalog.list_collections(
            db,
            request.app.state.settings.collections,
            cursor=cursor,
            limit=limit,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@get(
    "/collections/{key:str}",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def get_collection(
    request: Request,
    key: FromPath[str],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> Response[CollectionDetail]:
    """Return configured metadata and read-only fixed-target status."""

    try:
        result = catalog.get_collection(
            db,
            request.app.state.settings.collections,
            key,
            target=_collection_target(request, key),
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@get(
    "/collections/{key:str}/documents",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def list_documents(
    request: Request,
    key: FromPath[str],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    document_state: Annotated[
        DocumentState | None, QueryParameter(name="state")
    ] = None,
    cursor: FromQuery[str | None] = None,
    limit: Annotated[int, QueryParameter(ge=1, le=100)] = 50,
) -> Response[DocumentListResponse]:
    """Return current nonterminal documents in one configured collection."""

    try:
        result = catalog.list_documents(
            db,
            definitions=request.app.state.settings.collections,
            collection_key=key,
            state=document_state,
            cursor=cursor,
            limit=limit,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@post(
    "/collections/{key:str}/name-check",
    dependencies=_CSRF_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def name_check(
    request: Request,
    key: FromPath[str],
    data: JSONBody[NameCheckRequest],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> NameCheckResponse:
    """Return a filename-only advisory before upload."""

    del actor
    try:
        return document.name_check(
            db,
            settings=request.app.state.settings,
            collection_key=key,
            filename=data.filename,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc


@post(
    "/collections/{key:str}/documents",
    dependencies=_MUTATION_DEPENDENCIES,
    status_code=202,
    responses=problem_responses(),
    sync_to_thread=True,
)
def upload_document(
    request: Request,
    key: FromPath[str],
    data: MultipartBody[UploadForm],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    idempotency_key: NamedDependency[str],
) -> UploadAcceptedResponse:
    """Stream, scan, durably admit, and enqueue exactly one PDF."""

    try:
        return document.upload_document(
            db,
            settings=request.app.state.settings,
            scanner=request.app.state.scanner,
            transition_lock=request.app.state.transition_lock,
            worker=getattr(request.app.state, "worker", None),
            file=data.file.file,
            filename=data.file.filename or "",
            content_type=data.file.content_type,
            collection_key=key,
            idempotency_key=idempotency_key,
            actor_type=actor.kind,
            actor_id=actor.identifier,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    finally:
        data.file.file.close()


@get(
    "/documents/{document_id:uuid}",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def get_document(
    request: Request,
    document_id: FromPath[uuid.UUID],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> Response[DocumentDetail]:
    """Return one composed immutable metadata and lifecycle view."""

    try:
        _require_visible_document(request, db, document_id)
        result = catalog.get_document(db, document_id)
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@get(
    "/documents/{document_id:uuid}/source",
    dependencies=_READ_DEPENDENCIES,
    media_type="application/pdf",
    responses=problem_responses(),
    sync_to_thread=True,
)
def get_source(
    request: Request,
    document_id: FromPath[uuid.UUID],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> File:
    """Serve a retained clean source PDF inline and without caching."""

    try:
        _require_visible_document(request, db, document_id)
        result = document_service.source_content(
            db,
            document_id=document_id,
            storage_root=request.app.state.settings.storage_root,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return File(
        path=result.path,
        media_type="application/pdf",
        filename=result.filename,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'self'",
            "X-Content-Type-Options": "nosniff",
            "X-CSRF-Token": csrf_token(request),
        },
    )


@get(
    "/documents/{document_id:uuid}/markdown",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def get_markdown(
    request: Request,
    document_id: FromPath[uuid.UUID],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> Response[MarkdownDocument]:
    """Return validated canonical Markdown and exact page provenance as JSON."""

    try:
        _require_visible_document(request, db, document_id)
        result = catalog.get_markdown(db, document_id)
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@get(
    "/documents/{document_id:uuid}/chunks",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def list_chunks(
    request: Request,
    document_id: FromPath[uuid.UUID],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    cursor: FromQuery[str | None] = None,
    limit: Annotated[int, QueryParameter(ge=1, le=100)] = 50,
) -> Response[ChunkListResponse]:
    """Return one revision-bound public chunk page without numeric vectors."""

    try:
        _require_visible_document(request, db, document_id)
        result = catalog.list_chunks(
            db,
            document_id=document_id,
            cursor=cursor,
            limit=limit,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@get(
    "/documents/{document_id:uuid}/preflight",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def get_preflight(
    request: Request,
    document_id: FromPath[uuid.UUID],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    cursor: FromQuery[str | None] = None,
    limit: Annotated[int, QueryParameter(ge=1, le=100)] = 25,
) -> Response[PreflightResponse]:
    """Return retained revision completeness and bounded candidate evidence."""

    try:
        _require_visible_document(request, db, document_id)
        result = catalog.get_preflight(
            db,
            document_id=document_id,
            cursor=cursor,
            limit=limit,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@post(
    "/documents/{document_id:uuid}/decision",
    dependencies=_MUTATION_DEPENDENCIES,
    status_code=202,
    responses=problem_responses(),
    sync_to_thread=True,
)
def decide_document(
    request: Request,
    document_id: FromPath[uuid.UUID],
    data: JSONBody[DecisionRequest],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    idempotency_key: NamedDependency[str],
) -> MutationResponse:
    """Record Keep, Replace, or Cancel against one exact revision."""

    try:
        return document.decide_document(
            db,
            settings=request.app.state.settings,
            transition_lock=request.app.state.transition_lock,
            worker=getattr(request.app.state, "worker", None),
            document_id=document_id,
            request=data,
            idempotency_key=idempotency_key,
            actor_type=actor.kind,
            actor_id=actor.identifier,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc


@post(
    "/documents/{document_id:uuid}/retry",
    dependencies=_MUTATION_DEPENDENCIES,
    status_code=202,
    responses=problem_responses(),
    sync_to_thread=True,
    operation_class=OptionalRequestBodyOperation,
)
def retry_document(
    request: Request,
    document_id: FromPath[uuid.UUID],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    idempotency_key: NamedDependency[str],
    data: Annotated[
        RetryRequest | None,
        Body(media_type=RequestEncodingType.JSON),
    ] = None,
) -> MutationResponse:
    """Resume the exact durable failed checkpoint without mutable input."""

    del data
    try:
        return document.retry_document(
            db,
            transition_lock=request.app.state.transition_lock,
            worker=getattr(request.app.state, "worker", None),
            document_id=document_id,
            idempotency_key=idempotency_key,
            actor_type=actor.kind,
            actor_id=actor.identifier,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc


@delete(
    "/documents/{document_id:uuid}",
    dependencies=_MUTATION_DEPENDENCIES,
    status_code=202,
    responses=problem_responses(),
    sync_to_thread=True,
)
def delete_document(
    request: Request,
    document_id: FromPath[uuid.UUID],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    idempotency_key: NamedDependency[str],
) -> MutationResponse:
    """Block reads and queue high-priority verified deletion."""

    try:
        return document.delete_document(
            db,
            settings=request.app.state.settings,
            transition_lock=request.app.state.transition_lock,
            worker=getattr(request.app.state, "worker", None),
            document_id=document_id,
            idempotency_key=idempotency_key,
            actor_type=actor.kind,
            actor_id=actor.identifier,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc


@get(
    "/documents/{document_id:uuid}/events",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def list_events(
    request: Request,
    document_id: FromPath[uuid.UUID],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    cursor: FromQuery[str | None] = None,
    limit: Annotated[int, QueryParameter(ge=1, le=100)] = 50,
) -> Response[EventListResponse]:
    """Return a content-free page of lifecycle audit events."""

    try:
        _require_visible_document(request, db, document_id)
        result = catalog.list_events(
            db,
            document_id=document_id,
            cursor=cursor,
            limit=limit,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@get(
    "/operations/metrics",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def get_operation_metrics(
    request: Request,
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> Response[OperationMetricsResponse]:
    """Return content-free queue depth, age, and durable phase aggregates."""

    result = catalog.operation_metrics(
        db, definitions=request.app.state.settings.collections
    )
    return _authenticated_response(request, result)


@get(
    "/operations/{operation_id:uuid}",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def get_operation(
    request: Request,
    operation_id: FromPath[uuid.UUID],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> Response[OperationDetail]:
    """Return queue position, phase age, attempt, and sanitized failure."""

    try:
        _require_visible_operation(request, db, operation_id)
        result = catalog.get_operation(db, operation_id)
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@get(
    "/history",
    dependencies=_READ_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def list_history(
    request: Request,
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    collection_key: FromQuery[str | None] = None,
    disposition: FromQuery[TerminalDisposition | None] = None,
    cursor: FromQuery[str | None] = None,
    limit: Annotated[int, QueryParameter(ge=1, le=100)] = 50,
) -> Response[HistoryResponse]:
    """Return terminal content-free tombstones."""

    try:
        result = catalog.list_history(
            db,
            definitions=request.app.state.settings.collections,
            collection_key=collection_key,
            disposition=disposition,
            cursor=cursor,
            limit=limit,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc
    return _authenticated_response(request, result)


@post(
    "/operator/search",
    dependencies=_CSRF_DEPENDENCIES,
    responses=problem_responses(),
    sync_to_thread=True,
)
def operator_search(
    request: Request,
    data: JSONBody[OperatorSearchRequest],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> OperatorSearchResponse:
    """Proxy one bounded operator-only query to the configured retrieval service."""

    del actor
    try:
        return search.search_documents(
            db,
            settings=request.app.state.settings,
            request=data,
            client=request.app.state.search_http_client,
        )
    except _TRANSPORT_ERRORS as exc:
        raise _problem(exc) from exc


_API_ROUTE_HANDLERS = (
    live,
    ready,
    list_collections,
    get_collection,
    list_documents,
    name_check,
    get_document,
    get_source,
    get_markdown,
    list_chunks,
    get_preflight,
    decide_document,
    retry_document,
    delete_document,
    list_events,
    get_operation_metrics,
    get_operation,
    list_history,
    operator_search,
)


def create_api_routers(upload_request_max_body_size: int) -> list[Router]:
    """Build the sole v2 router with a route-specific upload body limit."""

    if upload_request_max_body_size <= 0:
        raise ValueError("upload_request_max_body_size must be positive")
    upload_handler = copy.copy(upload_document)
    upload_handler.request_max_body_size = upload_request_max_body_size
    return [
        Router(
            path="/api/v2",
            route_handlers=[*_API_ROUTE_HANDLERS, upload_handler],
            tags=["PDF Bridge API v2"],
        )
    ]
