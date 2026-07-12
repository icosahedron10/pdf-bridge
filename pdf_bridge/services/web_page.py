"""Business and page-data services for the server-rendered web interface."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import ceil
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from pdf_bridge.contracts.schemas import SearchMode, SearchRequest, SearchResponse
from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.models import (
    Document,
    DocumentState,
    JobBatch,
    LanguageCode,
    LanguageStatus,
    QueueOperation,
)
from pdf_bridge.presentation.view_models import (
    audit_event_view,
    components_view,
    document_view,
    operation_view,
)
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.lifecycle import ACTIVE_DOCUMENT_STATES

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
PAGE_SIZE = 25

SearchRetriever = Callable[..., Awaitable[SearchResponse]]
ScannerPing = Callable[..., bool]
ThemeRenderer = Callable[[Settings], str]


@dataclass(frozen=True, slots=True)
class WebRequestState:
    """Framework values extracted by the controller for page construction."""

    request: Any
    settings: Settings
    csrf_token: str
    actor_kind: str
    actor_identifier: str
    app_version: str
    search_http_client: Any | None = None


@dataclass(frozen=True, slots=True)
class PageResult:
    """Framework-neutral page description returned to the controller."""

    template_name: str
    context: dict[str, Any]
    status_code: int = 200


def collection_view(definition: Any) -> dict[str, Any]:
    return {
        "key": definition.key,
        "display_name": definition.display_name,
        "description": definition.description,
        "audience": definition.audience,
    }


def configured_collections(settings: Settings) -> list[dict[str, Any]]:
    return [collection_view(item) for item in settings.collections]


def normalized_language(value: str, *, allow_undetermined: bool = False) -> str:
    normalized = value.strip().lower()
    if normalized not in LANGUAGE_FILTERS:
        return "all"
    if normalized == "und" and not allow_undetermined:
        return "all"
    return normalized


def language_value(value: str) -> LanguageCode | None:
    return None if value == "all" else LanguageCode(value)


def collection_counts(db: Session) -> dict[str | None, dict[str, int]]:
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
        language_code = getattr(language, "value", language)
        if (
            state in RETRIEVAL_STATES
            and language_code in {"en", "fr"}
            and language_status in RETRIEVAL_LANGUAGE_STATUSES
        ):
            item["available"] += count
            if language_code == "en":
                item["english"] += count
            else:
                item["french"] += count
        if state in PROCESSING_STATES:
            item["processing"] += count
        if state == DocumentState.CLASSIFICATION_REVIEW:
            item["review_required"] += count
    return counts


def available_document_count(
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


def base_context(state: WebRequestState, *, active_page: str) -> dict[str, Any]:
    return {
        "request": state.request,
        "active_page": active_page,
        "csrf_token": state.csrf_token,
        "actor_display_name": (
            state.actor_identifier if state.actor_kind == "trusted-header" else "POC workspace"
        ),
        "environment_name": (
            "Proof of concept" if state.settings.app_env != "enterprise" else "Enterprise"
        ),
        "theme_default": state.settings.theme_default,
        "app_version": state.app_version,
    }


def error_page(state: WebRequestState, *, status_code: int, title: str, message: str) -> PageResult:
    context = base_context(state, active_page="")
    context.update({"status_code": status_code, "title": title, "message": message})
    return PageResult("error.html", context, status_code)


def render_theme_stylesheet(settings: Settings, *, renderer: ThemeRenderer) -> str:
    return renderer(settings)


def index_location() -> str:
    return "/library"


async def build_library_page(
    state: WebRequestState,
    db: Session,
    *,
    query_value: str,
    mode: SearchMode,
    language: str,
    search_retriever: SearchRetriever,
) -> PageResult:
    response_status = 200
    query = query_value.strip()
    selected_language = normalized_language(language)
    collections = configured_collections(state.settings)
    counts_by_collection = collection_counts(db)
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

    context = base_context(state, active_page="library")
    context.update(
        {
            "collections": collections,
            "search_query": query,
            "search_mode": mode.value,
            "language_filter": selected_language,
            "unassigned_review_count": counts_by_collection.get(None, {}).get("review_required", 0),
        }
    )

    if query:
        try:
            search_response = await search_retriever(
                state.settings,
                SearchRequest(
                    query=query,
                    mode=mode,
                    collections=[item["key"] for item in collections],
                    language=language_value(selected_language),
                    include_hits=False,
                    page=1,
                    page_size=1,
                ),
                client=state.search_http_client,
            )
            groups = {group.collection_key: group for group in search_response.groups}
            expected = {item["key"] for item in collections}
            if set(groups) != expected:
                raise ServiceError(
                    (
                        "The retrieval service did not return exactly one count for every "
                        "configured collection. No partial counts were shown."
                    ),
                    status=502,
                    code="search-invalid-response",
                    title="Search returned incomplete collection counts",
                )
            validated_totals: dict[str, int] = {}
            for collection in collections:
                group_total = groups[collection["key"]].total
                if group_total > available_document_count(
                    db,
                    collection_key=collection["key"],
                    language=language_value(selected_language),
                ):
                    raise ServiceError(
                        (
                            "The retrieval service returned a collection total larger than the "
                            "active PDF Bridge catalog. No partial counts were shown."
                        ),
                        status=502,
                        code="search-catalog-mismatch",
                        title="Search count exceeded the catalog",
                    )
                validated_totals[collection["key"]] = group_total

            query_string = urlencode(
                {
                    "q": query,
                    "mode": mode.value,
                    **({"language": selected_language} if selected_language != "all" else {}),
                }
            )
            for collection in collections:
                collection["search_total"] = validated_totals[collection["key"]]
                collection["href"] = f"/library/{collection['key']}?{query_string}"
        except ServiceError as exc:
            context["search_error"] = str(exc)
            response_status = exc.status
    return PageResult("library.html", context, response_status)


async def build_collection_page(
    state: WebRequestState,
    db: Session,
    *,
    collection_key: str,
    query_value: str,
    mode: SearchMode,
    language: str,
    page: int,
    search_retriever: SearchRetriever,
) -> PageResult:
    collection_map = {item["key"]: item for item in configured_collections(state.settings)}
    collection = collection_map.get(collection_key)
    if collection is None:
        return error_page(
            state,
            status_code=404,
            title="Collection not found",
            message="The requested collection is not configured for this PDF Bridge deployment.",
        )

    query = query_value.strip()
    selected_language = normalized_language(language)
    context = base_context(state, active_page="library")
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
                    **({"language": selected_language} if selected_language != "all" else {}),
                }
            ),
        }
    )

    try:
        if query:
            search_response = await search_retriever(
                state.settings,
                SearchRequest(
                    query=query,
                    mode=mode,
                    collections=[collection_key],
                    language=language_value(selected_language),
                    include_hits=True,
                    page=page,
                    page_size=PAGE_SIZE,
                ),
                client=state.search_http_client,
            )
            if (
                len(search_response.groups) != 1
                or search_response.groups[0].collection_key != collection_key
            ):
                raise ServiceError(
                    (
                        "The retrieval service did not return exactly the requested collection. "
                        "No partial results were shown."
                    ),
                    status=502,
                    code="search-invalid-response",
                    title="Search returned the wrong collection",
                )
            group = search_response.groups[0]
            if group.total > available_document_count(
                db,
                collection_key=collection_key,
                language=language_value(selected_language),
            ):
                raise ServiceError(
                    (
                        "The retrieval service returned a total larger than this collection's "
                        "active catalog. No partial results were shown."
                    ),
                    status=502,
                    code="search-catalog-mismatch",
                    title="Search count exceeded the catalog",
                )
            hit_ids = [hit.document_id for hit in group.hits]
            statement = select(Document).where(
                Document.id.in_(hit_ids),
                Document.collection_key == collection_key,
                Document.state.in_(RETRIEVAL_STATES),
                Document.language.in_(RETRIEVAL_LANGUAGES),
                Document.language_status.in_(RETRIEVAL_LANGUAGE_STATUSES),
            )
            selected_language_value = language_value(selected_language)
            if selected_language_value is not None:
                statement = statement.where(Document.language == selected_language_value)
            documents = db.scalars(statement).all() if hit_ids else []
            documents_by_id = {document.id: document for document in documents}
            if any(document_id not in documents_by_id for document_id in hit_ids):
                raise ServiceError(
                    (
                        "The retrieval service returned an inactive document or a document from "
                        "another collection or language. No partial results were shown."
                    ),
                    status=502,
                    code="search-catalog-mismatch",
                    title="Search crossed a catalog boundary",
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
                    "total_pages": max(1, ceil(group.total / PAGE_SIZE)),
                }
            )
        else:
            filters = [
                Document.collection_key == collection_key,
                Document.state.in_(RETRIEVAL_STATES),
                Document.language.in_(RETRIEVAL_LANGUAGES),
                Document.language_status.in_(RETRIEVAL_LANGUAGE_STATUSES),
            ]
            selected_language_value = language_value(selected_language)
            if selected_language_value is not None:
                filters.append(Document.language == selected_language_value)
            total = db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0
            documents = db.scalars(
                select(Document)
                .where(*filters)
                .order_by(Document.ingested_at.desc(), Document.uploaded_at.desc())
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
            ).all()
            context.update(
                {
                    "documents": [document_view(document) for document in documents],
                    "document_count": total,
                    "total_pages": max(1, ceil(total / PAGE_SIZE)),
                }
            )
    except ServiceError as exc:
        response_status = exc.status
        context.update(
            {
                "search_results": [],
                "search_error": str(exc),
                "result_count": 0,
            }
        )
    return PageResult("collection_detail.html", context, response_status)


def build_queue_page(
    state: WebRequestState,
    db: Session,
    *,
    status: str,
    collection: str,
    language: str,
    sort: str,
    order: str,
    page: int,
) -> PageResult:
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

    collection_keys = {item.key for item in state.settings.collections}
    selected_collection = collection if collection in collection_keys else "all"
    if selected_collection != "all":
        current_operations = [
            item
            for item in current_operations
            if item.document.collection_key == selected_collection
        ]

    selected_language = normalized_language(language, allow_undetermined=True)
    if selected_language != "all":
        current_operations = [
            item
            for item in current_operations
            if getattr(item.document.language, "value", item.document.language) == selected_language
        ]

    reverse = order == "desc"
    if sort == "filename":
        current_operations.sort(
            key=lambda item: item.document.normalized_filename,
            reverse=reverse,
        )
    elif sort == "status":
        current_operations.sort(
            key=lambda item: item.document.state.value,
            reverse=reverse,
        )
    else:
        current_operations.sort(key=lambda item: item.created_at, reverse=reverse)

    total = len(current_operations)
    start = (page - 1) * PAGE_SIZE
    visible = current_operations[start : start + PAGE_SIZE]
    last_claim_at = db.scalar(select(func.max(JobBatch.claimed_at)))
    context = base_context(state, active_page="queue")
    context.update(
        {
            "operations": [operation_view(item) for item in visible],
            "operation_count": total,
            "status_filter": status,
            "collection_filter": selected_collection,
            "language_filter": selected_language,
            "collections": configured_collections(state.settings),
            "sort": sort,
            "order": order,
            "page": page,
            "total_pages": max(1, ceil(total / PAGE_SIZE)),
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
    return PageResult("queue.html", context)


def build_review_page(
    state: WebRequestState,
    db: Session,
    *,
    collection: str,
    page: int,
) -> PageResult:
    configured = configured_collections(state.settings)
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
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    collection_map = {item["key"]: item for item in configured}
    views = []
    for document in documents:
        item = document_view(document)
        item["collection"] = collection_map.get(document.collection_key)
        views.append(item)

    context = base_context(state, active_page="review")
    context.update(
        {
            "documents": views,
            "review_count": total,
            "collections": configured,
            "collection_filter": selected_collection,
            "page": page,
            "total_pages": max(1, ceil(total / PAGE_SIZE)),
            "pagination_query": urlencode({"collection": selected_collection}),
        }
    )
    return PageResult("review.html", context)


async def build_upload_page(
    state: WebRequestState,
    *,
    collection: str,
    scanner_ping: ScannerPing,
) -> PageResult:
    settings = state.settings
    scanner_available = await asyncio.to_thread(
        scanner_ping,
        host=settings.clamd_host,
        port=settings.clamd_port,
        timeout=min(settings.clamd_timeout, 1.0),
    )
    collection_keys = {item.key for item in settings.collections}
    context = base_context(state, active_page="upload")
    context.update(
        {
            "max_file_count": settings.max_upload_files,
            "max_file_bytes": settings.max_upload_bytes,
            "max_file_size_display": f"{settings.max_upload_bytes / 1024 / 1024:.0f} MiB",
            "scanner_available": scanner_available,
            "collections": configured_collections(settings),
            "selected_collection": collection if collection in collection_keys else None,
        }
    )
    return PageResult("upload.html", context)


def build_document_page(
    state: WebRequestState,
    db: Session,
    *,
    document_id: UUID,
) -> PageResult:
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
        return error_page(
            state,
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
    configured = configured_collections(state.settings)
    collection_map = {item["key"]: item for item in configured}
    context = base_context(state, active_page=active_page)
    context.update(
        {
            "document": document_view(document),
            "collection": collection_map.get(document.collection_key),
            "collections": configured,
            "active_operation": (operation_view(active_operation) if active_operation else None),
            "audit_events": [audit_event_view(event) for event in document.audit_events],
            "pipeline_components": components_view(active_operation),
        }
    )
    return PageResult("document_detail.html", context)
