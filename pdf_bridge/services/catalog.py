"""Catalog queries and catalog-boundary validation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from pdf_bridge.contracts.schemas import (
    CollectionListResponse,
    CollectionSummary,
    DocumentDetail,
    DocumentListResponse,
    QueueListResponse,
    QueueOperationSummary,
    SearchRequest,
    SearchResponse,
)
from pdf_bridge.core.config import CollectionDefinition
from pdf_bridge.persistence.models import (
    Document,
    DocumentState,
    QueueOperation,
)
from pdf_bridge.presentation.api_serializers import document_summary
from pdf_bridge.services.errors import ServiceError

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
def _active_document_states() -> tuple[DocumentState, ...]:
    # Import lazily so catalog initialization stays independent of lifecycle setup.
    from pdf_bridge.services.lifecycle import ACTIVE_DOCUMENT_STATES

    return ACTIVE_DOCUMENT_STATES


def retrieval_catalog_filters(*, collection_key: str | None = None) -> list:
    """Build the common retrieval-eligibility SQL predicates."""

    filters = [Document.state.in_(RETRIEVAL_STATES)]
    if collection_key is not None:
        filters.append(Document.collection_key == collection_key)
    return filters


def configured_collection(
    definitions: Sequence[CollectionDefinition], collection_key: str
) -> CollectionDefinition:
    """Resolve a deployment collection or fail with a stable domain error."""

    collection = next((item for item in definitions if item.key == collection_key), None)
    if collection is None:
        raise ServiceError(
            "Choose one of the collections configured for this PDF Bridge deployment.",
            status=422,
            code="collection-not-configured",
            title="Collection was rejected",
        )
    return collection


def collection_list(
    session: Session, definitions: Sequence[CollectionDefinition]
) -> CollectionListResponse:
    """Return configured collections enriched with authoritative catalog counts."""

    processing_states = tuple(
        state
        for state in _active_document_states()
        if state != DocumentState.INGESTED
    )
    items: list[CollectionSummary] = []
    for definition in definitions:
        available = (
            session.scalar(
                select(func.count())
                .select_from(Document)
                .where(*retrieval_catalog_filters(collection_key=definition.key))
            )
            or 0
        )
        processing = (
            session.scalar(
                select(func.count())
                .select_from(Document)
                .where(
                    Document.collection_key == definition.key,
                    Document.state.in_(processing_states),
                )
            )
            or 0
        )
        items.append(
            CollectionSummary(
                key=definition.key,
                display_name=definition.display_name,
                description=definition.description,
                audience=definition.audience,
                available_documents=available,
                processing_documents=processing,
                detail_url=f"/library/{definition.key}",
            )
        )
    return CollectionListResponse(items=items, total=len(items))


def document_list(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    document_scope: Literal["library", "queue", "all"],
    document_state: DocumentState | None,
    collection_key: str | None,
    page: int,
    page_size: int,
) -> DocumentListResponse:
    """Query one page of catalog documents for the public API."""

    filters = []
    if collection_key is not None:
        configured_collection(definitions, collection_key)
        filters.append(Document.collection_key == collection_key)
    if document_state is not None:
        filters.append(Document.state == document_state)
    elif document_scope == "library":
        filters.extend(retrieval_catalog_filters())
    elif document_scope == "queue":
        filters.append(
            Document.state.in_(
                tuple(
                    item
                    for item in _active_document_states()
                    if item != DocumentState.INGESTED
                )
            )
        )

    total = session.scalar(select(func.count()).select_from(Document).where(*filters)) or 0
    documents = session.scalars(
        select(Document)
        .where(*filters)
        .order_by(Document.uploaded_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return DocumentListResponse.create(
        [document_summary(document) for document in documents],
        total=total,
        page=page,
        page_size=page_size,
    )


def document_detail_record(session: Session, document_id: UUID) -> Document:
    """Load a document with operations and audit events."""

    document = (
        session.execute(
            select(Document)
            .where(Document.id == document_id)
            .options(joinedload(Document.operations), joinedload(Document.audit_events))
        )
        .unique()
        .scalar_one_or_none()
    )
    if document is None:
        raise ServiceError(
            "No catalog record exists for this document ID.",
            status=404,
            code="document-not-found",
            title="Document not found",
        )
    return document


def document_detail(session: Session, document_id: UUID) -> DocumentDetail:
    """Return the public detailed representation of one catalog document."""

    document = document_detail_record(session, document_id)
    return DocumentDetail.model_validate(document).model_copy(
        update={"detail_url": f"/documents/{document.id}"}
    )


def queue_list(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    collection_key: str | None,
    page: int,
    page_size: int,
) -> QueueListResponse:
    """Return the latest active queue operation for each matching document."""

    filters = [
        Document.state.in_(
            tuple(
                item
                for item in _active_document_states()
                if item != DocumentState.INGESTED
            )
        )
    ]
    if collection_key is not None:
        configured_collection(definitions, collection_key)
        filters.append(Document.collection_key == collection_key)
    query = (
        select(QueueOperation)
        .join(QueueOperation.document)
        .where(*filters)
        .options(joinedload(QueueOperation.document))
    )
    latest: dict[UUID, QueueOperation] = {}
    for operation in session.scalars(query).all():
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
                update={"document": document_summary(operation.document)}
            )
            for operation in visible
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


def available_document_count(session: Session, *, collection_key: str) -> int:
    """Count retrieval-eligible documents in a collection."""

    return (
        session.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                *retrieval_catalog_filters(collection_key=collection_key)
            )
        )
        or 0
    )


def validate_search_response(
    session: Session, request: SearchRequest, response: SearchResponse
) -> None:
    """Fail closed when retrieval results cross catalog boundaries."""

    for group in response.groups:
        ids = [hit.document_id for hit in group.hits]
        documents = (
            session.scalars(
                select(Document).where(
                    Document.id.in_(ids),
                    *retrieval_catalog_filters(),
                )
            ).all()
            if ids
            else []
        )
        documents_by_id = {document.id: document for document in documents}
        invalid_hit = any(
            document_id not in documents_by_id
            or documents_by_id[document_id].collection_key != group.collection_key
            for document_id in ids
        )
        catalog_total = available_document_count(
            session,
            collection_key=group.collection_key,
        )
        if invalid_hit or group.total > catalog_total:
            raise ServiceError(
                (
                    "The retrieval response included a document or total outside its requested "
                    "collection boundary. No partial results were returned."
                ),
                status=502,
                code="search-catalog-mismatch",
                title="Search and catalog are out of sync",
            )


def validate_configured_collections(
    definitions: Sequence[CollectionDefinition], collection_keys: Iterable[str]
) -> None:
    """Require every supplied collection key to be configured."""

    for collection_key in collection_keys:
        configured_collection(definitions, collection_key)
