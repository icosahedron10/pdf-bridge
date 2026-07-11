"""Server-rendered browser routes."""

from __future__ import annotations

from math import ceil
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool

from pdf_bridge import __version__
from pdf_bridge.db import get_db
from pdf_bridge.models import Document, DocumentState, JobBatch, QueueOperation
from pdf_bridge.problems import ProblemError
from pdf_bridge.scanner import clamd_ping
from pdf_bridge.schemas import SearchMode, SearchRequest
from pdf_bridge.search import search_retrieval
from pdf_bridge.security import csrf_token, get_actor
from pdf_bridge.view_models import (
    audit_event_view,
    components_view,
    document_view,
    operation_view,
)

TEMPLATE_ROOT = Path(__file__).with_name("templates")
templates = Jinja2Templates(directory=str(TEMPLATE_ROOT))
router = APIRouter(include_in_schema=False)

LIBRARY_STATES = (
    DocumentState.INGESTED,
    DocumentState.DELETE_QUEUED,
    DocumentState.DELETE_CLAIMED,
    DocumentState.DELETE_CLEANUP,
    DocumentState.DELETE_FAILED,
)
QUEUE_STATES = (
    DocumentState.QUEUED,
    DocumentState.CLAIMED,
    DocumentState.STAGED,
    DocumentState.INGEST_FAILED,
    DocumentState.DELETE_QUEUED,
    DocumentState.DELETE_CLAIMED,
    DocumentState.DELETE_CLEANUP,
    DocumentState.DELETE_FAILED,
    DocumentState.CANCEL_CLEANUP,
)


def _base_context(request: Request, *, active_page: str) -> dict[str, Any]:
    actor = get_actor(request)
    return {
        "request": request,
        "active_page": active_page,
        "csrf_token": csrf_token(request),
        "actor_display_name": (
            actor.identifier if actor.kind == "trusted-header" else "POC workspace"
        ),
        "environment_name": (
            "Proof of concept"
            if request.app.state.settings.app_env != "enterprise"
            else "Enterprise"
        ),
        "app_version": __version__,
    }


@router.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/library", status_code=307)


@router.get("/library")
async def library_page(
    request: Request,
    q: str = Query(default="", max_length=1000),
    mode: SearchMode = SearchMode.HYBRID,
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    page_size = 25
    context = _base_context(request, active_page="library")
    context.update(
        {
            "search_query": q.strip(),
            "search_mode": mode.value,
            "page": page,
            "total_pages": 1,
            "pagination_query": "",
        }
    )

    if q.strip():
        try:
            search_response = await search_retrieval(
                request.app.state.settings,
                SearchRequest(query=q.strip(), mode=mode, limit=100),
                client=getattr(request.app.state, "search_http_client", None),
            )
            hit_ids = [hit.document_id for hit in search_response.hits]
            documents = (
                db.scalars(
                    select(Document).where(
                        Document.id.in_(hit_ids), Document.state.in_(LIBRARY_STATES)
                    )
                ).all()
                if hit_ids
                else []
            )
            documents_by_id = {document.id: document for document in documents}
            missing = [document_id for document_id in hit_ids if document_id not in documents_by_id]
            if missing:
                raise ProblemError(
                    status=502,
                    code="search-catalog-mismatch",
                    title="Search and catalog are out of sync",
                    detail=(
                        "The retrieval service returned a document that is not an active library "
                        "record. No partial result set was shown."
                    ),
                )
            results = [
                {
                    "document": document_view(documents_by_id[hit.document_id]),
                    "score": hit.score,
                    "snippet": hit.snippet,
                    "match_metadata": hit.match_metadata,
                }
                for hit in search_response.hits
            ]
            context.update({"search_results": results, "result_count": len(results)})
        except ProblemError as exc:
            context.update({"search_results": [], "search_error": exc.detail, "result_count": 0})
    else:
        total = (
            db.scalar(
                select(func.count()).select_from(Document).where(Document.state.in_(LIBRARY_STATES))
            )
            or 0
        )
        documents = db.scalars(
            select(Document)
            .where(Document.state.in_(LIBRARY_STATES))
            .order_by(Document.ingested_at.desc(), Document.uploaded_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        context.update(
            {
                "documents": [document_view(document) for document in documents],
                "document_count": total,
                "total_pages": max(1, ceil(total / page_size)),
            }
        )
    return templates.TemplateResponse(request, "library.html", context)


@router.get("/queue")
def queue_page(
    request: Request,
    status: str = Query(default="all", max_length=30),
    sort: str = Query(default="created_at", pattern="^(created_at|status|filename)$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    page_size = 25
    operations = db.scalars(
        select(QueueOperation)
        .join(QueueOperation.document)
        .options(joinedload(QueueOperation.document))
        .where(Document.state.in_(QUEUE_STATES))
    ).all()

    latest_by_document: dict[UUID, QueueOperation] = {}
    for operation_item in operations:
        current = latest_by_document.get(operation_item.document_id)
        if current is None or (operation_item.created_at, operation_item.attempt) > (
            current.created_at,
            current.attempt,
        ):
            latest_by_document[operation_item.document_id] = operation_item
    current_operations = list(latest_by_document.values())

    normalized_status = status.upper()
    if status.casefold() != "all":
        current_operations = [
            item for item in current_operations if item.document.state.value == normalized_status
        ]

    reverse = order == "desc"
    if sort == "filename":
        current_operations.sort(key=lambda item: item.document.normalized_filename, reverse=reverse)
    elif sort == "status":
        current_operations.sort(key=lambda item: item.document.state.value, reverse=reverse)
    else:
        current_operations.sort(key=lambda item: item.created_at, reverse=reverse)

    total = len(current_operations)
    start = (page - 1) * page_size
    visible = current_operations[start : start + page_size]
    last_claim_at = db.scalar(select(func.max(JobBatch.claimed_at)))
    context = _base_context(request, active_page="queue")
    context.update(
        {
            "operations": [operation_view(item) for item in visible],
            "operation_count": total,
            "status_filter": status,
            "sort": sort,
            "order": order,
            "page": page,
            "total_pages": max(1, ceil(total / page_size)),
            "pagination_query": f"status={status}&sort={sort}&order={order}",
            "last_claim_at": last_claim_at,
        }
    )
    return templates.TemplateResponse(request, "queue.html", context)


@router.get("/upload")
async def upload_page(request: Request):
    settings = request.app.state.settings
    scanner_available = await run_in_threadpool(
        clamd_ping,
        host=settings.clamd_host,
        port=settings.clamd_port,
        timeout=min(settings.clamd_timeout, 1.0),
    )
    context = _base_context(request, active_page="upload")
    context.update(
        {
            "max_file_count": settings.max_upload_files,
            "max_file_bytes": settings.max_upload_bytes,
            "max_file_size_display": f"{settings.max_upload_bytes / 1024 / 1024:.0f} MiB",
            "scanner_available": scanner_available,
        }
    )
    return templates.TemplateResponse(request, "upload.html", context)


@router.get("/documents/{document_id}")
def document_page(request: Request, document_id: UUID, db: Session = Depends(get_db)):
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
        context = _base_context(request, active_page="")
        context.update(
            {
                "status_code": 404,
                "title": "Document not found",
                "message": "The requested document does not exist or is no longer available.",
            }
        )
        return templates.TemplateResponse(request, "error.html", context, status_code=404)

    active_operation = max(
        document.operations,
        key=lambda item: (item.created_at, item.attempt),
        default=None,
    )
    context = _base_context(
        request,
        active_page=("library" if document.state in LIBRARY_STATES else "queue"),
    )
    context.update(
        {
            "document": document_view(document),
            "active_operation": operation_view(active_operation) if active_operation else None,
            "audit_events": [audit_event_view(event) for event in document.audit_events],
            "pipeline_components": components_view(active_operation),
        }
    )
    return templates.TemplateResponse(request, "document_detail.html", context)
