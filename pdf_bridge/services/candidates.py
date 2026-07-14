"""Deterministic candidate discovery rules and rank fusion.

This module is pure: it evaluates per-chunk retrieval results that were
gathered elsewhere. Deterministic rules decide *whether* a document is a
candidate; reciprocal rank fusion decides *ordering*. Raw dense and BM25
scores are never mixed directly.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Literal

CANDIDATES_VERSION = "candidates/v1"
DENSE_TOP_K = 30
BM25_TOP_K = 30
COSINE_STRONG_THRESHOLD = 0.86
COSINE_MULTI_THRESHOLD = 0.72
COSINE_MULTI_MIN_CHUNKS = 2
BM25_STRONG_PLACEMENT_RANK = 3
BM25_STRONG_MIN_CHUNKS = 3
RRF_K = 60
MAX_CLASSIFIED_CANDIDATES = 12

CandidateSource = Literal["active", "screening"]

RULE_IDENTICAL_TEXT = "identical-normalized-text"
RULE_FILENAME_FAMILY = "filename-family-match"
RULE_COSINE_STRONG = "cosine-strong-single-chunk"
RULE_COSINE_MULTI = "cosine-moderate-multiple-chunks"
RULE_BM25_REPEATED = "bm25-repeated-strong-placement"


@dataclass(frozen=True, slots=True)
class ChunkHit:
    """One retrieval hit for one incoming chunk against one stored chunk."""

    document_id: uuid.UUID
    source: CandidateSource
    chunk_id: str
    score: float
    rank: int


@dataclass(slots=True)
class CandidateEvidence:
    """Aggregated deterministic evidence for one candidate document."""

    document_id: uuid.UUID
    source: CandidateSource
    reasons: list[str] = field(default_factory=list)
    max_cosine: float = 0.0
    strong_cosine_chunks: int = 0
    moderate_cosine_chunks: int = 0
    bm25_strong_placements: int = 0
    fused_score: float = 0.0
    matched_chunk_pairs: list[tuple[int, str]] = field(default_factory=list)

    @property
    def qualified(self) -> bool:
        return bool(self.reasons)


def reciprocal_rank_fusion(
    rankings: list[list[uuid.UUID]], *, k: int = RRF_K
) -> dict[uuid.UUID, float]:
    """Fuse ranked document lists with RRF; each list contributes 1/(k+rank)."""

    if k <= 0:
        raise ValueError("RRF k must be positive")
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        seen: set[uuid.UUID] = set()
        for position, document_id in enumerate(ranking, start=1):
            if document_id in seen:
                continue
            seen.add(document_id)
            scores[document_id] = scores.get(document_id, 0.0) + 1.0 / (k + position)
    return scores


def evaluate_candidates(
    *,
    dense_results: list[list[ChunkHit]],
    bm25_results: list[list[ChunkHit]],
    filename_family_ids: set[uuid.UUID],
    identical_text_ids: set[uuid.UUID],
    sources: dict[uuid.UUID, CandidateSource],
) -> list[CandidateEvidence]:
    """Apply the deterministic candidate rules and fuse ranks for ordering.

    ``dense_results`` and ``bm25_results`` hold one hit list per incoming
    chunk. Each list can contain both active and screening hits, but every
    hit keeps its rank inside its own source collection. ``sources`` maps
    every referenced document — including filename-family and identical-text
    matches — to active or screening. Returns every qualified candidate
    ordered by fused rank, then UUID for determinism.
    """

    referenced_ids = set(filename_family_ids) | set(identical_text_ids)
    for result_group in (*dense_results, *bm25_results):
        for hit in result_group:
            if hit.rank < 1:
                raise ValueError("candidate hit ranks must be positive")
            if not hit.chunk_id:
                raise ValueError("candidate hit chunk IDs cannot be blank")
            if not math.isfinite(hit.score):
                raise ValueError("candidate hit scores must be finite")
            referenced_ids.add(hit.document_id)
            expected_source = sources.get(hit.document_id)
            if expected_source is not None and expected_source != hit.source:
                raise ValueError("candidate source evidence is inconsistent")
    missing_sources = referenced_ids - sources.keys()
    if missing_sources:
        raise ValueError("every candidate document must have an explicit source")
    if any(source not in {"active", "screening"} for source in sources.values()):
        raise ValueError("candidate sources must be active or screening")

    evidence: dict[uuid.UUID, CandidateEvidence] = {}

    def entry(document_id: uuid.UUID) -> CandidateEvidence:
        if document_id not in evidence:
            evidence[document_id] = CandidateEvidence(
                document_id=document_id,
                source=sources[document_id],
            )
        return evidence[document_id]

    strong_chunks: dict[uuid.UUID, set[int]] = {}
    moderate_chunks: dict[uuid.UUID, set[int]] = {}
    bm25_strong: dict[uuid.UUID, set[int]] = {}

    for chunk_index, hits in enumerate(dense_results):
        for hit in hits:
            item = entry(hit.document_id)
            item.max_cosine = max(item.max_cosine, hit.score)
            if hit.score >= COSINE_STRONG_THRESHOLD:
                strong_chunks.setdefault(hit.document_id, set()).add(chunk_index)
                item.matched_chunk_pairs.append((chunk_index, hit.chunk_id))
            elif hit.score >= COSINE_MULTI_THRESHOLD:
                moderate_chunks.setdefault(hit.document_id, set()).add(chunk_index)
                item.matched_chunk_pairs.append((chunk_index, hit.chunk_id))

    for chunk_index, hits in enumerate(bm25_results):
        for hit in hits:
            if hit.rank <= BM25_STRONG_PLACEMENT_RANK:
                bm25_strong.setdefault(hit.document_id, set()).add(chunk_index)

    for document_id in identical_text_ids:
        entry(document_id).reasons.append(RULE_IDENTICAL_TEXT)
    for document_id in filename_family_ids:
        entry(document_id).reasons.append(RULE_FILENAME_FAMILY)
    for document_id, chunks in strong_chunks.items():
        item = entry(document_id)
        item.strong_cosine_chunks = len(chunks)
        item.reasons.append(RULE_COSINE_STRONG)
    for document_id, chunks in moderate_chunks.items():
        item = entry(document_id)
        item.moderate_cosine_chunks = len(chunks)
        if len(chunks | strong_chunks.get(document_id, set())) >= COSINE_MULTI_MIN_CHUNKS:
            item.reasons.append(RULE_COSINE_MULTI)
    for document_id, chunks in bm25_strong.items():
        item = entry(document_id)
        item.bm25_strong_placements = len(chunks)
        if len(chunks) >= BM25_STRONG_MIN_CHUNKS:
            item.reasons.append(RULE_BM25_REPEATED)

    rankings: list[list[uuid.UUID]] = []
    for hits in (*dense_results, *bm25_results):
        for source in ("active", "screening"):
            source_ranking = [
                hit.document_id
                for hit in sorted(hits, key=lambda item: item.rank)
                if hit.source == source
            ]
            if source_ranking:
                rankings.append(source_ranking)
    fused = reciprocal_rank_fusion(rankings)
    for document_id, score in fused.items():
        if document_id in evidence:
            evidence[document_id].fused_score = round(score, 6)
    # Rule-only candidates (identical text or filename family) may not appear
    # in any retrieval ranking; a zero fused score still qualifies them.
    qualified = [item for item in evidence.values() if item.qualified]
    qualified.sort(key=lambda item: (-item.fused_score, str(item.document_id)))
    return qualified
