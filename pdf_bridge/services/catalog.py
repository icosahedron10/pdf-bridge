"""Catalog queries and catalog-boundary validation."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from pdf_bridge.contracts.schemas import (
    AnalysisDetailResponse,
    ChunkExcerptPublic,
    CollectionListResponse,
    CollectionSummary,
    DocumentDetail,
    DocumentListResponse,
    SearchRequest,
    SearchResponse,
    UploadListResponse,
    UploadResource,
)
from pdf_bridge.core.config import CollectionDefinition
from pdf_bridge.persistence.models import (
    OPEN_UPLOAD_STATES,
    AnalysisCandidate,
    AnalysisChunk,
    Document,
    DocumentState,
    IntakeDecision,
    OperationState,
    ReplacementState,
    ReplacementWorkflow,
    WorkOperation,
)
from pdf_bridge.presentation.api_serializers import (
    analysis_summary,
    candidate_public,
    chunk_excerpt,
    decision_summary,
    document_summary,
    operation_summary,
    upload_resource,
)
from pdf_bridge.services.analysis import candidate_snapshot_analysis_id
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.intake import (
    latest_analysis,
    latest_operation,
    replacement_target_issue,
)

LIBRARY_STATES = (
    DocumentState.INGESTED,
    DocumentState.DELETING,
    DocumentState.DELETE_FAILED,
)
RETRIEVAL_STATES = (
    DocumentState.INGESTED,
    DocumentState.DELETING,
    DocumentState.DELETE_FAILED,
)
MAX_EXCERPTS_PER_SIDE = 6


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
                    Document.state.in_(OPEN_UPLOAD_STATES),
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
        filters.append(Document.state.in_(LIBRARY_STATES))
    elif document_scope == "queue":
        filters.append(Document.state.in_(OPEN_UPLOAD_STATES))

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


def _upload_resource_for(session: Session, document: Document) -> UploadResource:
    operation = latest_operation(session, document.id)
    analysis = latest_analysis(session, document.id)
    replacement = session.scalar(
        select(ReplacementWorkflow).where(
            ReplacementWorkflow.new_document_id == document.id
        )
    )
    decision = session.scalar(
        select(IntakeDecision)
        .where(IntakeDecision.document_id == document.id)
        .order_by(IntakeDecision.created_at.desc())
        .limit(1)
    )
    return upload_resource(
        document,
        operation=operation,
        analysis=analysis,
        replacement=replacement,
        decision=decision,
    )


def upload_list(
    session: Session,
    *,
    definitions: Sequence[CollectionDefinition],
    open_only: bool,
    collection_key: str | None,
    page: int,
    page_size: int,
) -> UploadListResponse:
    """Return one page of durable upload workspace rows."""

    filters = []
    if collection_key is not None:
        configured_collection(definitions, collection_key)
        filters.append(Document.collection_key == collection_key)
    if open_only:
        open_operation = (
            select(WorkOperation.id)
            .where(
                WorkOperation.document_id == Document.id,
                WorkOperation.state.in_(
                    (OperationState.QUEUED, OperationState.RUNNING)
                ),
            )
            .exists()
        )
        open_replacement = (
            select(ReplacementWorkflow.id)
            .where(
                ReplacementWorkflow.new_document_id == Document.id,
                ReplacementWorkflow.state.not_in(
                    (ReplacementState.SUCCEEDED, ReplacementState.FAILED)
                ),
            )
            .exists()
        )
        filters.append(
            or_(
                Document.state.in_(OPEN_UPLOAD_STATES),
                open_operation,
                open_replacement,
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
    return UploadListResponse.create(
        [_upload_resource_for(session, document) for document in documents],
        total=total,
        page=page,
        page_size=page_size,
    )


def upload_detail(session: Session, upload_id: UUID) -> UploadResource:
    """Return one durable upload workspace row."""

    document = session.get(Document, upload_id)
    if document is None:
        raise ServiceError(
            "No upload exists for this ID.",
            status=404,
            code="upload-not-found",
            title="Upload not found",
        )
    return _upload_resource_for(session, document)


def _candidate_excerpt_pairs(
    session: Session,
    candidate: AnalysisCandidate,
) -> tuple[list[ChunkExcerptPublic], list[ChunkExcerptPublic]]:
    incoming_indexes: list[int] = []
    matched_ids: list[uuid.UUID] = []
    for pair in candidate.matched_chunk_pairs:
        index = int(pair[0])
        if index not in incoming_indexes:
            incoming_indexes.append(index)
        try:
            chunk_id = uuid.UUID(str(pair[1]))
        except ValueError:
            continue
        if chunk_id not in matched_ids:
            matched_ids.append(chunk_id)

    incoming_query = (
        select(AnalysisChunk)
        .where(AnalysisChunk.analysis_id == candidate.analysis_id)
        .order_by(AnalysisChunk.chunk_index)
    )
    if incoming_indexes:
        incoming_query = incoming_query.where(
            AnalysisChunk.chunk_index.in_(incoming_indexes[:MAX_EXCERPTS_PER_SIDE])
        )
    incoming_chunks = list(
        session.scalars(incoming_query.limit(MAX_EXCERPTS_PER_SIDE)).all()
    )

    candidate_analysis_id = candidate_snapshot_analysis_id(session, candidate)
    matched_chunks: list[AnalysisChunk] = []
    if matched_ids:
        correlated = list(
            session.scalars(
                select(AnalysisChunk).where(
                    AnalysisChunk.id.in_(matched_ids[:MAX_EXCERPTS_PER_SIDE]),
                    AnalysisChunk.document_id == candidate.matched_document_id,
                    AnalysisChunk.analysis_id == candidate_analysis_id,
                )
            ).all()
        )
        by_id = {chunk.id: chunk for chunk in correlated}
        matched_chunks = [
            by_id[chunk_id]
            for chunk_id in matched_ids[:MAX_EXCERPTS_PER_SIDE]
            if chunk_id in by_id
        ]
    if candidate.matched_chunk_pairs and len(matched_chunks) != len(
        matched_ids[:MAX_EXCERPTS_PER_SIDE]
    ):
        # Never replace a stale or cross-document Qdrant reference with text
        # from an unrelated chunk.
        matched_chunks = []
    elif not candidate.matched_chunk_pairs and candidate_analysis_id is not None:
        matched_chunks = list(
            session.scalars(
                select(AnalysisChunk)
                .where(
                    AnalysisChunk.document_id == candidate.matched_document_id,
                    AnalysisChunk.analysis_id == candidate_analysis_id,
                )
                .order_by(AnalysisChunk.chunk_index)
                .limit(MAX_EXCERPTS_PER_SIDE)
            ).all()
        )
    return (
        [
            chunk_excerpt(chunk, reference=f"incoming:{chunk.chunk_index}")
            for chunk in incoming_chunks
        ],
        [chunk_excerpt(chunk, reference=f"candidate:{chunk.id}") for chunk in matched_chunks],
    )


def analysis_detail(
    session: Session,
    *,
    upload_id: UUID,
    page: int,
    page_size: int,
) -> AnalysisDetailResponse:
    """Return the current analysis with one page of candidate evidence."""

    document = session.get(Document, upload_id)
    if document is None:
        raise ServiceError(
            "No upload exists for this ID.",
            status=404,
            code="upload-not-found",
            title="Upload not found",
        )
    analysis = latest_analysis(session, document.id)
    if analysis is None:
        raise ServiceError(
            "This upload has no analysis yet.",
            status=404,
            code="analysis-not-found",
            title="Analysis not found",
        )
    total = (
        session.scalar(
            select(func.count())
            .select_from(AnalysisCandidate)
            .where(AnalysisCandidate.analysis_id == analysis.id)
        )
        or 0
    )
    candidates = session.scalars(
        select(AnalysisCandidate)
        .where(AnalysisCandidate.analysis_id == analysis.id)
        .options(joinedload(AnalysisCandidate.findings))
        .order_by(AnalysisCandidate.rank)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).unique().all()

    rendered = []
    for candidate in candidates:
        live_target = session.get(Document, candidate.matched_document_id)
        replacement_eligible = (
            live_target is not None
            and replacement_target_issue(
                session,
                live_target,
                collection_key=document.collection_key,
            )
            is None
        )
        incoming_excerpts, candidate_excerpts = _candidate_excerpt_pairs(session, candidate)
        rendered.append(
            candidate_public(
                candidate,
                replacement_eligible=replacement_eligible,
                incoming_excerpts=incoming_excerpts,
                candidate_excerpts=candidate_excerpts,
            )
        )
    return AnalysisDetailResponse(
        upload_id=document.id,
        analysis=analysis_summary(analysis),
        candidates=rendered,
        total_candidates=total,
        page=page,
        page_size=page_size,
        pages=math.ceil(total / page_size) if total else 0,
    )


def document_detail_record(session: Session, document_id: UUID) -> Document:
    """Load a document with operations and audit events."""

    document = (
        session.execute(
            select(Document)
            .where(Document.id == document_id)
            .options(
                joinedload(Document.operations),
                joinedload(Document.audit_events),
                joinedload(Document.decisions),
            )
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
    analysis = latest_analysis(session, document.id)
    detail = DocumentDetail.model_validate(document).model_copy(
        update={
            "detail_url": f"/documents/{document.id}",
            "analysis": analysis_summary(analysis) if analysis else None,
            "decisions": [decision_summary(item) for item in document.decisions],
            "operations": [operation_summary(item) for item in document.operations],
        }
    )
    return detail


def available_document_count(session: Session, *, collection_key: str) -> int:
    """Count retrieval-eligible documents in a collection."""

    return (
        session.scalar(
            select(func.count())
            .select_from(Document)
            .where(*retrieval_catalog_filters(collection_key=collection_key))
        )
        or 0
    )


def validate_search_response(
    session: Session, request: SearchRequest, response: SearchResponse
) -> None:
    """Fail closed when retrieval results cross catalog boundaries.

    Pending, screening-only, and tombstoned content must never surface:
    every hit must belong to a retrieval-eligible document in the requested
    collection.
    """

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
