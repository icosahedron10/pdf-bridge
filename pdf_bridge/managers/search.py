"""Operator retrieval orchestration and catalog correlation."""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import OperatorSearchRequest, OperatorSearchResponse
from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.models import Document, DocumentState, PreparedRevision, RevisionStatus
from pdf_bridge.services.errors import ServiceError
from pdf_bridge.services.search import search_retrieval


def search_documents(
    session: Session,
    *,
    settings: Settings,
    request: OperatorSearchRequest,
    client: httpx.Client,
) -> OperatorSearchResponse:
    """Return only hits correlated to current READY catalog revisions."""

    response = search_retrieval(settings, request, client=client)
    if not response.results:
        return response

    document_ids = {hit.document_id for hit in response.results}
    revision_ids = {hit.prepared_revision_id for hit in response.results}
    documents = {
        document.id: document
        for document in session.scalars(
            select(Document).where(Document.id.in_(document_ids))
        ).all()
    }
    revisions = {
        revision.id: revision
        for revision in session.scalars(
            select(PreparedRevision).where(PreparedRevision.id.in_(revision_ids))
        ).all()
    }
    for hit in response.results:
        document = documents.get(hit.document_id)
        revision = revisions.get(hit.prepared_revision_id)
        if (
            document is None
            or document.state is not DocumentState.READY
            or document.collection_key != request.collection_key
            or document.original_filename != hit.original_filename
            or revision is None
            or revision.document_id != document.id
            or revision.status is not RevisionStatus.SEALED
            or revision.publication is None
        ):
            raise ServiceError(
                "The retrieval result no longer matches the READY catalog.",
                status=502,
                code="search_catalog_mismatch",
                title="Search response was invalid",
            )
    return response
