"""Thin orchestration layer for server-rendered web requests."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import SearchMode
from pdf_bridge.core.config import Settings
from pdf_bridge.services import web_page
from pdf_bridge.services.web_page import (
    PageResult,
    ScannerPing,
    SearchRetriever,
    ThemeRenderer,
    WebRequestState,
)


def get_theme_stylesheet(settings: Settings, *, renderer: ThemeRenderer) -> str:
    """Render the deployment theme through the presentation adapter."""

    return web_page.render_theme_stylesheet(settings, renderer=renderer)


def get_index_location() -> str:
    """Return the canonical browser landing location."""

    return web_page.index_location()


def get_library_page(
    state: WebRequestState,
    db: Session,
    *,
    query_value: str,
    mode: SearchMode,
    search_retriever: SearchRetriever,
) -> PageResult:
    """Build the collection library page context."""

    return web_page.build_library_page(
        state,
        db,
        query_value=query_value,
        mode=mode,
        search_retriever=search_retriever,
    )


def get_collection_page(
    state: WebRequestState,
    db: Session,
    *,
    collection_key: str,
    query_value: str,
    mode: SearchMode,
    page: int,
    search_retriever: SearchRetriever,
) -> PageResult:
    """Build one collection page with optional retrieval results."""

    return web_page.build_collection_page(
        state,
        db,
        collection_key=collection_key,
        query_value=query_value,
        mode=mode,
        page=page,
        search_retriever=search_retriever,
    )


def get_queue_page(
    state: WebRequestState,
    db: Session,
    *,
    status: str,
    collection: str,
    sort: str,
    order: str,
    page: int,
) -> PageResult:
    """Build the filtered and sorted queue page context."""

    return web_page.build_queue_page(
        state,
        db,
        status=status,
        collection=collection,
        sort=sort,
        order=order,
        page=page,
    )


def get_upload_page(
    state: WebRequestState,
    *,
    collection: str,
    scanner_ping: ScannerPing,
) -> PageResult:
    """Build upload page context including scanner readiness."""

    return web_page.build_upload_page(
        state,
        collection=collection,
        scanner_ping=scanner_ping,
    )


def get_document_page(
    state: WebRequestState,
    db: Session,
    *,
    document_id: UUID,
) -> PageResult:
    """Build the detail page context for a catalog document."""

    return web_page.build_document_page(
        state,
        db,
        document_id=document_id,
    )
