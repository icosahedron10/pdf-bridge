"""Human-facing typed JSON API."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from litestar import Request, Router, delete, get, post
from litestar.concurrency import sync_to_thread
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
from pydantic import RootModel, TypeAdapter, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from pdf_bridge.lifecycle import (
    ACTIVE_DOCUMENT_STATES,
    DuplicateDocumentError,
    LifecycleError,
    PossibleDuplicateError,
    can_serve_content,
    cancel_queued_document,
    finalize_cancelled_storage,
    find_preflight_duplicates,
    queue_classification_review,
    queue_document_deletion,
    register_staged_upload,
    retry_failed_document,
)
from pdf_bridge.models import (
    Document,
    DocumentState,
    LanguageCode,
    LanguageStatus,
    OperationType,
    QueueOperation,
)
from pdf_bridge.problems import ProblemError
from pdf_bridge.scanner import ScannerError, clamd_ping
from pdf_bridge.schemas import (
    ClassificationRequest,
    CollectionLanguageCounts,
    CollectionListResponse,
    CollectionSummary,
    DeleteDocumentRequest,
    DocumentDetail,
    DocumentListResponse,
    DocumentMutationResponse,
    DocumentSummary,
    DuplicateMatch,
    HealthResponse,
    IdempotencyKey,
    QueueListResponse,
    QueueOperationSummary,
    SearchRequest,
    SearchResponse,
    UploadPreflightRequest,
    UploadPreflightResponse,
    UploadResponse,
    problem_responses,
)
from pdf_bridge.search import search_retrieval
from pdf_bridge.security import Actor, get_actor, require_csrf
from pdf_bridge.storage import (
    FileTooLargeError,
    InvalidFilenameError,
    InvalidPdfError,
    StorageLayout,
    normalize_filename,
    remove_storage_key,
    resolve_storage_key,
    stream_upload,
    validate_pdf_filename,
)

logger = logging.getLogger(__name__)
idempotency_adapter = TypeAdapter(IdempotencyKey)


@dataclass(slots=True)
class UploadForm:
    """Multipart upload contract with the public form field names."""

    file: UploadFile
    collection_key: str
    possible_duplicate_confirmed: bool = False
    idempotency_key: str | None = None


class ClassificationBody(RootModel[ClassificationRequest]):
    """Single Pydantic body model around the discriminated request union."""


@dataclass
class OptionalRequestBodyOperation(Operation):
    """Correct Litestar 2.24's always-required OpenAPI request-body flag."""

    def __post_init__(self) -> None:
        if isinstance(self.request_body, RequestBody):
            self.request_body.required = False


_BROWSER_ACTOR_DEPENDENCIES = {"_actor": Provide(get_actor, sync_to_thread=False)}
_CSRF_ACTOR_DEPENDENCIES = {"actor": Provide(require_csrf)}
_CSRF_CHECK_DEPENDENCIES = {"_actor": Provide(require_csrf)}

LIBRARY_STATES = (
    DocumentState.INGESTED,
    DocumentState.DELETE_QUEUED,
    DocumentState.DELETE_CLAIMED,
    DocumentState.DELETE_CLEANUP,
    DocumentState.DELETE_FAILED,
)
RETRIEVAL_STATES = (
    DocumentState.INGESTED,
    DocumentState.DELETE_QUEUED,
    DocumentState.DELETE_CLAIMED,
    DocumentState.DELETE_FAILED,
)
RETRIEVAL_LANGUAGES = (LanguageCode.EN, LanguageCode.FR)
RETRIEVAL_LANGUAGE_STATUSES = (LanguageStatus.DETECTED, LanguageStatus.OVERRIDDEN)


def _retrieval_catalog_filters(
    *, collection_key: str | None = None, language: LanguageCode | None = None
) -> list:
    filters = [
        Document.state.in_(RETRIEVAL_STATES),
        Document.language.in_(RETRIEVAL_LANGUAGES),
        Document.language_status.in_(RETRIEVAL_LANGUAGE_STATUSES),
    ]
    if collection_key is not None:
        filters.append(Document.collection_key == collection_key)
    if language is not None:
        filters.append(Document.language == language)
    return filters


def _configured_collection(request: Request, collection_key: str):
    collection = next(
        (item for item in request.app.state.settings.collections if item.key == collection_key),
        None,
    )
    if collection is None:
        raise ProblemError(
            status=422,
            code="collection-not-configured",
            title="Collection was rejected",
            detail="Choose one of the collections configured for this PDF Bridge deployment.",
        )
    return collection


def _document_summary(document: Document) -> DocumentSummary:
    return DocumentSummary.model_validate(document).model_copy(
        update={"detail_url": f"/documents/{document.id}"}
    )


def _duplicate_match(document: Document) -> DuplicateMatch:
    return DuplicateMatch(
        document_id=document.id,
        filename=document.original_filename,
        size_bytes=document.size_bytes,
        state=document.state,
        collection_key=document.collection_key,
        language=document.language,
        detail_url=f"/documents/{document.id}",
    )


def _lifecycle_problem(exc: LifecycleError) -> ProblemError:
    extra = None
    if isinstance(exc, DuplicateDocumentError):
        extra = {"duplicate": _duplicate_match(exc.document).model_dump(mode="json")}
    elif isinstance(exc, PossibleDuplicateError):
        extra = {
            "possible_duplicates": [
                _duplicate_match(document).model_dump(mode="json") for document in exc.documents
            ]
        }
    return ProblemError(
        status=exc.status,
        code=exc.code,
        title="Document operation was rejected",
        detail=str(exc),
        extra=extra,
    )


def _get_document_detail(db: Session, document_id: UUID) -> Document:
    document = (
        db.execute(
            select(Document)
            .where(Document.id == document_id)
            .options(joinedload(Document.operations), joinedload(Document.audit_events))
        )
        .unique()
        .scalar_one_or_none()
    )
    if document is None:
        raise ProblemError(
            status=404,
            code="document-not-found",
            title="Document not found",
            detail="No catalog record exists for this document ID.",
        )
    return document


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
    processing_states = tuple(
        state
        for state in ACTIVE_DOCUMENT_STATES
        if state not in {DocumentState.INGESTED, DocumentState.CLASSIFICATION_REVIEW}
    )
    items: list[CollectionSummary] = []
    for definition in request.app.state.settings.collections:
        available = (
            db.scalar(
                select(func.count()).select_from(Document).where(
                    *_retrieval_catalog_filters(collection_key=definition.key),
                )
            )
            or 0
        )
        processing = (
            db.scalar(
                select(func.count()).select_from(Document).where(
                    Document.collection_key == definition.key,
                    Document.state.in_(processing_states),
                )
            )
            or 0
        )
        review = (
            db.scalar(
                select(func.count()).select_from(Document).where(
                    Document.collection_key == definition.key,
                    Document.state == DocumentState.CLASSIFICATION_REVIEW,
                )
            )
            or 0
        )
        language_counts = {
            language.value: (
                db.scalar(
                    select(func.count()).select_from(Document).where(
                        *_retrieval_catalog_filters(
                            collection_key=definition.key,
                            language=language,
                        ),
                    )
                )
                or 0
            )
            for language in LanguageCode
        }
        items.append(
            CollectionSummary(
                key=definition.key,
                display_name=definition.display_name,
                description=definition.description,
                audience=definition.audience,
                available_documents=available,
                processing_documents=processing,
                review_documents=review,
                languages=CollectionLanguageCounts(**language_counts),
                detail_url=f"/library/{definition.key}",
            )
        )
    return CollectionListResponse(items=items, total=len(items))


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
        Literal["library", "queue", "review", "all"], QueryParameter(name="scope")
    ] = "all",
    document_state: Annotated[
        DocumentState | None, QueryParameter(name="state")
    ] = None,
    collection_key: FromQuery[str | None] = None,
    language: FromQuery[LanguageCode | None] = None,
    page: Annotated[int, QueryParameter(ge=1)] = 1,
    page_size: Annotated[int, QueryParameter(ge=1, le=100)] = 25,
) -> DocumentListResponse:
    filters = []
    if collection_key is not None:
        _configured_collection(request, collection_key)
        filters.append(Document.collection_key == collection_key)
    if language is not None:
        filters.append(Document.language == language)
    if document_state is not None:
        filters.append(Document.state == document_state)
    elif document_scope == "library":
        filters.extend(_retrieval_catalog_filters())
    elif document_scope == "queue":
        filters.append(
            Document.state.in_(
                tuple(
                    item
                    for item in ACTIVE_DOCUMENT_STATES
                    if item not in {DocumentState.INGESTED, DocumentState.CLASSIFICATION_REVIEW}
                )
            )
        )
    elif document_scope == "review":
        filters.append(Document.state == DocumentState.CLASSIFICATION_REVIEW)
    total = db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0
    documents = db.scalars(
        select(Document)
        .where(*filters)
        .order_by(Document.uploaded_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return DocumentListResponse.create(
        [_document_summary(document) for document in documents],
        total=total,
        page=page,
        page_size=page_size,
    )


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
    document = _get_document_detail(db, document_id)
    return DocumentDetail.model_validate(document).model_copy(
        update={"detail_url": f"/documents/{document.id}"}
    )


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
    document = db.get(Document, document_id)
    if document is None:
        raise ProblemError(
            status=404,
            code="document-not-found",
            title="Document not found",
            detail="No catalog record exists for this document ID.",
        )
    if not can_serve_content(document):
        raise ProblemError(
            status=409,
            code="content-not-available",
            title="PDF content is not available",
            detail="Only retained PDFs with a clean malware scan can be opened.",
        )
    layout = StorageLayout.from_root(request.app.state.settings.storage_root)
    path = resolve_storage_key(layout, document.storage_key or "")
    if not path.is_file():
        raise ProblemError(
            status=500,
            code="stored-file-missing",
            title="Stored PDF is missing",
            detail="The catalog and canonical storage are inconsistent. Contact the operator.",
        )
    return File(
        path,
        media_type="application/pdf",
        filename=document.original_filename,
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
    _configured_collection(request, data.collection_key)
    try:
        normalized = normalize_filename(data.filename)
    except InvalidFilenameError as exc:
        raise ProblemError(
            status=422,
            code="invalid-filename",
            title="Filename was rejected",
            detail=str(exc),
        ) from exc
    matches = find_preflight_duplicates(
        db, normalized_filename=normalized, size_bytes=data.size_bytes
    )
    return UploadPreflightResponse(
        normalized_filename=normalized,
        requires_confirmation=bool(matches),
        possible_duplicates=[_duplicate_match(document) for document in matches],
    )


@post(
    "/uploads",
    dependencies=_CSRF_ACTOR_DEPENDENCIES,
    status_code=201,
    responses=problem_responses(),
)
async def upload_document(
    request: Request,
    data: MultipartBody[UploadForm],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
    header_idempotency_key: Annotated[
        str | None, HeaderParameter(name="Idempotency-Key")
    ] = None,
) -> UploadResponse:
    _configured_collection(request, data.collection_key)
    raw_key = header_idempotency_key or data.idempotency_key
    try:
        idempotency_key = idempotency_adapter.validate_python(raw_key)
    except ValidationError as exc:
        raise ProblemError(
            status=422,
            code="invalid-idempotency-key",
            title="Idempotency key was rejected",
            detail="Provide an 8–128 character Idempotency-Key header.",
        ) from exc
    if (
        header_idempotency_key
        and data.idempotency_key
        and (header_idempotency_key != data.idempotency_key)
    ):
        raise ProblemError(
            status=422,
            code="idempotency-key-mismatch",
            title="Idempotency keys did not match",
            detail="The header and form idempotency keys must be identical.",
        )
    try:
        filename = validate_pdf_filename(data.file.filename or "")
    except InvalidFilenameError as exc:
        raise ProblemError(
            status=422,
            code="invalid-filename",
            title="Filename was rejected",
            detail=str(exc),
        ) from exc
    if data.file.content_type and data.file.content_type.casefold() not in {
        "application/pdf",
        "application/octet-stream",
    }:
        raise ProblemError(
            status=422,
            code="invalid-content-type",
            title="File type was rejected",
            detail="The upload must use the application/pdf content type.",
        )

    settings = request.app.state.settings
    layout = StorageLayout.from_root(settings.storage_root)
    staged = None
    registration = None
    try:
        staged = await stream_upload(
            data.file,
            layout,
            max_bytes=settings.max_upload_bytes,
            chunk_bytes=settings.upload_chunk_bytes,
        )
        scan_result = await sync_to_thread(request.app.state.scanner, staged.path)
        with request.app.state.transition_lock:
            registration = register_staged_upload(
                db,
                staged=staged,
                layout=layout,
                filename=filename,
                collection_key=data.collection_key,
                idempotency_key=idempotency_key,
                actor_type=actor.kind,
                actor_id=actor.identifier,
                scan_result=scan_result,
                allow_possible_duplicate=data.possible_duplicate_confirmed,
            )
            if registration.idempotent_replay:
                staged.path.unlink(missing_ok=True)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                if registration.promoted is not None:
                    registration.promoted.path.unlink(missing_ok=True)
                existing = db.scalar(
                    select(Document).where(Document.idempotency_key == idempotency_key)
                )
                if existing is None:
                    raise
                if (
                    existing.sha256 != staged.sha256
                    or existing.size_bytes != staged.size_bytes
                    or existing.normalized_filename != normalize_filename(filename)
                    or existing.collection_key != data.collection_key
                ):
                    raise LifecycleError(
                        "The idempotency key was already used for a different file.",
                        code="idempotency-key-conflict",
                    ) from exc
                operation = db.scalar(
                    select(QueueOperation)
                    .where(
                        QueueOperation.document_id == existing.id,
                        QueueOperation.operation_type == OperationType.INGEST,
                    )
                    .order_by(QueueOperation.attempt.desc(), QueueOperation.created_at.desc())
                    .limit(1)
                )
                if operation is None:
                    raise
                return UploadResponse(
                    document=_document_summary(existing),
                    operation_id=operation.id,
                    idempotent_replay=True,
                )
    except (FileTooLargeError, InvalidPdfError) as exc:
        db.rollback()
        raise ProblemError(
            status=413 if isinstance(exc, FileTooLargeError) else 422,
            code="upload-too-large" if isinstance(exc, FileTooLargeError) else "invalid-pdf",
            title="PDF upload was rejected",
            detail=str(exc),
        ) from exc
    except ScannerError as exc:
        db.rollback()
        raise ProblemError(
            status=503,
            code="scanner-unavailable",
            title="Malware scan could not be completed",
            detail="The upload was not queued. Retry after ClamAV is healthy.",
        ) from exc
    except LifecycleError as exc:
        db.rollback()
        raise _lifecycle_problem(exc) from exc
    finally:
        await data.file.close()
        if staged is not None:
            staged.path.unlink(missing_ok=True)

    if registration is None:
        raise RuntimeError("upload registration unexpectedly missing")
    return UploadResponse(
        document=_document_summary(registration.document),
        operation_id=registration.operation.id,
        idempotent_replay=registration.idempotent_replay,
    )


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
    language: FromQuery[LanguageCode | None] = None,
    page: Annotated[int, QueryParameter(ge=1)] = 1,
    page_size: Annotated[int, QueryParameter(ge=1, le=100)] = 25,
) -> QueueListResponse:
    filters = [
        Document.state.in_(
            tuple(
                item
                for item in ACTIVE_DOCUMENT_STATES
                if item not in {DocumentState.INGESTED, DocumentState.CLASSIFICATION_REVIEW}
            )
        )
    ]
    if collection_key is not None:
        _configured_collection(request, collection_key)
        filters.append(Document.collection_key == collection_key)
    if language is not None:
        filters.append(Document.language == language)
    query = (
        select(QueueOperation)
        .join(QueueOperation.document)
        .where(*filters)
        .options(joinedload(QueueOperation.document))
    )
    all_operations = list(db.scalars(query).all())
    latest: dict[UUID, QueueOperation] = {}
    for operation in all_operations:
        previous = latest.get(operation.document_id)
        if previous is None or (operation.created_at, operation.attempt) > (
            previous.created_at,
            previous.attempt,
        ):
            latest[operation.document_id] = operation
    items = sorted(latest.values(), key=lambda item: item.created_at)
    total = len(items)
    visible = items[(page - 1) * page_size : page * page_size]
    return QueueListResponse.create(
        [
            QueueOperationSummary.model_validate(operation).model_copy(
                update={"document": _document_summary(operation.document)}
            )
            for operation in visible
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


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
    with request.app.state.transition_lock:
        try:
            document, storage_key = cancel_queued_document(
                db,
                operation_id=operation_id,
                actor_type=actor.kind,
                actor_id=actor.identifier,
            )
            db.commit()
        except LifecycleError as exc:
            db.rollback()
            raise _lifecycle_problem(exc) from exc
    if storage_key:
        layout = StorageLayout.from_root(request.app.state.settings.storage_root)
        try:
            remove_storage_key(layout, storage_key, missing_ok=True)
        except OSError as exc:
            logger.exception(
                "canonical cleanup failed",
                extra={"document_id": str(document.id), "operation_id": str(operation_id)},
            )
            raise ProblemError(
                status=500,
                code="storage-cleanup-failed",
                title="Queue cancellation cleanup failed",
                detail="The cleanup remains pending and this cancellation can be retried.",
            ) from exc
        with request.app.state.transition_lock:
            try:
                document = finalize_cancelled_storage(
                    db,
                    document_id=document.id,
                    storage_key=storage_key,
                    actor_type=actor.kind,
                    actor_id=actor.identifier,
                )
                db.commit()
            except LifecycleError as exc:
                db.rollback()
                raise _lifecycle_problem(exc) from exc
    return DocumentMutationResponse(document=_document_summary(document))


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
    with request.app.state.transition_lock:
        try:
            operation = retry_failed_document(
                db,
                operation_id=operation_id,
                actor_type=actor.kind,
                actor_id=actor.identifier,
            )
            db.commit()
        except LifecycleError as exc:
            db.rollback()
            raise _lifecycle_problem(exc) from exc
    return DocumentMutationResponse(
        document=_document_summary(operation.document), operation_id=operation.id
    )


@post(
    "/documents/{document_id:uuid}/classification",
    dependencies=_CSRF_ACTOR_DEPENDENCIES,
    status_code=200,
    responses=problem_responses(),
    sync_to_thread=True,
)
def resolve_document_classification(
    request: Request,
    document_id: FromPath[UUID],
    data: JSONBody[ClassificationBody],
    actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> DocumentMutationResponse:
    payload = data.root
    language = payload.language if payload.action == "override" else None
    reason = payload.reason if payload.action == "override" else None
    with request.app.state.transition_lock:
        existing = _get_document_detail(db, document_id)
        collection_key = payload.collection_key or existing.collection_key
        if collection_key is None:
            raise ProblemError(
                status=422,
                code="collection-required",
                title="Collection is required",
                detail="Assign a configured collection before resolving this document.",
            )
        _configured_collection(request, collection_key)
        try:
            operation = queue_classification_review(
                db,
                document_id=document_id,
                collection_key=collection_key,
                language=language,
                reason=reason,
                actor_type=actor.kind,
                actor_id=actor.identifier,
            )
            db.commit()
        except LifecycleError as exc:
            db.rollback()
            raise _lifecycle_problem(exc) from exc
    return DocumentMutationResponse(
        document=_document_summary(operation.document), operation_id=operation.id
    )


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
    with request.app.state.transition_lock:
        try:
            operation = queue_document_deletion(
                db,
                document_id=document_id,
                actor_type=actor.kind,
                actor_id=actor.identifier,
                reason=data.reason if data else None,
            )
            db.commit()
        except LifecycleError as exc:
            db.rollback()
            raise _lifecycle_problem(exc) from exc
    return DocumentMutationResponse(
        document=_document_summary(operation.document), operation_id=operation.id
    )


@post(
    "/search",
    dependencies=_CSRF_CHECK_DEPENDENCIES,
    status_code=200,
    responses=problem_responses(),
)
async def search_documents(
    request: Request,
    data: JSONBody[SearchRequest],
    _actor: NamedDependency[Actor],
    db: NamedDependency[Session],
) -> SearchResponse:
    for collection_key in data.collections:
        _configured_collection(request, collection_key)
    response = await search_retrieval(
        request.app.state.settings,
        data,
        client=getattr(request.app.state, "search_http_client", None),
    )
    for group in response.groups:
        ids = [hit.document_id for hit in group.hits]
        documents = (
            db.scalars(
                select(Document).where(
                    Document.id.in_(ids),
                    *_retrieval_catalog_filters(),
                )
            ).all()
            if ids
            else []
        )
        documents_by_id = {document.id: document for document in documents}
        invalid_hit = any(
            document_id not in documents_by_id
            or documents_by_id[document_id].collection_key != group.collection_key
            or (
                data.language is not None
                and documents_by_id[document_id].language != data.language
            )
            for document_id in ids
        )
        catalog_total = (
            db.scalar(
                select(func.count()).select_from(Document).where(
                    *_retrieval_catalog_filters(
                        collection_key=group.collection_key,
                        language=data.language,
                    ),
                )
            )
            or 0
        )
        if invalid_hit or group.total > catalog_total:
            raise ProblemError(
                status=502,
                code="search-catalog-mismatch",
                title="Search and catalog are out of sync",
                detail=(
                    "The retrieval response included a document or total outside its requested "
                    "collection boundary. No partial results were returned."
                ),
            )
    return response


@get(
    "/health/live",
    responses=problem_responses(),
    sync_to_thread=False,
)
def live() -> HealthResponse:
    return HealthResponse(status="ok", checks={"process": "ok"})


def _dependency_checks(request: Request, db: Session) -> dict[str, str]:
    settings = request.app.state.settings
    checks: dict[str, str] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        logger.exception("readiness database check failed")
        checks["database"] = "error"
    root = Path(settings.storage_root)
    storage_directories = (root, root / "objects", root / "temporary")
    checks["storage"] = (
        "ok"
        if all(path.is_dir() and os.access(path, os.W_OK) for path in storage_directories)
        else "error"
    )
    checks["scanner"] = (
        "ok"
        if clamd_ping(
            host=settings.clamd_host,
            port=settings.clamd_port,
            timeout=min(settings.clamd_timeout, 2.0),
        )
        else "error"
    )
    return checks


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
    return _health_response(_dependency_checks(request, db))


@get(
    "/health/dependencies",
    responses=_health_responses("A dependency is unavailable"),
    sync_to_thread=True,
)
def dependencies(request: Request, db: NamedDependency[Session]) -> Response[HealthResponse]:
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
    resolve_document_classification,
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
