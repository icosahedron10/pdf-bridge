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
    return web_page.render_theme_stylesheet(settings, renderer=renderer)


def get_index_location() -> str:
    return web_page.index_location()


async def get_library_page(
    state: WebRequestState,
    db: Session,
    *,
    query_value: str,
    mode: SearchMode,
    language: str,
    search_retriever: SearchRetriever,
) -> PageResult:
    return await web_page.build_library_page(
        state,
        db,
        query_value=query_value,
        mode=mode,
        language=language,
        search_retriever=search_retriever,
    )


async def get_collection_page(
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
    return await web_page.build_collection_page(
        state,
        db,
        collection_key=collection_key,
        query_value=query_value,
        mode=mode,
        language=language,
        page=page,
        search_retriever=search_retriever,
    )


def get_queue_page(
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
    return web_page.build_queue_page(
        state,
        db,
        status=status,
        collection=collection,
        language=language,
        sort=sort,
        order=order,
        page=page,
    )


def get_review_page(
    state: WebRequestState,
    db: Session,
    *,
    collection: str,
    page: int,
) -> PageResult:
    return web_page.build_review_page(
        state,
        db,
        collection=collection,
        page=page,
    )


async def get_upload_page(
    state: WebRequestState,
    *,
    collection: str,
    scanner_ping: ScannerPing,
) -> PageResult:
    return await web_page.build_upload_page(
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
    return web_page.build_document_page(
        state,
        db,
        document_id=document_id,
    )
