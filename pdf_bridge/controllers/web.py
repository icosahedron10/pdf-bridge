"""Litestar controllers for the server-rendered browser interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

from litestar import Request, Response, Router, get
from litestar.di import NamedDependency
from litestar.params import FromPath, FromQuery, QueryParameter
from litestar.response import Redirect, Template
from sqlalchemy.orm import Session

from pdf_bridge import __version__
from pdf_bridge.contracts.schemas import SearchMode
from pdf_bridge.http.security import csrf_token, get_actor
from pdf_bridge.managers import web
from pdf_bridge.presentation.theme import render_theme_css
from pdf_bridge.services.scanner import clamd_ping
from pdf_bridge.services.search import search_retrieval
from pdf_bridge.services.web_page import PageResult, WebRequestState

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"


def _request_state(request: Request) -> WebRequestState:
    actor = get_actor(request)
    return WebRequestState(
        request=request,
        settings=request.app.state.settings,
        csrf_token=csrf_token(request),
        actor_kind=actor.kind,
        actor_identifier=actor.identifier,
        app_version=__version__,
        search_http_client=getattr(request.app.state, "search_http_client", None),
    )


def _template_response(result: PageResult) -> Template:
    return Template(
        template_name=result.template_name,
        context=result.context,
        status_code=result.status_code,
    )


@get("/theme.css", sync_to_thread=False)
def theme_stylesheet(request: Request) -> Response[str]:
    """Render deployment brand settings as a no-cache stylesheet."""

    settings = request.app.state.settings
    stylesheet = web.get_theme_stylesheet(settings, renderer=render_theme_css)
    return Response(
        content=stylesheet,
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@get("/", sync_to_thread=False)
def index() -> Redirect:
    """Redirect the browser root to the collection library."""

    location = web.get_index_location()
    return Redirect(location, status_code=307)


@get("/library")
async def library_page(
    request: Request,
    db: NamedDependency[Session],
    q: Annotated[str, QueryParameter(max_length=1000)] = "",
    mode: FromQuery[SearchMode] = SearchMode.HYBRID,
) -> Template:
    """Render the collection library and optional cross-collection search."""

    state = _request_state(request)
    result = await web.get_library_page(
        state,
        db,
        query_value=q,
        mode=mode,
        search_retriever=search_retrieval,
    )
    return _template_response(result)


@get("/library/{collection_key:str}")
async def collection_page(
    request: Request,
    collection_key: FromPath[str],
    db: NamedDependency[Session],
    q: Annotated[str, QueryParameter(max_length=1000)] = "",
    mode: FromQuery[SearchMode] = SearchMode.HYBRID,
    page: Annotated[int, QueryParameter(ge=1)] = 1,
) -> Template:
    """Render one collection with browsed or searched documents."""

    state = _request_state(request)
    result = await web.get_collection_page(
        state,
        db,
        collection_key=collection_key,
        query_value=q,
        mode=mode,
        page=page,
        search_retriever=search_retrieval,
    )
    return _template_response(result)


@get("/queue", sync_to_thread=True)
def queue_page(
    request: Request,
    db: NamedDependency[Session],
    status: Annotated[str, QueryParameter(max_length=30)] = "all",
    collection: Annotated[str, QueryParameter(max_length=64)] = "all",
    sort: Annotated[str, QueryParameter(pattern="^(created_at|status|filename)$")] = "created_at",
    order: Annotated[str, QueryParameter(pattern="^(asc|desc)$")] = "asc",
    page: Annotated[int, QueryParameter(ge=1)] = 1,
) -> Template:
    """Render the filterable, sortable ingestion queue."""

    state = _request_state(request)
    result = web.get_queue_page(
        state,
        db,
        status=status,
        collection=collection,
        sort=sort,
        order=order,
        page=page,
    )
    return _template_response(result)


@get("/upload")
async def upload_page(
    request: Request,
    collection: Annotated[str, QueryParameter(max_length=64)] = "",
) -> Template:
    """Render upload controls with current scanner availability."""

    state = _request_state(request)
    result = await web.get_upload_page(
        state,
        collection=collection,
        scanner_ping=clamd_ping,
    )
    return _template_response(result)


@get("/documents/{document_id:uuid}", sync_to_thread=True)
def document_page(
    request: Request,
    document_id: FromPath[UUID],
    db: NamedDependency[Session],
) -> Template:
    """Render document metadata, processing status, and audit history."""

    state = _request_state(request)
    result = web.get_document_page(
        state,
        db,
        document_id=document_id,
    )
    return _template_response(result)


web_router = Router(
    path="",
    route_handlers=[
        theme_stylesheet,
        index,
        library_page,
        collection_page,
        queue_page,
        upload_page,
        document_page,
    ],
    include_in_schema=False,
)
router = web_router
