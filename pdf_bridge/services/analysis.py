"""Persistence steps of the analysis pipeline.

The worker manager sequences these steps and owns every commit; functions
here mutate and flush only. Heavy artifacts go to compressed private storage
through :mod:`pdf_bridge.services.artifacts`; bounded evidence lives in
normalized tables for review rendering and quote validation.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pdf_bridge.persistence.models import (
    AnalysisCandidate,
    AnalysisChunk,
    AnalysisStatus,
    CollectionEpoch,
    Document,
    DocumentAnalysis,
    DocumentArtifact,
    IndexAction,
    IndexOutboxEntry,
    IndexTarget,
    OutboxState,
    utc_now,
)
from pdf_bridge.services.artifacts import ArtifactRecord
from pdf_bridge.services.bm25 import SparseVectorData
from pdf_bridge.services.candidates import (
    MAX_CLASSIFIED_CANDIDATES,
    CandidateEvidence,
)
from pdf_bridge.services.chunking import Chunk
from pdf_bridge.services.classification import FindingResult, SourceExcerpt
from pdf_bridge.services.vector_index import ChunkPoint, point_id

MAX_EVIDENCE_EXCERPTS = 6


def create_analysis_revision(
    session: Session,
    document: Document,
    *,
    pipeline_fingerprint: str | None,
    epoch: int,
) -> DocumentAnalysis:
    """Open the next analysis revision for a document."""

    latest = session.scalar(
        select(DocumentAnalysis.revision)
        .where(DocumentAnalysis.document_id == document.id)
        .order_by(DocumentAnalysis.revision.desc())
        .limit(1)
    )
    analysis = DocumentAnalysis(
        document_id=document.id,
        revision=(latest or 0) + 1,
        status=AnalysisStatus.RUNNING,
        pipeline_fingerprint=pipeline_fingerprint,
        collection_epoch=epoch,
    )
    session.add(analysis)
    document.analysis_revision = analysis.revision
    session.flush()
    return analysis


def record_extraction(
    session: Session,
    analysis: DocumentAnalysis,
    document: Document,
    *,
    page_count: int,
    text_sha256: str,
    chunks: list[Chunk],
) -> None:
    """Persist chunk rows and document-level extraction results."""

    analysis.page_count = page_count
    analysis.chunk_count = len(chunks)
    analysis.text_sha256 = text_sha256
    document.page_count = page_count
    document.chunk_count = len(chunks)
    document.text_sha256 = text_sha256
    for chunk in chunks:
        session.add(
            AnalysisChunk(
                id=uuid.UUID(point_id(document.id, analysis.id, chunk.index)),
                analysis_id=analysis.id,
                document_id=document.id,
                chunk_index=chunk.index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                token_count=chunk.token_count,
                text_hash=chunk.text_hash,
                text=chunk.text,
            )
        )
    session.flush()


def record_artifact(
    session: Session, analysis: DocumentAnalysis, record: ArtifactRecord
) -> DocumentArtifact:
    """Record one immutable artifact owned by an analysis revision."""

    if record.analysis_id != analysis.id:
        raise ValueError("artifact record belongs to another analysis")
    if record.document_id != analysis.document_id:
        raise ValueError("artifact record belongs to another document")

    existing = session.scalar(
        select(DocumentArtifact).where(
            DocumentArtifact.analysis_id == analysis.id,
            DocumentArtifact.kind == record.kind,
        )
    )
    if existing is not None:
        if (
            existing.storage_key == record.storage_key
            and existing.sha256 == record.sha256
            and existing.size_bytes == record.size_bytes
        ):
            return existing
        raise ValueError(f"analysis artifact {record.kind!r} already has different metadata")
    artifact = DocumentArtifact(
        analysis_id=analysis.id,
        kind=record.kind,
        storage_key=record.storage_key,
        sha256=record.sha256,
        size_bytes=record.size_bytes,
    )
    session.add(artifact)
    session.flush()
    return artifact


def build_chunk_points(
    analysis: DocumentAnalysis,
    document: Document,
    chunks: list[Chunk],
    dense_vectors: list[list[float]],
    sparse_vectors: list[SparseVectorData],
) -> list[ChunkPoint]:
    """Pair chunks with their vectors as writable index points."""

    if not len(chunks) == len(dense_vectors) == len(sparse_vectors):
        raise ValueError("chunks and vectors lost correlation")
    return [
        ChunkPoint(
            document_id=document.id,
            analysis_id=analysis.id,
            chunk_index=chunk.index,
            collection_key=document.collection_key,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            text_hash=chunk.text_hash,
            text=chunk.text,
            dense=tuple(dense),
            sparse=sparse,
        )
        for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors, strict=True)
    ]


def enqueue_index_entry(
    session: Session,
    *,
    document: Document,
    analysis_id: uuid.UUID | None,
    target: IndexTarget,
    action: IndexAction,
    expected_points: int | None,
    collection_epoch: int | None = None,
) -> IndexOutboxEntry:
    """Append one ordered, epoch-pinned Qdrant mutation to the outbox.

    Analysis-backed writes inherit the exact physical collection epoch that
    produced their vectors. Other writes use the current logical collection
    epoch. Removal callers must name the physical epoch explicitly so cleanup
    cannot accidentally follow a mutable alias.
    """

    if action == IndexAction.DELETE and collection_epoch is None:
        raise ValueError("index deletion requires an explicit collection epoch")
    if collection_epoch is not None:
        resolved_epoch = collection_epoch
    elif analysis_id is not None:
        analysis = session.get(DocumentAnalysis, analysis_id)
        if analysis is None:
            raise ValueError("index mutation references a missing analysis")
        if analysis.document_id != document.id:
            raise ValueError("index mutation analysis belongs to another document")
        resolved_epoch = analysis.collection_epoch
    else:
        current = session.get(CollectionEpoch, document.collection_key)
        if current is None:
            current = CollectionEpoch(collection_key=document.collection_key, epoch=1)
            session.add(current)
            session.flush()
        resolved_epoch = current.epoch
    if resolved_epoch < 1:
        raise ValueError("index mutation collection epoch must be positive")

    entry = IndexOutboxEntry(
        document_id=document.id,
        analysis_id=analysis_id,
        collection_key=document.collection_key,
        collection_epoch=resolved_epoch,
        target=target,
        action=action,
        expected_points=expected_points,
        state=OutboxState.PENDING,
    )
    session.add(entry)
    session.flush()
    return entry


def pending_outbox_entries(
    session: Session, *, document_id: uuid.UUID | None = None
) -> list[IndexOutboxEntry]:
    """Return pending outbox entries in strict insertion order."""

    query = (
        select(IndexOutboxEntry)
        .where(IndexOutboxEntry.state == OutboxState.PENDING)
        .order_by(IndexOutboxEntry.id)
    )
    if document_id is not None:
        query = query.where(IndexOutboxEntry.document_id == document_id)
    return list(session.scalars(query).all())


def document_snapshot(document: Document) -> dict[str, Any]:
    """Freeze the candidate document fields needed to render evidence later."""

    return {
        "document_id": str(document.id),
        "filename": document.original_filename,
        "size_bytes": document.size_bytes,
        "state": document.state.value,
        "collection_key": document.collection_key,
        "uploaded_at": document.uploaded_at.isoformat(),
        "ingested_at": document.ingested_at.isoformat() if document.ingested_at else None,
        "analysis_revision": document.analysis_revision,
    }


def candidate_snapshot_analysis_id(
    session: Session, candidate: AnalysisCandidate
) -> uuid.UUID | None:
    """Resolve the candidate analysis captured by its immutable snapshot.

    New candidate snapshots record the exact document revision used during
    discovery. Older rows fall back to the latest retained revision, but every
    caller must still constrain chunks to the candidate document UUID.
    """

    snapshot = candidate.document_snapshot or {}
    revision = snapshot.get("analysis_revision")
    query = select(DocumentAnalysis.id).where(
        DocumentAnalysis.document_id == candidate.matched_document_id
    )
    if type(revision) is int and revision >= 1:
        query = query.where(DocumentAnalysis.revision == revision)
    else:
        query = query.order_by(DocumentAnalysis.revision.desc()).limit(1)
    return session.scalar(query)


def apply_candidates(
    session: Session,
    analysis: DocumentAnalysis,
    evaluated: list[CandidateEvidence],
    documents_by_id: dict[uuid.UUID, Document],
) -> list[AnalysisCandidate]:
    """Persist every qualified candidate, marking the classified top set.

    Candidates beyond the classification budget are persisted as overflow;
    their existence alone forces review.
    """

    records: list[AnalysisCandidate] = []
    for rank, item in enumerate(evaluated, start=1):
        matched = documents_by_id.get(item.document_id)
        if matched is None:
            continue
        record = AnalysisCandidate(
            analysis_id=analysis.id,
            matched_document_id=item.document_id,
            source=item.source,
            rank=rank,
            reasons=list(item.reasons),
            max_cosine=round(item.max_cosine, 6),
            strong_cosine_chunks=item.strong_cosine_chunks,
            moderate_cosine_chunks=item.moderate_cosine_chunks,
            bm25_strong_placements=item.bm25_strong_placements,
            fused_score=item.fused_score,
            classified=rank <= MAX_CLASSIFIED_CANDIDATES,
            overflow=rank > MAX_CLASSIFIED_CANDIDATES,
            matched_chunk_pairs=[[pair[0], pair[1]] for pair in item.matched_chunk_pairs[:50]],
            document_snapshot=document_snapshot(matched),
        )
        session.add(record)
        records.append(record)
    analysis.candidate_count = len(records)
    analysis.classified_count = sum(1 for record in records if record.classified)
    analysis.overflow_count = sum(1 for record in records if record.overflow)
    session.flush()
    return records


def record_finding(session: Session, candidate: AnalysisCandidate, result: FindingResult) -> None:
    """Persist one validated (or explicitly invalid) LLM finding."""

    from pdf_bridge.persistence.models import CandidateFindingRecord

    session.add(
        CandidateFindingRecord(
            candidate_id=candidate.id,
            role=result.role,
            model_id=result.model_id,
            valid=result.valid,
            label=result.finding.label if result.finding else None,
            summary=result.finding.summary if result.finding else None,
            evidence=(
                [item.model_dump() for item in result.finding.evidence] if result.finding else []
            ),
            error=result.error,
            attempts=result.attempts,
        )
    )
    session.flush()


def candidate_excerpts(
    session: Session,
    analysis: DocumentAnalysis,
    candidate: AnalysisCandidate,
) -> tuple[list[SourceExcerpt], list[SourceExcerpt]]:
    """Select bounded, page-referenced excerpts for one candidate pair.

    Uses the matched chunk pairs discovered during retrieval, falling back to
    each document's leading chunks so classification always has content.
    """

    incoming_indexes: list[int] = []
    candidate_chunk_ids: list[str] = []
    for pair in candidate.matched_chunk_pairs:
        if pair[0] not in incoming_indexes:
            incoming_indexes.append(int(pair[0]))
        if pair[1] not in candidate_chunk_ids:
            candidate_chunk_ids.append(str(pair[1]))

    incoming_chunks = list(
        session.scalars(
            select(AnalysisChunk)
            .where(AnalysisChunk.analysis_id == analysis.id)
            .order_by(AnalysisChunk.chunk_index)
        ).all()
    )
    if incoming_indexes:
        selected_incoming = [
            chunk for chunk in incoming_chunks if chunk.chunk_index in set(incoming_indexes)
        ]
    else:
        selected_incoming = incoming_chunks
    selected_incoming = selected_incoming[:MAX_EVIDENCE_EXCERPTS]

    candidate_analysis_id = candidate_snapshot_analysis_id(session, candidate)
    matched_chunks: list[AnalysisChunk] = []
    if candidate_chunk_ids:
        wanted: list[uuid.UUID] = []
        for chunk_id in candidate_chunk_ids:
            try:
                wanted.append(uuid.UUID(chunk_id))
            except ValueError:
                continue
        if wanted:
            correlated = list(
                session.scalars(
                    select(AnalysisChunk).where(
                        AnalysisChunk.id.in_(wanted),
                        AnalysisChunk.document_id == candidate.matched_document_id,
                        AnalysisChunk.analysis_id == candidate_analysis_id,
                    )
                ).all()
            )
            by_id = {chunk.id: chunk for chunk in correlated}
            matched_chunks = [by_id[chunk_id] for chunk_id in wanted if chunk_id in by_id]
        if len(matched_chunks) != len(wanted) or len(wanted) != len(candidate_chunk_ids):
            # A Qdrant payload referenced a missing, stale, or different
            # document's chunk. Do not substitute unrelated evidence.
            matched_chunks = []
    elif candidate_analysis_id is not None:
        matched_chunks = list(
            session.scalars(
                select(AnalysisChunk)
                .where(
                    AnalysisChunk.document_id == candidate.matched_document_id,
                    AnalysisChunk.analysis_id == candidate_analysis_id,
                )
                .order_by(AnalysisChunk.chunk_index)
                .limit(MAX_EVIDENCE_EXCERPTS)
            ).all()
        )
    matched_chunks = matched_chunks[:MAX_EVIDENCE_EXCERPTS]

    incoming_excerpts = [
        SourceExcerpt(
            reference=f"incoming:{chunk.chunk_index}",
            pages=f"{chunk.page_start}-{chunk.page_end}",
            text=chunk.text,
        )
        for chunk in selected_incoming
    ]
    matched_excerpts = [
        SourceExcerpt(
            reference=f"candidate:{chunk.id}",
            pages=f"{chunk.page_start}-{chunk.page_end}",
            text=chunk.text,
        )
        for chunk in matched_chunks
    ]
    return incoming_excerpts, matched_excerpts


def finalize_analysis(
    session: Session,
    analysis: DocumentAnalysis,
    *,
    filename_warnings: list[dict[str, Any]],
    semantic_complete: bool,
    classification_complete: bool,
    incomplete_reasons: list[str],
    screening_indexed: bool,
) -> None:
    """Complete an analysis and derive its automatic-ingestion eligibility.

    A clear analysis — complete checks and zero qualifying candidates —
    ingests automatically; anything else requires an explicit decision.
    """

    analysis.filename_warnings = filename_warnings
    analysis.semantic_complete = semantic_complete
    analysis.classification_complete = classification_complete
    analysis.incomplete_reasons = incomplete_reasons
    analysis.screening_indexed = screening_indexed
    analysis.status = AnalysisStatus.COMPLETE
    analysis.completed_at = utc_now()
    analysis.auto_ingest_eligible = (
        semantic_complete
        and classification_complete
        and analysis.candidate_count == 0
        and not filename_warnings
    )
    session.flush()


def fail_analysis(session: Session, analysis: DocumentAnalysis) -> None:
    """Mark an analysis revision failed (terminal for this revision)."""

    analysis.status = AnalysisStatus.FAILED
    analysis.completed_at = utc_now()
    session.flush()
