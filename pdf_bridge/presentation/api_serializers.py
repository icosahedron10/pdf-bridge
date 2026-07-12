"""Stateless conversions from catalog models to public API schemas."""

from __future__ import annotations

from pdf_bridge.contracts.schemas import DocumentSummary, DuplicateMatch
from pdf_bridge.persistence.models import Document


def document_summary(document: Document) -> DocumentSummary:
    """Create the stable public summary for a catalog document."""

    return DocumentSummary.model_validate(document).model_copy(
        update={"detail_url": f"/documents/{document.id}"}
    )


def duplicate_match(document: Document) -> DuplicateMatch:
    """Create the duplicate-warning representation for a catalog document."""

    return DuplicateMatch(
        document_id=document.id,
        filename=document.original_filename,
        size_bytes=document.size_bytes,
        state=document.state,
        collection_key=document.collection_key,
        language=document.language,
        detail_url=f"/documents/{document.id}",
    )
