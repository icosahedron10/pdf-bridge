from __future__ import annotations

import uuid

import pytest

from pdf_bridge.services import candidates, filenames


def _hit(
    document_id: uuid.UUID,
    *,
    score: float,
    rank: int,
    source: candidates.CandidateSource = "active",
) -> candidates.ChunkHit:
    return candidates.ChunkHit(
        document_id=document_id,
        source=source,
        chunk_id=f"chunk-{rank}",
        score=score,
        rank=rank,
    )


def test_candidate_rules_are_deterministic_across_modalities() -> None:
    semantic = uuid.UUID("00000000-0000-0000-0000-000000000010")
    lexical = uuid.UUID("00000000-0000-0000-0000-000000000020")
    family = uuid.UUID("00000000-0000-0000-0000-000000000030")

    result = candidates.evaluate_candidates(
        dense_results=[
            [_hit(semantic, score=0.74, rank=1)],
            [_hit(semantic, score=0.73, rank=1)],
        ],
        bm25_results=[
            [_hit(lexical, score=9.0, rank=1, source="screening")],
            [_hit(lexical, score=8.0, rank=2, source="screening")],
            [_hit(lexical, score=7.0, rank=3, source="screening")],
        ],
        filename_family_ids={family},
        identical_text_ids=set(),
        sources={semantic: "active", lexical: "screening", family: "active"},
    )

    by_id = {item.document_id: item for item in result}
    assert by_id[semantic].reasons == [candidates.RULE_COSINE_MULTI]
    assert by_id[lexical].reasons == [candidates.RULE_BM25_REPEATED]
    assert by_id[lexical].source == "screening"
    assert by_id[family].reasons == [candidates.RULE_FILENAME_FAMILY]
    assert [item.document_id for item in result] == [lexical, semantic, family]


def test_candidate_evidence_requires_explicit_consistent_sources() -> None:
    document_id = uuid.uuid4()

    with pytest.raises(ValueError, match="explicit source"):
        candidates.evaluate_candidates(
            dense_results=[[_hit(document_id, score=0.9, rank=1)]],
            bm25_results=[],
            filename_family_ids=set(),
            identical_text_ids=set(),
            sources={},
        )

    with pytest.raises(ValueError, match="inconsistent"):
        candidates.evaluate_candidates(
            dense_results=[
                [_hit(document_id, score=0.9, rank=1, source="screening")]
            ],
            bm25_results=[],
            filename_family_ids=set(),
            identical_text_ids=set(),
            sources={document_id: "active"},
        )


def test_rrf_deduplicates_each_ranking_and_rejects_invalid_k() -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()

    scores = candidates.reciprocal_rank_fusion(
        [[first, first, second], [second, first]], k=60
    )

    assert scores[first] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[second] == pytest.approx(1 / 63 + 1 / 61)
    with pytest.raises(ValueError, match="positive"):
        candidates.reciprocal_rank_fusion([[first]], k=0)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Sales-Report-May-2024.pdf", "sales_report_june_2024.PDF"),
        ("onboarding-handbook-v2.pdf", "Onboarding Handbook v3.1.pdf"),
        ("q1-benefits-summary.pdf", "Q3 Benefits Summary.pdf"),
    ],
)
def test_filename_families_ignore_bounded_variable_tokens(left: str, right: str) -> None:
    match = filenames.compare_filenames(
        filenames.profile_filename(left), filenames.profile_filename(right)
    )

    assert match is not None
    assert match.kind == "filename-family"


def test_filename_advisory_rejects_weak_or_unrelated_families() -> None:
    assert (
        filenames.compare_filenames(
            filenames.profile_filename("may-report.pdf"),
            filenames.profile_filename("june-report.pdf"),
        )
        is None
    )
    assert (
        filenames.compare_filenames(
            filenames.profile_filename("travel-expense-policy.pdf"),
            filenames.profile_filename("kitchen-safety-poster.pdf"),
        )
        is None
    )
