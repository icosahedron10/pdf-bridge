"""Stateless conversions from catalog models to public API schemas."""

from __future__ import annotations

from typing import Any

from pdf_bridge.contracts.schemas import (
    AnalysisSummary,
    CandidatePublic,
    ChunkExcerptPublic,
    DecisionSummary,
    DocumentSummary,
    DuplicateMatch,
    FilenameWarningPublic,
    FindingPublic,
    OperationSummary,
    ReplacementSummary,
    UploadResource,
)
from pdf_bridge.persistence.models import (
    OPEN_UPLOAD_STATES,
    AnalysisCandidate,
    AnalysisChunk,
    CandidateFindingRecord,
    Document,
    DocumentAnalysis,
    DocumentState,
    IntakeDecision,
    OperationState,
    ReplacementState,
    ReplacementWorkflow,
    WorkOperation,
)


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
        detail_url=f"/documents/{document.id}",
    )


def duplicate_match_from_snapshot(snapshot: dict[str, Any]) -> DuplicateMatch:
    """Rebuild a duplicate match from a frozen analysis-time snapshot."""

    return DuplicateMatch(
        document_id=snapshot["document_id"],
        filename=snapshot["filename"],
        size_bytes=snapshot.get("size_bytes", 0),
        state=DocumentState(snapshot["state"]),
        collection_key=snapshot["collection_key"],
        detail_url=f"/documents/{snapshot['document_id']}",
    )


def filename_warning(document: Document, match: Any) -> FilenameWarningPublic:
    """Serialize a live filename comparison result (a FilenameMatch shape)."""

    return FilenameWarningPublic(
        kind=match.kind,
        similarity=min(match.similarity, 1.0),
        shared_tokens=list(match.shared_family_tokens),
        matched=duplicate_match(document),
    )


def filename_warning_from_stored(payload: dict[str, Any]) -> FilenameWarningPublic:
    """Rebuild a stored analysis filename warning."""

    return FilenameWarningPublic(
        kind=payload["kind"],
        similarity=min(float(payload["similarity"]), 1.0),
        shared_tokens=list(payload.get("shared_tokens", [])),
        matched=duplicate_match_from_snapshot(payload["matched"]),
    )


def operation_summary(operation: WorkOperation) -> OperationSummary:
    """Serialize one durable worker operation."""

    return OperationSummary.model_validate(operation)


def analysis_summary(analysis: DocumentAnalysis) -> AnalysisSummary:
    """Serialize one analysis revision's completeness and results."""

    return AnalysisSummary(
        id=analysis.id,
        revision=analysis.revision,
        status=analysis.status,
        pipeline_fingerprint=analysis.pipeline_fingerprint,
        page_count=analysis.page_count,
        chunk_count=analysis.chunk_count,
        filename_warnings=[
            filename_warning_from_stored(item) for item in analysis.filename_warnings
        ],
        semantic_complete=analysis.semantic_complete,
        classification_complete=analysis.classification_complete,
        incomplete_reasons=list(analysis.incomplete_reasons),
        auto_ingest_eligible=analysis.auto_ingest_eligible,
        candidate_count=analysis.candidate_count,
        classified_count=analysis.classified_count,
        overflow_count=analysis.overflow_count,
        created_at=analysis.created_at,
        completed_at=analysis.completed_at,
    )


def decision_summary(decision: IntakeDecision) -> DecisionSummary:
    """Serialize one immutable intake decision."""

    return DecisionSummary.model_validate(decision)


def replacement_summary(workflow: ReplacementWorkflow) -> ReplacementSummary:
    """Serialize the progress of one replacement workflow."""

    return ReplacementSummary.model_validate(workflow)


def upload_resource(
    document: Document,
    *,
    operation: WorkOperation | None,
    analysis: DocumentAnalysis | None,
    replacement: ReplacementWorkflow | None,
    decision: IntakeDecision | None,
) -> UploadResource:
    """Assemble the durable upload workspace representation."""

    operation_open = operation is not None and operation.state in (
        OperationState.QUEUED,
        OperationState.RUNNING,
    )
    replacement_open = replacement is not None and replacement.state not in (
        ReplacementState.SUCCEEDED,
        ReplacementState.FAILED,
    )
    return UploadResource(
        upload_id=document.id,
        document=document_summary(document),
        operation=operation_summary(operation) if operation else None,
        analysis=analysis_summary(analysis) if analysis else None,
        replacement=replacement_summary(replacement) if replacement else None,
        decision=decision_summary(decision) if decision else None,
        review_required=document.state == DocumentState.REVIEW_REQUIRED,
        open=(
            document.state in OPEN_UPLOAD_STATES
            or operation_open
            or replacement_open
        ),
        status_url=f"/api/v1/uploads/{document.id}",
        analysis_url=(
            f"/api/v1/uploads/{document.id}/analysis" if analysis is not None else None
        ),
    )


def chunk_excerpt(chunk: AnalysisChunk, *, reference: str) -> ChunkExcerptPublic:
    """Serialize one page-referenced chunk excerpt for evidence rendering."""

    return ChunkExcerptPublic(
        chunk_reference=reference,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        text=chunk.text[:4000],
    )


def finding_public(record: CandidateFindingRecord) -> FindingPublic:
    """Serialize one validated LLM finding."""

    return FindingPublic(
        role=record.role,
        model_id=record.model_id,
        valid=record.valid,
        label=record.label,
        summary=record.summary,
        evidence=list(record.evidence),
        error=record.error,
    )


def candidate_public(
    candidate: AnalysisCandidate,
    *,
    replacement_eligible: bool,
    incoming_excerpts: list[ChunkExcerptPublic],
    candidate_excerpts: list[ChunkExcerptPublic],
) -> CandidatePublic:
    """Serialize one qualifying candidate with its evidence."""

    return CandidatePublic(
        candidate_id=candidate.id,
        document=duplicate_match_from_snapshot(candidate.document_snapshot),
        source=candidate.source,
        rank=candidate.rank,
        reasons=list(candidate.reasons),
        max_cosine=candidate.max_cosine,
        strong_cosine_chunks=candidate.strong_cosine_chunks,
        moderate_cosine_chunks=candidate.moderate_cosine_chunks,
        bm25_strong_placements=candidate.bm25_strong_placements,
        fused_score=candidate.fused_score,
        classified=candidate.classified,
        overflow=candidate.overflow,
        replacement_eligible=replacement_eligible,
        findings=[finding_public(record) for record in candidate.findings],
        incoming_excerpts=incoming_excerpts,
        candidate_excerpts=candidate_excerpts,
    )
