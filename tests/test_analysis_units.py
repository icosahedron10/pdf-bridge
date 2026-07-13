"""Unit coverage for the deterministic analysis building blocks."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest

from pdf_bridge.services import bm25, candidates, chunking, classification, filenames
from pdf_bridge.services.embeddings import EmbeddingConfig, EmbeddingError, embed_texts
from pdf_bridge.services.extraction import (
    ExtractionLimits,
    ExtractionRejectedError,
    extract_pdf_text,
)
from pdf_bridge.services.fingerprint import pipeline_fingerprint

# --- helpers ---------------------------------------------------------------


def make_text_pdf(*page_texts: str) -> bytes:
    """Build a minimal, valid, parseable PDF with one text line per page."""

    objects: list[bytes] = []

    def obj(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    page_object_ids = []
    content_ids = []
    font_id_placeholder = 3 + 2 * len(page_texts)
    pages_id = 2
    for index, text in enumerate(page_texts):
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1", "replace")
        content_ids.append(3 + 2 * index)
        page_object_ids.append(4 + 2 * index)

    kids = " ".join(f"{page_id} 0 R" for page_id in page_object_ids)
    obj(b"<< /Type /Catalog /Pages 2 0 R >>")
    obj(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode())
    for index, text in enumerate(page_texts):
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1", "replace")
        obj(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        obj(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_ids[index]} 0 R "
                f"/Resources << /Font << /F1 {font_id_placeholder} 0 R >> >> >>"
            ).encode()
        )
    obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


DEFAULT_LIMITS = ExtractionLimits(
    max_pages=50,
    max_characters=100_000,
    cpu_seconds=30,
    memory_bytes=512 * 1024 * 1024,
    wall_clock_seconds=60.0,
)

PROSE = (
    "The quarterly onboarding policy explains how new employees request "
    "equipment, complete security training, and enroll in benefits. "
)


# --- chunking ---------------------------------------------------------------


def test_normalize_text_collapses_whitespace_and_control_characters() -> None:
    raw = "A title\r\nwith\todd\x07 spacing\n\n\nnext paragraph "
    normalized = chunking.normalize_text(raw)
    assert normalized == "A title\nwith odd spacing\n\n\nnext paragraph"


def test_chunk_pages_is_deterministic_and_page_mapped() -> None:
    pages = [
        chunking.PageText(number=1, text=PROSE * 30),
        chunking.PageText(number=2, text=PROSE * 30),
    ]
    first = chunking.chunk_pages(pages)
    second = chunking.chunk_pages(pages)
    assert [chunk.text_hash for chunk in first] == [chunk.text_hash for chunk in second]
    assert first[0].page_start == 1
    assert first[-1].page_end == 2
    assert all(chunk.index == position for position, chunk in enumerate(first))


def test_chunk_sizes_respect_target_and_hard_cap() -> None:
    pages = [chunking.PageText(number=1, text=PROSE * 120)]
    chunks = chunking.chunk_pages(pages)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= chunking.CHUNK_HARD_CAP_CHARS
        assert chunk.token_count <= chunking.TARGET_CHUNK_TOKENS + 80


def test_chunks_overlap_between_neighbours() -> None:
    pages = [chunking.PageText(number=1, text=PROSE * 120)]
    chunks = chunking.chunk_pages(pages)
    first_tail = chunks[0].text.split("\n")[-1]
    assert first_tail in chunks[1].text


def test_oversized_sentence_is_hard_split_not_truncated() -> None:
    single_sentence = " ".join(f"tok{index}" for index in range(2_000))
    chunks = chunking.chunk_pages([chunking.PageText(number=1, text=single_sentence)])
    assert all(len(chunk.text) <= chunking.CHUNK_HARD_CAP_CHARS for chunk in chunks)
    total_words = sum(chunk.token_count for chunk in chunks)
    assert total_words >= 2_000  # overlap may repeat words, never drop them


def test_page_budget_is_enforced_loudly() -> None:
    pages = [
        chunking.PageText(number=index + 1, text="x") for index in range(chunking.MAX_PAGES + 1)
    ]
    with pytest.raises(chunking.TextBudgetExceededError) as excinfo:
        chunking.chunk_pages(pages)
    assert excinfo.value.limit_name == "page-count"


def test_insufficient_text_is_rejected() -> None:
    with pytest.raises(chunking.InsufficientTextError):
        chunking.chunk_pages([chunking.PageText(number=1, text="page 1 of 2")])


def test_boilerplate_only_documents_are_rejected() -> None:
    boilerplate = "CONFIDENTIAL 2024 " * 20
    with pytest.raises(chunking.InsufficientTextError):
        chunking.chunk_pages([chunking.PageText(number=1, text=boilerplate)])


def test_document_text_hash_ignores_page_reflow() -> None:
    two_pages = [
        chunking.PageText(number=1, text="Alpha beta"),
        chunking.PageText(number=2, text="gamma delta"),
    ]
    three_pages = [
        chunking.PageText(number=1, text="Alpha"),
        chunking.PageText(number=2, text="beta gamma"),
        chunking.PageText(number=3, text="delta"),
    ]
    assert chunking.document_text_hash(two_pages) == chunking.document_text_hash(three_pages)


def test_document_text_hash_normalizes_unicode_and_whitespace() -> None:
    extracted = [
        chunking.PageText(number=1, text="Ａlpha\u00a0Cafe\u0301\r\npolicy"),
        chunking.PageText(number=2, text="\tterms"),
    ]
    canonical = [
        chunking.PageText(number=1, text="Alpha Café policy terms"),
    ]
    assert chunking.document_text_hash(extracted) == chunking.document_text_hash(canonical)


def test_document_text_hash_preserves_meaningful_text_changes() -> None:
    statement = [chunking.PageText(number=1, text="Hello world")]
    punctuation_change = [chunking.PageText(number=1, text="Hello world!")]
    content_change = [chunking.PageText(number=1, text="Hello worlds")]
    assert chunking.document_text_hash(statement) != chunking.document_text_hash(punctuation_change)
    assert chunking.document_text_hash(statement) != chunking.document_text_hash(content_change)


# --- filename families -------------------------------------------------------


def profile(name: str) -> filenames.FilenameProfile:
    return filenames.profile_filename(name)


def test_monthly_report_family_matches() -> None:
    match = filenames.compare_filenames(
        profile("Sales-Report-May-2024.pdf"), profile("sales_report_june_2024.PDF")
    )
    assert match is not None
    assert match.kind == "filename-family"
    assert match.shared_family_tokens == ("report", "sales")


def test_single_substantive_token_family_does_not_match() -> None:
    assert (
        filenames.compare_filenames(profile("may-report.pdf"), profile("june-report.pdf")) is None
    )


def test_version_tokens_are_variable() -> None:
    match = filenames.compare_filenames(
        profile("onboarding-handbook-v2.pdf"), profile("Onboarding Handbook v3.1.pdf")
    )
    assert match is not None
    assert match.kind == "filename-family"


def test_quarter_tokens_are_variable() -> None:
    match = filenames.compare_filenames(
        profile("q1-benefits-summary.pdf"), profile("Q3 Benefits Summary.pdf")
    )
    assert match is not None


def test_unrelated_filenames_do_not_match() -> None:
    assert (
        filenames.compare_filenames(
            profile("travel-expense-policy.pdf"), profile("kitchen safety poster.pdf")
        )
        is None
    )


def test_token_set_similarity_threshold_matches_reordered_names() -> None:
    match = filenames.compare_filenames(
        profile("employee handbook security.pdf"),
        profile("security employee handbook.pdf"),
    )
    assert match is not None


def test_jaro_winkler_catches_near_typo_names() -> None:
    match = filenames.compare_filenames(
        profile("procurement guidelines.pdf"), profile("procurement guidelnes.pdf")
    )
    assert match is not None
    assert match.similarity >= 0.9


def test_unicode_and_punctuation_normalization() -> None:
    left = profile("Résumé—Guidelines.pdf")
    right = profile("resume guidelines.PDF")
    assert left.normalized == "résumé guidelines"
    match = filenames.compare_filenames(left, right)
    assert match is not None


# --- bm25 --------------------------------------------------------------------


def test_bm25_vectors_are_deterministic_and_sorted() -> None:
    first = bm25.bm25_document_vector("alpha beta beta gamma")
    second = bm25.bm25_document_vector("alpha beta beta gamma")
    assert first == second
    assert list(first.indices) == sorted(first.indices)


def test_bm25_weights_grow_with_term_frequency() -> None:
    single = bm25.bm25_document_vector("compliance")
    double = bm25.bm25_document_vector("compliance compliance")
    index = bm25.token_index("compliance")
    weight_single = dict(zip(single.indices, single.values, strict=True))[index]
    weight_double = dict(zip(double.indices, double.values, strict=True))[index]
    assert weight_double > weight_single


def test_bm25_query_vector_uses_unit_weights() -> None:
    query = bm25.bm25_query_vector("travel policy travel")
    assert set(query.values) == {1.0}
    assert len(query.indices) == 2


# --- candidate rules and fusion ----------------------------------------------


def hit(
    document: uuid.UUID, *, score: float, rank: int, source: str = "active"
) -> candidates.ChunkHit:
    return candidates.ChunkHit(
        document_id=document,
        source=source,  # type: ignore[arg-type]
        chunk_id=f"{document}:{rank}",
        score=score,
        rank=rank,
    )


def test_strong_single_chunk_cosine_qualifies() -> None:
    document = uuid.uuid4()
    result = candidates.evaluate_candidates(
        dense_results=[[hit(document, score=0.87, rank=1)]],
        bm25_results=[[]],
        filename_family_ids=set(),
        identical_text_ids=set(),
        sources={document: "active"},
    )
    assert [item.document_id for item in result] == [document]
    assert candidates.RULE_COSINE_STRONG in result[0].reasons


def test_moderate_cosine_requires_two_chunks() -> None:
    document = uuid.uuid4()
    one_chunk = candidates.evaluate_candidates(
        dense_results=[[hit(document, score=0.75, rank=1)]],
        bm25_results=[],
        filename_family_ids=set(),
        identical_text_ids=set(),
        sources={document: "active"},
    )
    assert one_chunk == []
    two_chunks = candidates.evaluate_candidates(
        dense_results=[
            [hit(document, score=0.75, rank=1)],
            [hit(document, score=0.73, rank=1)],
        ],
        bm25_results=[],
        filename_family_ids=set(),
        identical_text_ids=set(),
        sources={document: "active"},
    )
    assert candidates.RULE_COSINE_MULTI in two_chunks[0].reasons


def test_bm25_repeated_strong_placement_qualifies() -> None:
    document = uuid.uuid4()
    strong = [[hit(document, score=9.0, rank=1)] for _ in range(3)]
    result = candidates.evaluate_candidates(
        dense_results=[],
        bm25_results=strong,
        filename_family_ids=set(),
        identical_text_ids=set(),
        sources={document: "active"},
    )
    assert candidates.RULE_BM25_REPEATED in result[0].reasons
    below = candidates.evaluate_candidates(
        dense_results=[],
        bm25_results=strong[:2],
        filename_family_ids=set(),
        identical_text_ids=set(),
        sources={document: "active"},
    )
    assert below == []


def test_identical_text_and_filename_family_qualify_without_retrieval() -> None:
    identical = uuid.uuid4()
    family = uuid.uuid4()
    result = candidates.evaluate_candidates(
        dense_results=[],
        bm25_results=[],
        filename_family_ids={family},
        identical_text_ids={identical},
        sources={identical: "screening", family: "active"},
    )
    by_id = {item.document_id: item for item in result}
    assert by_id[identical].reasons == [candidates.RULE_IDENTICAL_TEXT]
    assert by_id[identical].source == "screening"
    assert by_id[family].reasons == [candidates.RULE_FILENAME_FAMILY]


def test_rrf_orders_candidates_across_modalities() -> None:
    steady = uuid.uuid4()
    spiky = uuid.uuid4()
    dense = [
        [hit(steady, score=0.9, rank=1), hit(spiky, score=0.88, rank=2)],
        [hit(steady, score=0.9, rank=1)],
    ]
    sparse = [[hit(steady, score=5.0, rank=1)]]
    result = candidates.evaluate_candidates(
        dense_results=dense,
        bm25_results=sparse,
        filename_family_ids=set(),
        identical_text_ids=set(),
        sources={steady: "active", spiky: "active"},
    )
    assert result[0].document_id == steady
    assert result[0].fused_score > result[1].fused_score


def test_rrf_scores_follow_the_reciprocal_formula() -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    scores = candidates.reciprocal_rank_fusion([[first, second], [second, first]], k=60)
    assert scores[first] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[second] == pytest.approx(1 / 61 + 1 / 62)


# --- embeddings ----------------------------------------------------------------


def embedding_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


EMBED_CONFIG = EmbeddingConfig(
    api_url="https://models.internal/v1",
    model_id="bridge-embed-1",
    dimension=3,
)


def test_embeddings_correlate_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "bridge-embed-1"
        data = [
            {"index": index, "embedding": [float(index), 0.0, 1.0]}
            for index in range(len(body["input"]))
        ]
        return httpx.Response(200, json={"model": "bridge-embed-1", "data": list(reversed(data))})

    with embedding_client(handler) as client:
        vectors = embed_texts(EMBED_CONFIG, ["a", "b", "c"], client=client)
    assert vectors == [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [2.0, 0.0, 1.0]]


def test_embeddings_reject_wrong_dimension() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    with embedding_client(handler) as client, pytest.raises(EmbeddingError):
        embed_texts(EMBED_CONFIG, ["a"], client=client)


def test_embeddings_reject_non_finite_components() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0, float("inf"), 0.0]}]}
        )

    with embedding_client(handler) as client, pytest.raises(EmbeddingError):
        embed_texts(EMBED_CONFIG, ["a"], client=client)


def test_embeddings_reject_missing_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with embedding_client(handler) as client, pytest.raises(EmbeddingError):
        embed_texts(EMBED_CONFIG, ["a", "b"], client=client)


# --- classification --------------------------------------------------------------


LLM_CONFIG = classification.LlmConfig(
    api_url="https://models.internal/v1",
    classifier_model="bridge-classify-1",
    verifier_model="bridge-verify-1",
)

INCOMING = [
    classification.SourceExcerpt(
        reference="incoming:0", pages="1", text="The travel policy allows economy flights."
    )
]
CANDIDATE = [
    classification.SourceExcerpt(
        reference="candidate:0", pages="2", text="The travel policy allows business flights."
    )
]


def chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


def test_classification_validates_quotes_against_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["temperature"] == 0
        assert body["model"] == "bridge-classify-1"
        return chat_response(
            json.dumps(
                {
                    "label": "potential_contradiction",
                    "summary": "Flight class differs between the documents.",
                    "evidence": [{"chunk_reference": "candidate:0", "quote": "business flights"}],
                }
            )
        )

    with embedding_client(handler) as client:
        result = classification.classify_candidate(
            LLM_CONFIG,
            role="classifier",
            incoming_excerpts=INCOMING,
            candidate_excerpts=CANDIDATE,
            client=client,
        )
    assert result.valid
    assert result.finding is not None
    assert result.finding.label == "potential_contradiction"


def test_classification_rejects_fabricated_quotes_after_one_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return chat_response(
            json.dumps(
                {
                    "label": "near_duplicate",
                    "summary": "Same document.",
                    "evidence": [
                        {"chunk_reference": "candidate:0", "quote": "first class flights"}
                    ],
                }
            )
        )

    with embedding_client(handler) as client:
        result = classification.classify_candidate(
            LLM_CONFIG,
            role="verifier",
            incoming_excerpts=INCOMING,
            candidate_excerpts=CANDIDATE,
            client=client,
        )
    assert calls == 2
    assert not result.valid
    assert result.finding is None
    assert "quote" in (result.error or "")


def test_classification_rejects_unknown_labels() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response(json.dumps({"label": "duplicate!!", "summary": "x", "evidence": []}))

    with embedding_client(handler) as client:
        result = classification.classify_candidate(
            LLM_CONFIG,
            role="classifier",
            incoming_excerpts=INCOMING,
            candidate_excerpts=CANDIDATE,
            client=client,
        )
    assert not result.valid


def test_classification_unavailable_endpoint_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with (
        embedding_client(handler) as client,
        pytest.raises(classification.ClassificationUnavailableError),
    ):
        classification.classify_candidate(
            LLM_CONFIG,
            role="classifier",
            incoming_excerpts=INCOMING,
            candidate_excerpts=CANDIDATE,
            client=client,
        )


def test_prompt_marks_document_text_as_untrusted() -> None:
    prompt = classification.build_prompt(INCOMING, CANDIDATE)
    assert '<document role="incoming">' in prompt
    assert '<document role="candidate">' in prompt


# --- extraction -------------------------------------------------------------------


def test_extraction_returns_page_mapped_text(tmp_path: Path) -> None:
    path = tmp_path / "sample.pdf"
    path.write_bytes(make_text_pdf("Hello analysis world", "Second page content"))
    extracted = extract_pdf_text(path, DEFAULT_LIMITS)
    assert extracted.page_count == 2
    assert "Hello analysis world" in extracted.pages[0].text
    assert extracted.pages[1].number == 2


def test_extraction_rejects_malformed_pdfs(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not really a pdf")
    with pytest.raises(ExtractionRejectedError) as excinfo:
        extract_pdf_text(path, DEFAULT_LIMITS)
    assert excinfo.value.reason == "malformed"


def test_extraction_rejects_encrypted_pdfs(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("owner-secret")
    path = tmp_path / "locked.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    with pytest.raises(ExtractionRejectedError) as excinfo:
        extract_pdf_text(path, DEFAULT_LIMITS)
    assert excinfo.value.reason == "encrypted"


def test_extraction_enforces_page_budget(tmp_path: Path) -> None:
    path = tmp_path / "many-pages.pdf"
    path.write_bytes(make_text_pdf(*["page text"] * 4))
    limits = ExtractionLimits(
        max_pages=3,
        max_characters=100_000,
        cpu_seconds=30,
        memory_bytes=512 * 1024 * 1024,
        wall_clock_seconds=60.0,
    )
    with pytest.raises(ExtractionRejectedError) as excinfo:
        extract_pdf_text(path, limits)
    assert excinfo.value.reason == "page-budget"


def test_image_only_pdf_fails_the_text_gate(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    path = tmp_path / "image-only.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    extracted = extract_pdf_text(path, DEFAULT_LIMITS)
    with pytest.raises(chunking.InsufficientTextError):
        chunking.chunk_pages(extracted.pages)


# --- fingerprint -------------------------------------------------------------------


def test_pipeline_fingerprint_tracks_model_configuration() -> None:
    base = pipeline_fingerprint(
        embedding_model_id="bridge-embed-1",
        embedding_dimension=1024,
        classifier_model="bridge-classify-1",
        verifier_model="bridge-verify-1",
    )
    same = pipeline_fingerprint(
        embedding_model_id="bridge-embed-1",
        embedding_dimension=1024,
        classifier_model="bridge-classify-1",
        verifier_model="bridge-verify-1",
    )
    different = pipeline_fingerprint(
        embedding_model_id="bridge-embed-2",
        embedding_dimension=1024,
        classifier_model="bridge-classify-1",
        verifier_model="bridge-verify-1",
    )
    assert base == same
    assert base != different
    assert base.startswith("pl1-")
