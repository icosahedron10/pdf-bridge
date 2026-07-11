"""Human-facing typed JSON API."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool

from pdf_bridge.db import get_db
from pdf_bridge.lifecycle import (
    ACTIVE_DOCUMENT_STATES,
    DuplicateDocumentError,
    LifecycleError,
    PossibleDuplicateError,
    can_serve_content,
    cancel_queued_document,
    finalize_cancelled_storage,
    find_preflight_duplicates,
    queue_document_deletion,
    register_staged_upload,
    retry_failed_document,
)
from pdf_bridge.models import Document, DocumentState, OperationType, QueueOperation
from pdf_bridge.problems import ProblemError
from pdf_bridge.scanner import ScannerError, clamd_ping
from pdf_bridge.schemas import (
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
router = APIRouter(prefix="/api/v1", tags=["PDF Bridge"], responses=problem_responses())
idempotency_adapter = TypeAdapter(IdempotencyKey)


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


@router.get("/documents", response_model=DocumentListResponse)
def list_documents(
    scope: Literal["library", "queue", "all"] = "all",
    state: DocumentState | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    filters = []
    if state is not None:
        filters.append(Document.state == state)
    elif scope == "library":
        filters.append(
            Document.state.in_(
                (
                    DocumentState.INGESTED,
                    DocumentState.DELETE_QUEUED,
                    DocumentState.DELETE_CLAIMED,
                    DocumentState.DELETE_CLEANUP,
                    DocumentState.DELETE_FAILED,
                )
            )
        )
    elif scope == "queue":
        filters.append(
            Document.state.in_(
                tuple(item for item in ACTIVE_DOCUMENT_STATES if item != DocumentState.INGESTED)
            )
        )
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


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def get_document(
    document_id: UUID,
    _actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DocumentDetail:
    document = _get_document_detail(db, document_id)
    return DocumentDetail.model_validate(document).model_copy(
        update={"detail_url": f"/documents/{document.id}"}
    )


@router.get("/documents/{document_id}/content", response_class=FileResponse)
def document_content(
    request: Request,
    document_id: UUID,
    _actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> FileResponse:
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
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=document.original_filename,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'self'",
        },
    )


@router.post("/uploads/preflight", response_model=UploadPreflightResponse)
def upload_preflight(
    payload: UploadPreflightRequest,
    _actor: Actor = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> UploadPreflightResponse:
    try:
        normalized = normalize_filename(payload.filename)
    except InvalidFilenameError as exc:
        raise ProblemError(
            status=422,
            code="invalid-filename",
            title="Filename was rejected",
            detail=str(exc),
        ) from exc
    matches = find_preflight_duplicates(
        db, normalized_filename=normalized, size_bytes=payload.size_bytes
    )
    return UploadPreflightResponse(
        normalized_filename=normalized,
        requires_confirmation=bool(matches),
        possible_duplicates=[_duplicate_match(document) for document in matches],
    )


@router.post("/uploads", response_model=UploadResponse, status_code=201)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File()],
    possible_duplicate_confirmed: Annotated[bool, Form()] = False,
    form_idempotency_key: Annotated[str | None, Form(alias="idempotency_key")] = None,
    header_idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    actor: Actor = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> UploadResponse:
    raw_key = header_idempotency_key or form_idempotency_key
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
        and form_idempotency_key
        and (header_idempotency_key != form_idempotency_key)
    ):
        raise ProblemError(
            status=422,
            code="idempotency-key-mismatch",
            title="Idempotency keys did not match",
            detail="The header and form idempotency keys must be identical.",
        )
    try:
        filename = validate_pdf_filename(file.filename or "")
    except InvalidFilenameError as exc:
        raise ProblemError(
            status=422,
            code="invalid-filename",
            title="Filename was rejected",
            detail=str(exc),
        ) from exc
    if file.content_type and file.content_type.casefold() not in {
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
            file,
            layout,
            max_bytes=settings.max_upload_bytes,
            chunk_bytes=settings.upload_chunk_bytes,
        )
        scan_result = await run_in_threadpool(request.app.state.scanner, staged.path)
        with request.app.state.transition_lock:
            registration = register_staged_upload(
                db,
                staged=staged,
                layout=layout,
                filename=filename,
                idempotency_key=idempotency_key,
                actor_type=actor.kind,
                actor_id=actor.identifier,
                scan_result=scan_result,
                allow_possible_duplicate=possible_duplicate_confirmed,
            )
            if registration.idempotent_replay:
                staged.path.unlink(missing_ok=True)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if registration.promoted is not None:
                    registration.promoted.path.unlink(missing_ok=True)
                existing = db.scalar(
                    select(Document).where(Document.idempotency_key == idempotency_key)
                )
                if existing is None or existing.sha256 != staged.sha256:
                    raise
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
        await file.close()
        if staged is not None:
            staged.path.unlink(missing_ok=True)

    if registration is None:
        raise RuntimeError("upload registration unexpectedly missing")
    return UploadResponse(
        document=_document_summary(registration.document),
        operation_id=registration.operation.id,
        idempotent_replay=registration.idempotent_replay,
    )


@router.get("/queue", response_model=QueueListResponse)
def list_queue(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> QueueListResponse:
    query = (
        select(QueueOperation)
        .join(QueueOperation.document)
        .where(
            Document.state.in_(
                tuple(item for item in ACTIVE_DOCUMENT_STATES if item != DocumentState.INGESTED)
            )
        )
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


@router.delete("/queue/{operation_id}", response_model=DocumentMutationResponse)
def cancel_queue_item(
    request: Request,
    operation_id: UUID,
    actor: Actor = Depends(require_csrf),
    db: Session = Depends(get_db),
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


@router.post("/queue/{operation_id}/retry", response_model=DocumentMutationResponse)
def retry_queue_item(
    request: Request,
    operation_id: UUID,
    actor: Actor = Depends(require_csrf),
    db: Session = Depends(get_db),
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


@router.post("/documents/{document_id}/deletion", response_model=DocumentMutationResponse)
def request_document_deletion(
    request: Request,
    document_id: UUID,
    payload: Annotated[DeleteDocumentRequest | None, Body()] = None,
    actor: Actor = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> DocumentMutationResponse:
    with request.app.state.transition_lock:
        try:
            operation = queue_document_deletion(
                db,
                document_id=document_id,
                actor_type=actor.kind,
                actor_id=actor.identifier,
                reason=payload.reason if payload else None,
            )
            db.commit()
        except LifecycleError as exc:
            db.rollback()
            raise _lifecycle_problem(exc) from exc
    return DocumentMutationResponse(
        document=_document_summary(operation.document), operation_id=operation.id
    )


@router.post("/search", response_model=SearchResponse)
async def search_documents(
    request: Request,
    payload: SearchRequest,
    _actor: Actor = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SearchResponse:
    response = await search_retrieval(
        request.app.state.settings,
        payload,
        client=getattr(request.app.state, "search_http_client", None),
    )
    ids = [hit.document_id for hit in response.hits]
    known = set(
        db.scalars(
            select(Document.id).where(
                Document.id.in_(ids),
                Document.state.in_(
                    (
                        DocumentState.INGESTED,
                        DocumentState.DELETE_QUEUED,
                        DocumentState.DELETE_CLAIMED,
                        DocumentState.DELETE_CLEANUP,
                        DocumentState.DELETE_FAILED,
                    )
                ),
            )
        ).all()
    )
    if any(document_id not in known for document_id in ids):
        raise ProblemError(
            status=502,
            code="search-catalog-mismatch",
            title="Search and catalog are out of sync",
            detail="The retrieval response included an unknown or removed document ID.",
        )
    return response


@router.get("/health/live", response_model=HealthResponse)
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


def _health_response(checks: dict[str, str]) -> JSONResponse:
    healthy = all(value == "ok" for value in checks.values())
    body = HealthResponse(status="ok" if healthy else "degraded", checks=checks)
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=200 if healthy else 503,
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "A dependency is not ready"}},
)
def ready(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    return _health_response(_dependency_checks(request, db))


@router.get(
    "/health/dependencies",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "A dependency is unavailable"}},
)
def dependencies(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    return _health_response(_dependency_checks(request, db))
