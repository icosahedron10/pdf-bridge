"""Thin coordinators for read-only catalog use cases."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    CollectionListResponse,
    DocumentDetail,
    DocumentListResponse,
    QueueListResponse,
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


def list_queue(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    collection_key: str | None,
    page: int,
    page_size: int,
) -> QueueListResponse:
    """Return a filtered page of the current operation queue."""

    return catalog.queue_list(
        session,
        definitions=definitions,
        collection_key=collection_key,
        page=page,
        page_size=page_size,
    )
