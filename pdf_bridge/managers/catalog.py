"""Thin coordinators for read-only catalog use cases."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    AnalysisDetailResponse,
    CollectionListResponse,
    DocumentDetail,
    DocumentListResponse,
    UploadListResponse,
    UploadResource,
)
from pdf_bridge.core.config import CollectionDefinition
from pdf_bridge.persistence.models import DocumentState
from pdf_bridge.services import catalog


def list_collections(
    session: Session, definitions: Sequence[CollectionDefinition]
) -> CollectionListResponse:
    """Return configured collections enriched with catalog counts."""

    return catalog.collection_list(session, definitions)


def list_documents(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    document_scope: Literal["library", "queue", "all"],
    document_state: DocumentState | None,
    collection_key: str | None,
    page: int,
    page_size: int,
) -> DocumentListResponse:
    """Return a validated, filtered page of catalog documents."""

    return catalog.document_list(
        session,
        definitions=definitions,
        document_scope=document_scope,
        document_state=document_state,
        collection_key=collection_key,
        page=page,
        page_size=page_size,
    )


def get_document(session: Session, document_id: UUID) -> DocumentDetail:
    """Return detailed catalog data for one document."""

    return catalog.document_detail(session, document_id)


def list_uploads(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    open_only: bool,
    collection_key: str | None,
    page: int,
    page_size: int,
) -> UploadListResponse:
    """Return a filtered page of durable upload workspace rows."""

    return catalog.upload_list(
        session,
        definitions=definitions,
        open_only=open_only,
        collection_key=collection_key,
        page=page,
        page_size=page_size,
    )


def get_upload(session: Session, upload_id: UUID) -> UploadResource:
    """Return one durable upload workspace row."""

    return catalog.upload_detail(session, upload_id)


def get_upload_analysis(
    session: Session,
    *,
    upload_id: UUID,
    page: int,
    page_size: int,
) -> AnalysisDetailResponse:
    """Return the current analysis with paginated candidate evidence."""

    return catalog.analysis_detail(
        session, upload_id=upload_id, page=page, page_size=page_size
    )
