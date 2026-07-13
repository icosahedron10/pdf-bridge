"""Business and page-data services for the server-rendered web interface."""

from __future__ import annotations

from collections.abc import Callable
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
    QueueOperation,
)
from pdf_bridge.presentation.view_models import (
    audit_event_view,
    components_view,
    document_view,
    operation_view,
)
from pdf_bridge.services import catalog
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.lifecycle import ACTIVE_DOCUMENT_STATES

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
PROCESSING_STATES = tuple(
    state
    for state in ACTIVE_DOCUMENT_STATES
    if state != DocumentState.INGESTED
)
PAGE_SIZE = 25

SearchRetriever = Callable[..., SearchResponse]
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
    """Convert a configured collection definition to template data."""

    return {
        "key": definition.key,
        "display_name": definition.display_name,
        "description": definition.description,
        "audience": definition.audience,
    }


def configured_collections(settings: Settings) -> list[dict[str, Any]]:
    """Return template mappings for every configured collection."""

    return [collection_view(item) for item in settings.collections]


def collection_counts(db: Session) -> dict[str, dict[str, int]]:
    """Aggregate collection availability and processing counts."""

    counts: dict[str, dict[str, int]] = {}
    rows = db.execute(
        select(
            Document.collection_key,
            Document.state,
            func.count(),
        ).group_by(
            Document.collection_key,
            Document.state,
        )
    ).all()
    # One grouped query feeds every collection card using the same lifecycle
    # boundary as retrieval rather than raw document totals.
    for collection_key, state, count in rows:
        item = counts.setdefault(
            collection_key,
            {
                "available": 0,
                "processing": 0,
            },
        )
        if state in catalog.RETRIEVAL_STATES:
            item["available"] += count
        if state in PROCESSING_STATES:
            item["processing"] += count
    return counts


def base_context(state: WebRequestState, *, active_page: str) -> dict[str, Any]:
    """Build template context shared by every browser page."""

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
    """Build a framework-neutral error page response."""

    context = base_context(state, active_page="")
    context.update({"status_code": status_code, "title": title, "message": message})
    return PageResult("error.html", context, status_code)


def render_theme_stylesheet(settings: Settings, *, renderer: ThemeRenderer) -> str:
    """Render theme CSS through the injected presentation function."""

    return renderer(settings)


def index_location() -> str:
    """Return the canonical browser landing path."""

    return "/library"


def build_library_page(
    state: WebRequestState,
    db: Session,
    *,
    query_value: str,
    mode: SearchMode,
    search_retriever: SearchRetriever,
) -> PageResult:
    """Build collection cards and optional correlated search totals."""

    response_status = 200
    query = query_value.strip()
    collections = configured_collections(state.settings)
    counts_by_collection = collection_counts(db)
    for collection in collections:
        collection.update(counts_by_collection.get(collection["key"], {}))
        collection.setdefault("available", 0)
        collection.setdefault("processing", 0)
        collection["search_total"] = None
        collection["href"] = "/library/" + collection["key"]

    context = base_context(state, active_page="library")
    context.update(
        {
            "collections": collections,
            "search_query": query,
            "search_mode": mode.value,
        }
    )

    if query:
        try:
            search_request = SearchRequest(
                query=query,
                mode=mode,
                collections=[item["key"] for item in collections],
                include_hits=False,
                page=1,
                page_size=1,
            )
            search_response = search_retriever(
                state.settings,
                search_request,
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
            catalog.validate_search_response(db, search_request, search_response)

            query_string = urlencode(
                {
                    "q": query,
                    "mode": mode.value,
                }
            )
            for collection in collections:
                collection["search_total"] = groups[collection["key"]].total
                collection["href"] = f"/library/{collection['key']}?{query_string}"
        except ServiceError as exc:
            context["search_error"] = str(exc)
            response_status = exc.status
    return PageResult("library.html", context, response_status)


def build_collection_page(
    state: WebRequestState,
    db: Session,
    *,
    collection_key: str,
    query_value: str,
    mode: SearchMode,
    page: int,
    search_retriever: SearchRetriever,
) -> PageResult:
    """Build one collection page from browsing or external retrieval results."""

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
    context = base_context(state, active_page="library")
    response_status = 200
    context.update(
        {
            "collection": collection,
            "search_query": query,
            "search_mode": mode.value,
            "page": page,
            "total_pages": 1,
            "pagination_query": urlencode(
                {
                    **({"q": query, "mode": mode.value} if query else {}),
                }
            ),
        }
    )

    # Search hits are correlated back to eligible catalog rows; an empty query
    # stays entirely local and uses deterministic catalog pagination.
    try:
        if query:
            search_request = SearchRequest(
                query=query,
                mode=mode,
                collections=[collection_key],
                include_hits=True,
                page=page,
                page_size=PAGE_SIZE,
            )
            search_response = search_retriever(
                state.settings,
                search_request,
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
            catalog.validate_search_response(db, search_request, search_response)
            hit_ids = [hit.document_id for hit in group.hits]
            statement = select(Document).where(
                Document.id.in_(hit_ids),
                *catalog.retrieval_catalog_filters(collection_key=collection_key),
            )
            documents = db.scalars(statement).all() if hit_ids else []
            documents_by_id = {document.id: document for document in documents}
            if any(document_id not in documents_by_id for document_id in hit_ids):
                raise ServiceError(
                    (
                        "The retrieval service returned an inactive document or a document from "
                        "another collection. No partial results were shown."
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
            filters = catalog.retrieval_catalog_filters(collection_key=collection_key)
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
    sort: str,
    order: str,
    page: int,
) -> PageResult:
    """Build the current queue view with filters, sorting, and pagination."""

    operations = db.scalars(
        select(QueueOperation)
        .join(QueueOperation.document)
        .options(joinedload(QueueOperation.document))
        .where(Document.state.in_(QUEUE_STATES))
    ).all()

    # Retries leave historical attempts in the ledger, while the queue presents
    # only the latest operation for each active document.
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
            "collections": configured_collections(state.settings),
            "sort": sort,
            "order": order,
            "page": page,
            "total_pages": max(1, ceil(total / PAGE_SIZE)),
            "pagination_query": urlencode(
                {
                    "status": status,
                    "collection": selected_collection,
                    "sort": sort,
                    "order": order,
                }
            ),
            "last_claim_at": last_claim_at,
        }
    )
    return PageResult("queue.html", context)


def build_upload_page(
    state: WebRequestState,
    *,
    collection: str,
    scanner_ping: ScannerPing,
) -> PageResult:
    """Build upload limits, collection choices, and scanner readiness context."""

    settings = state.settings
    scanner_available = scanner_ping(
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
    """Build a document detail page with its latest operation and audit timeline."""

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
    if document.state in LIBRARY_STATES:
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
