"""Server-rendered browser routes."""

from __future__ import annotations

from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool

from pdf_bridge import __version__
from pdf_bridge.db import get_db
from pdf_bridge.lifecycle import ACTIVE_DOCUMENT_STATES
from pdf_bridge.models import (
    Document,
    DocumentState,
    JobBatch,
    LanguageCode,
    LanguageStatus,
    QueueOperation,
)
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
RETRIEVAL_STATES = (
    DocumentState.INGESTED,
    DocumentState.DELETE_QUEUED,
    DocumentState.DELETE_CLAIMED,
    DocumentState.DELETE_FAILED,
)
RETRIEVAL_LANGUAGES = (LanguageCode.EN, LanguageCode.FR)
RETRIEVAL_LANGUAGE_STATUSES = (LanguageStatus.DETECTED, LanguageStatus.OVERRIDDEN)
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
PROCESSING_STATES = tuple(
    state
    for state in ACTIVE_DOCUMENT_STATES
    if state not in {DocumentState.INGESTED, DocumentState.CLASSIFICATION_REVIEW}
)
LANGUAGE_FILTERS = frozenset({"all", "en", "fr", "und"})


def _collection_view(definition: Any) -> dict[str, Any]:
    return {
        "key": definition.key,
        "display_name": definition.display_name,
        "description": definition.description,
        "audience": definition.audience,
    }


def _configured_collections(request: Request) -> list[dict[str, Any]]:
    return [_collection_view(item) for item in request.app.state.settings.collections]


def _normalized_language(value: str, *, allow_undetermined: bool = False) -> str:
    normalized = value.strip().lower()
    if normalized not in LANGUAGE_FILTERS:
        return "all"
    if normalized == "und" and not allow_undetermined:
        return "all"
    return normalized


def _language_value(value: str) -> LanguageCode | None:
    return None if value == "all" else LanguageCode(value)


def _collection_counts(db: Session) -> dict[str | None, dict[str, int]]:
    counts: dict[str | None, dict[str, int]] = {}
    rows = db.execute(
        select(
            Document.collection_key,
            Document.state,
            Document.language,
            Document.language_status,
            func.count(),
        ).group_by(
            Document.collection_key,
            Document.state,
            Document.language,
            Document.language_status,
        )
    ).all()
    for collection_key, state, language, language_status, count in rows:
        item = counts.setdefault(
            collection_key,
            {
                "available": 0,
                "processing": 0,
                "review_required": 0,
                "english": 0,
                "french": 0,
                "undetermined": 0,
            },
        )
        language_value = getattr(language, "value", language)
        if (
            state in RETRIEVAL_STATES
            and language_value in {"en", "fr"}
            and language_status in RETRIEVAL_LANGUAGE_STATUSES
        ):
            item["available"] += count
            if language_value == "en":
                item["english"] += count
            else:
                item["french"] += count
        if state in PROCESSING_STATES:
            item["processing"] += count
        if state == DocumentState.CLASSIFICATION_REVIEW:
            item["review_required"] += count
    return counts


def _available_document_count(
    db: Session, *, collection_key: str, language: LanguageCode | None
) -> int:
    filters = [
        Document.collection_key == collection_key,
        Document.state.in_(RETRIEVAL_STATES),
        Document.language.in_(RETRIEVAL_LANGUAGES),
        Document.language_status.in_(RETRIEVAL_LANGUAGE_STATUSES),
    ]
    if language is not None:
        filters.append(Document.language == language)
    return db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0


def _error_page(
    request: Request, *, status_code: int, title: str, message: str
):
    context = _base_context(request, active_page="")
    context.update({"status_code": status_code, "title": title, "message": message})
    return templates.TemplateResponse(request, "error.html", context, status_code=status_code)


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
    language: str = Query(default="all", max_length=3),
    db: Session = Depends(get_db),
):
    response_status = 200
    query = q.strip()
    selected_language = _normalized_language(language)
    collections = _configured_collections(request)
    counts_by_collection = _collection_counts(db)
    for collection in collections:
        collection.update(counts_by_collection.get(collection["key"], {}))
        collection.setdefault("available", 0)
        collection.setdefault("processing", 0)
        collection.setdefault("review_required", 0)
        collection.setdefault("english", 0)
        collection.setdefault("french", 0)
        collection.setdefault("undetermined", 0)
        collection["search_total"] = None
        collection["href"] = "/library/" + collection["key"]

    context = _base_context(request, active_page="library")
    context.update(
        {
            "collections": collections,
            "search_query": query,
            "search_mode": mode.value,
            "language_filter": selected_language,
            "unassigned_review_count": counts_by_collection.get(None, {}).get(
                "review_required", 0
            ),
        }
    )

    if query:
        try:
            search_response = await search_retrieval(
                request.app.state.settings,
                SearchRequest(
                    query=query,
                    mode=mode,
                    collections=[item["key"] for item in collections],
                    language=_language_value(selected_language),
                    include_hits=False,
                    page=1,
                    page_size=1,
                ),
                client=getattr(request.app.state, "search_http_client", None),
            )
            groups = {group.collection_key: group for group in search_response.groups}
            expected = {item["key"] for item in collections}
            if set(groups) != expected:
                raise ProblemError(
                    status=502,
                    code="search-invalid-response",
                    title="Search returned incomplete collection counts",
                    detail=(
                        "The retrieval service did not return exactly one count for every "
                        "configured collection. No partial counts were shown."
                    ),
                )
            validated_totals: dict[str, int] = {}
            for collection in collections:
                group_total = groups[collection["key"]].total
                if group_total > _available_document_count(
                    db,
                    collection_key=collection["key"],
                    language=_language_value(selected_language),
                ):
                    raise ProblemError(
                        status=502,
                        code="search-catalog-mismatch",
                        title="Search count exceeded the catalog",
                        detail=(
                            "The retrieval service returned a collection total larger than the "
                            "active PDF Bridge catalog. No partial counts were shown."
                        ),
                    )
                validated_totals[collection["key"]] = group_total

            query_string = urlencode(
                {
                    "q": query,
                    "mode": mode.value,
                    **(
                        {"language": selected_language}
                        if selected_language != "all"
                        else {}
                    ),
                }
            )
            for collection in collections:
                collection["search_total"] = validated_totals[collection["key"]]
                collection["href"] = f"/library/{collection['key']}?{query_string}"
        except ProblemError as exc:
            context["search_error"] = exc.detail
            response_status = exc.status
    return templates.TemplateResponse(
        request, "library.html", context, status_code=response_status
    )


@router.get("/library/{collection_key}")
async def collection_page(
    request: Request,
    collection_key: str,
    q: str = Query(default="", max_length=1000),
    mode: SearchMode = SearchMode.HYBRID,
    language: str = Query(default="all", max_length=3),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    collection_map = {
        item["key"]: item for item in _configured_collections(request)
    }
    collection = collection_map.get(collection_key)
    if collection is None:
        return _error_page(
            request,
            status_code=404,
            title="Collection not found",
            message="The requested collection is not configured for this PDF Bridge deployment.",
        )

    page_size = 25
    query = q.strip()
    selected_language = _normalized_language(language)
    context = _base_context(request, active_page="library")
    response_status = 200
    context.update(
        {
            "collection": collection,
            "search_query": query,
            "search_mode": mode.value,
            "language_filter": selected_language,
            "page": page,
            "total_pages": 1,
            "pagination_query": urlencode(
                {
                    **({"q": query, "mode": mode.value} if query else {}),
                    **(
                        {"language": selected_language}
                        if selected_language != "all"
                        else {}
                    ),
                }
            ),
        }
    )

    try:
        if query:
            search_response = await search_retrieval(
                request.app.state.settings,
                SearchRequest(
                    query=query,
                    mode=mode,
                    collections=[collection_key],
                    language=_language_value(selected_language),
                    include_hits=True,
                    page=page,
                    page_size=page_size,
                ),
                client=getattr(request.app.state, "search_http_client", None),
            )
            if (
                len(search_response.groups) != 1
                or search_response.groups[0].collection_key != collection_key
            ):
                raise ProblemError(
                    status=502,
                    code="search-invalid-response",
                    title="Search returned the wrong collection",
                    detail=(
                        "The retrieval service did not return exactly the requested collection. "
                        "No partial results were shown."
                    ),
                )
            group = search_response.groups[0]
            if group.total > _available_document_count(
                db,
                collection_key=collection_key,
                language=_language_value(selected_language),
            ):
                raise ProblemError(
                    status=502,
                    code="search-catalog-mismatch",
                    title="Search count exceeded the catalog",
                    detail=(
                        "The retrieval service returned a total larger than this collection's "
                        "active catalog. No partial results were shown."
                    ),
                )
            hit_ids = [hit.document_id for hit in group.hits]
            statement = select(Document).where(
                Document.id.in_(hit_ids),
                Document.collection_key == collection_key,
                Document.state.in_(RETRIEVAL_STATES),
                Document.language.in_(RETRIEVAL_LANGUAGES),
                Document.language_status.in_(RETRIEVAL_LANGUAGE_STATUSES),
            )
            language_value = _language_value(selected_language)
            if language_value is not None:
                statement = statement.where(Document.language == language_value)
            documents = db.scalars(statement).all() if hit_ids else []
            documents_by_id = {document.id: document for document in documents}
            if any(document_id not in documents_by_id for document_id in hit_ids):
                raise ProblemError(
                    status=502,
                    code="search-catalog-mismatch",
                    title="Search crossed a catalog boundary",
                    detail=(
                        "The retrieval service returned an inactive document or a document from "
                        "another collection or language. No partial results were shown."
                    ),
                )
            results = [
                {
                    "document": document_view(documents_by_id[hit.document_id]),
                    "score": hit.score,
                    "snippet": hit.snippet,
                    "match_metadata": hit.match_metadata,
                }
                for hit in group.hits
            ]
            context.update(
                {
                    "search_results": results,
                    "result_count": group.total,
                    "total_pages": max(1, ceil(group.total / page_size)),
                }
            )
        else:
            filters = [
                Document.collection_key == collection_key,
                Document.state.in_(RETRIEVAL_STATES),
                Document.language.in_(RETRIEVAL_LANGUAGES),
                Document.language_status.in_(RETRIEVAL_LANGUAGE_STATUSES),
            ]
            language_value = _language_value(selected_language)
            if language_value is not None:
                filters.append(Document.language == language_value)
            total = db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0
            documents = db.scalars(
                select(Document)
                .where(*filters)
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
    except ProblemError as exc:
        response_status = exc.status
        context.update(
            {
                "search_results": [],
                "search_error": exc.detail,
                "result_count": 0,
            }
        )
    return templates.TemplateResponse(
        request, "collection_detail.html", context, status_code=response_status
    )


@router.get("/queue")
def queue_page(
    request: Request,
    status: str = Query(default="all", max_length=30),
    collection: str = Query(default="all", max_length=64),
    language: str = Query(default="all", max_length=3),
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

    collection_keys = {item.key for item in request.app.state.settings.collections}
    selected_collection = collection if collection in collection_keys else "all"
    if selected_collection != "all":
        current_operations = [
            item
            for item in current_operations
            if item.document.collection_key == selected_collection
        ]

    selected_language = _normalized_language(language, allow_undetermined=True)
    if selected_language != "all":
        current_operations = [
            item
            for item in current_operations
            if getattr(item.document.language, "value", item.document.language)
            == selected_language
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
            "collection_filter": selected_collection,
            "language_filter": selected_language,
            "collections": _configured_collections(request),
            "sort": sort,
            "order": order,
            "page": page,
            "total_pages": max(1, ceil(total / page_size)),
            "pagination_query": urlencode(
                {
                    "status": status,
                    "collection": selected_collection,
                    "language": selected_language,
                    "sort": sort,
                    "order": order,
                }
            ),
            "last_claim_at": last_claim_at,
        }
    )
    return templates.TemplateResponse(request, "queue.html", context)


@router.get("/review")
def review_page(
    request: Request,
    collection: str = Query(default="all", max_length=64),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    page_size = 25
    configured = _configured_collections(request)
    collection_keys = {item["key"] for item in configured}
    selected_collection = (
        collection if collection in collection_keys or collection == "unassigned" else "all"
    )
    filters = [Document.state == DocumentState.CLASSIFICATION_REVIEW]
    if selected_collection == "unassigned":
        filters.append(Document.collection_key.is_(None))
    elif selected_collection != "all":
        filters.append(Document.collection_key == selected_collection)

    total = db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0
    documents = db.scalars(
        select(Document)
        .where(*filters)
        .order_by(Document.uploaded_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    collection_map = {item["key"]: item for item in configured}
    views = []
    for document in documents:
        item = document_view(document)
        item["collection"] = collection_map.get(document.collection_key)
        views.append(item)

    context = _base_context(request, active_page="review")
    context.update(
        {
            "documents": views,
            "review_count": total,
            "collections": configured,
            "collection_filter": selected_collection,
            "page": page,
            "total_pages": max(1, ceil(total / page_size)),
            "pagination_query": urlencode({"collection": selected_collection}),
        }
    )
    return templates.TemplateResponse(request, "review.html", context)


@router.get("/upload")
async def upload_page(
    request: Request,
    collection: str = Query(default="", max_length=64),
):
    settings = request.app.state.settings
    scanner_available = await run_in_threadpool(
        clamd_ping,
        host=settings.clamd_host,
        port=settings.clamd_port,
        timeout=min(settings.clamd_timeout, 1.0),
    )
    collection_keys = {item.key for item in settings.collections}
    context = _base_context(request, active_page="upload")
    context.update(
        {
            "max_file_count": settings.max_upload_files,
            "max_file_bytes": settings.max_upload_bytes,
            "max_file_size_display": f"{settings.max_upload_bytes / 1024 / 1024:.0f} MiB",
            "scanner_available": scanner_available,
            "collections": _configured_collections(request),
            "selected_collection": collection if collection in collection_keys else None,
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
        return _error_page(
            request,
            status_code=404,
            title="Document not found",
            message="The requested document does not exist or is no longer available.",
        )

    active_operation = max(
        document.operations,
        key=lambda item: (item.created_at, item.attempt),
        default=None,
    )
    if document.state == DocumentState.CLASSIFICATION_REVIEW:
        active_page = "review"
    elif document.state in LIBRARY_STATES:
        active_page = "library"
    else:
        active_page = "queue"
    configured = _configured_collections(request)
    collection_map = {item["key"]: item for item in configured}
    context = _base_context(request, active_page=active_page)
    context.update(
        {
            "document": document_view(document),
            "collection": collection_map.get(document.collection_key),
            "collections": configured,
            "active_operation": operation_view(active_operation) if active_operation else None,
            "audit_events": [audit_event_view(event) for event in document.audit_events],
            "pipeline_components": components_view(active_operation),
        }
    )
    return templates.TemplateResponse(request, "document_detail.html", context)
