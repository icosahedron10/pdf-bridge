from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.persistence.db import Base, build_engine, build_session_factory
from pdf_bridge.persistence.models import (
    CandidateEvidence,
    CandidateSource,
    Document,
    DocumentState,
    EvidenceKind,
    FormatterBatch,
    OperationPhase,
    PreparedCandidate,
    PreparedChunk,
    PreparedPage,
    PreparedRevision,
    RevisionStatus,
    ScanState,
    utc_now,
)
from pdf_bridge.persistence.models import (
    ExtractedPage as ExtractedPageRow,
)
from pdf_bridge.services.extraction import (
    ExtractedDocument,
    ExtractedPage,
    ExtractionLimits,
)
from pdf_bridge.services.local_embeddings import SparseVector
from pdf_bridge.services.markdown_formatter import FormatterConfig
from pdf_bridge.services.preparation import (
    CandidateInput,
    EvidenceInput,
    PreflightCompleteness,
    PreparationError,
    PreparationIdentity,
    append_advisory_evidence,
    begin_preparation,
    candidate_evidence_sha256,
    prepare_revision_content,
    reconstruct_chunk_points,
    record_preflight_candidates,
    seal_prepared_revision,
    validate_preparation_identity,
)
from pdf_bridge.services.profiles import build_pipeline_profiles


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _add_document(
    factory: sessionmaker[Session],
    *,
    state: DocumentState = DocumentState.PREFLIGHTING,
    filename: str = "guide.pdf",
) -> uuid.UUID:
    with factory.begin() as session:
        document = Document(
            collection_key="customer",
            original_filename=filename,
            normalized_filename=filename,
            size_bytes=1234,
            sha256=hashlib.sha256(filename.encode()).hexdigest(),
            storage_key=f"objects/ab/{uuid.uuid4()}.pdf",
            state=state,
            scan_state=ScanState.CLEAN,
            scan_engine="test-clamd",
            scanned_at=utc_now(),
            created_by="operator@example.test",
        )
        session.add(document)
        session.flush()
        return document.id


def _identity() -> PreparationIdentity:
    profiles = build_pipeline_profiles(
        content_inputs={
            "extractor": "pypdf-layout",
            "formatter": "formatter-model",
            "formatter_tokenizer_class": "TestTokenizer",
            "chunker": "markdown-structure-v1",
        },
        index_inputs={"dense": "mpnet-revision", "sparse": "bm25-revision"},
        preflight_policy_inputs={"candidate_limit": 20},
        active_qdrant_collection="customer-pdfs",
    )
    return PreparationIdentity(
        active_qdrant_collection="customer-pdfs",
        profiles=profiles,
        formatter_model_id="formatter-model",
        formatter_tokenizer_class="TestTokenizer",
        dense_model_id="sentence-transformers/all-mpnet-base-v2",
        sparse_model_id="Qdrant/bm25",
    )


def test_preparation_identity_requires_profile_bound_tokenizer_class() -> None:
    valid = _identity()
    drifted = PreparationIdentity(
        active_qdrant_collection=valid.active_qdrant_collection,
        profiles=valid.profiles,
        formatter_model_id=valid.formatter_model_id,
        formatter_tokenizer_class="WrongTokenizer",
        dense_model_id=valid.dense_model_id,
        sparse_model_id=valid.sparse_model_id,
    )

    with pytest.raises(PreparationError, match="did not bind the formatter tokenizer"):
        validate_preparation_identity(drifted)


def _extracted_document() -> ExtractedDocument:
    texts = (
        "# Installation\n\nInstall the package locally and verify the service. "
        "This native English paragraph contains enough meaningful text for eligibility checks. "
        "Keep each ordered word and number 12 exactly intact.",
        "## Operations\n\nRun the readiness probe before publishing the prepared revision. "
        "The operator reviews durable evidence and verifies every expected point.",
    )
    pages = tuple(
        ExtractedPage(
            page_number=index,
            layout_text=text,
            character_count=len(text),
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
        for index, text in enumerate(texts, start=1)
    )
    return ExtractedDocument(
        page_count=len(pages),
        character_count=sum(page.character_count for page in pages),
        pages=pages,
    )


class EnglishDetector:
    def detect_language(self, text: str) -> str:
        assert text
        return "en"


@dataclass(slots=True)
class FakeResponse:
    payload: object
    status_code: int = 200

    def json(self) -> object:
        return self.payload


class FormatterClient:
    def __init__(self, *, invalid: bool = False) -> None:
        self.invalid = invalid
        self.chat_calls = 0

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        del headers, timeout
        assert url.endswith("/tokenizer_info")
        return FakeResponse({"tokenizer_class": "TestTokenizer", "model": "formatter-model"})

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        del headers, timeout
        if url.endswith("/tokenize"):
            if "prompt" in json:
                tokens = [ord(character) for character in json["prompt"]]
            else:
                pages = _source_pages(json)
                count = 20 + sum(
                    len(item["source_text"].split()) for page in pages for item in page["slices"]
                )
                tokens = list(range(count))
            return FakeResponse({"count": len(tokens), "max_model_len": 500, "tokens": tokens})
        assert url.endswith("/v1/chat/completions")
        self.chat_calls += 1
        pages = _source_pages(json)
        output = {
            "pages": [
                {
                    "page_number": page["page_number"],
                    "slices": [
                        {
                            "slice_index": item["slice_index"],
                            "source_text_sha256": item["source_text_sha256"],
                            "markdown": (
                                "invented provider output" if self.invalid else item["source_text"]
                            ),
                        }
                        for item in page["slices"]
                    ],
                }
                for page in pages
            ]
        }
        return FakeResponse(
            {
                "model": "formatter-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json_module(output)},
                    }
                ],
            }
        )


def json_module(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _source_pages(request: dict[str, Any]) -> list[dict[str, Any]]:
    content = request["messages"][-1]["content"]
    return json.loads(content.split("\n", 1)[1])["pages"]


class EmbeddingModels:
    def __init__(self) -> None:
        self.dense_calls = 0
        self.sparse_document_calls = 0

    def count_tokens(self, text: str) -> int:
        return max(1, len(text.split()) + 2)

    def embed_dense(self, texts: list[str]) -> list[tuple[float, ...]]:
        self.dense_calls += 1
        assert texts
        return [tuple([1.0, *([0.0] * 767)]) for _ in texts]

    def embed_sparse_documents(self, texts: list[str]) -> list[SparseVector]:
        self.sparse_document_calls += 1
        assert texts
        return [SparseVector(indices=(1, 7), values=(1.0, 0.5)) for _ in texts]


def _formatter_config(*, attempts: int = 2) -> FormatterConfig:
    return FormatterConfig(
        api_url="https://formatter.internal",
        model_id="formatter-model",
        max_input_tokens=200,
        max_output_tokens=50,
        token_safety_reserve=10,
        max_pages_per_request=8,
        max_attempts=attempts,
        expected_tokenizer_class="TestTokenizer",
    )


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker[Session],
    document_id: uuid.UUID,
    *,
    formatter_client: FormatterClient | None = None,
    progress_callback: Callable[[OperationPhase], None] | None = None,
):
    extracted = _extracted_document()
    monkeypatch.setattr(
        "pdf_bridge.services.preparation.extract_pdf_layout",
        lambda path, limits: extracted,
    )
    models = EmbeddingModels()
    result = prepare_revision_content(
        factory,
        document_id=document_id,
        identity=_identity(),
        source_path=Path("unused.pdf"),
        extraction_limits=ExtractionLimits(10, 10_000, 5, 1_000_000, 10),
        language_detector=EnglishDetector(),
        formatter_config=_formatter_config(attempts=1),
        formatter_client=formatter_client or FormatterClient(),
        embedding_models=models,
        progress_callback=progress_callback,
    )
    return result, models


def test_content_preparation_reports_truthful_phase_order(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    document_id = _add_document(session_factory)
    phases: list[OperationPhase] = []

    _prepare(
        monkeypatch,
        session_factory,
        document_id,
        progress_callback=phases.append,
    )

    assert phases == [
        OperationPhase.CHECKING_ELIGIBILITY,
        OperationPhase.PACKING_FORMATTER_BATCHES,
        OperationPhase.FORMATTING_MARKDOWN,
        OperationPhase.VALIDATING_MARKDOWN,
        OperationPhase.CHUNKING_MARKDOWN,
        OperationPhase.EMBEDDING_DENSE,
        OperationPhase.EMBEDDING_SPARSE,
    ]


def test_progress_checkpoint_loss_stops_without_mutating_revision_failure_state(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    document_id = _add_document(session_factory)

    class CheckpointLost(RuntimeError):
        pass

    def lose_checkpoint(phase: OperationPhase) -> None:
        assert phase is OperationPhase.CHECKING_ELIGIBILITY
        raise CheckpointLost("operation lease changed")

    with pytest.raises(CheckpointLost, match="operation lease changed"):
        _prepare(
            monkeypatch,
            session_factory,
            document_id,
            progress_callback=lose_checkpoint,
        )

    with session_factory() as session:
        revision = session.scalar(select(PreparedRevision))
        assert revision is not None and revision.status is RevisionStatus.PREPARING
        assert session.scalars(select(ExtractedPageRow)).all() == []
        assert session.scalars(select(PreparedPage)).all() == []
        assert session.scalars(select(PreparedChunk)).all() == []


def test_content_preparation_seals_and_publication_reconstructs_only_persisted_points(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    document_id = _add_document(session_factory)
    result, models = _prepare(monkeypatch, session_factory, document_id)

    assert models.dense_calls == 1
    assert models.sparse_document_calls == 1
    assert result.points
    assert [point.chunk_index for point in result.points] == list(range(len(result.points)))
    assert all(point.document_id == document_id for point in result.points)
    assert all(point.active_qdrant_collection == "customer-pdfs" for point in result.points)

    with session_factory() as session:
        revision = session.get(PreparedRevision, result.handle.revision_id)
        assert revision is not None
        assert revision.status is RevisionStatus.PREPARING
        assert revision.extraction_sha256 == result.extraction_sha256
        assert revision.markdown_sha256 == result.markdown_sha256
        assert revision.vector_manifest_sha256 == result.vector_manifest_sha256
        assert len(session.scalars(select(ExtractedPageRow)).all()) == 2
        assert len(session.scalars(select(PreparedPage)).all()) == 2
        assert len(session.scalars(select(PreparedChunk)).all()) == len(result.points)
        batches = session.scalars(select(FormatterBatch)).all()
        assert batches and all(
            "source_text" not in item for row in batches for item in row.page_slices
        )

    sealed = seal_prepared_revision(
        session_factory,
        handle=result.handle,
        completeness=PreflightCompleteness(True, True, True),
    )
    assert sealed.clear_for_publication is True
    assert sealed.manifest_sha256

    # This public reconstruction API accepts no parser, formatter, tokenizer,
    # or embedding dependency and therefore cannot regenerate content.
    reconstructed = reconstruct_chunk_points(
        session_factory,
        revision_id=result.handle.revision_id,
    )
    assert reconstructed == result.points
    with session_factory() as session:
        revision = session.get(PreparedRevision, result.handle.revision_id)
        assert revision is not None
        assert revision.status is RevisionStatus.SEALED
        assert revision.manifest_sha256 == sealed.manifest_sha256
        assert revision.expected_point_count == revision.chunk_count


def test_begin_preparation_allocates_monotonic_revision_numbers(
    session_factory: sessionmaker[Session],
) -> None:
    document_id = _add_document(session_factory)
    first = begin_preparation(
        session_factory,
        document_id=document_id,
        identity=_identity(),
    )
    second = begin_preparation(
        session_factory,
        document_id=document_id,
        identity=_identity(),
    )
    assert (first.revision_number, second.revision_number) == (1, 2)
    assert first.revision_id != second.revision_id


def test_formatter_failure_marks_revision_failed_without_sealing_or_source_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    document_id = _add_document(session_factory)
    source = _extracted_document().pages[0].layout_text
    phases: list[OperationPhase] = []

    with pytest.raises(PreparationError) as raised:
        _prepare(
            monkeypatch,
            session_factory,
            document_id,
            formatter_client=FormatterClient(invalid=True),
            progress_callback=phases.append,
        )

    assert raised.value.code == "formatter_failed"
    assert phases[-1] is OperationPhase.VALIDATING_MARKDOWN
    assert source not in str(raised.value)
    with session_factory() as session:
        revision = session.scalar(select(PreparedRevision))
        assert revision is not None
        assert revision.status is RevisionStatus.FAILED
        assert revision.sealed_at is None
        assert revision.manifest_sha256 is None
        assert session.scalars(select(PreparedPage)).all() == []
        assert session.scalars(select(PreparedChunk)).all() == []


def test_seal_rejects_tampered_vector_and_leaves_revision_preparing(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    document_id = _add_document(session_factory)
    result, _ = _prepare(monkeypatch, session_factory, document_id)
    with session_factory.begin() as session:
        chunk = session.scalar(select(PreparedChunk))
        assert chunk is not None and chunk.vector is not None
        chunk.vector.dense_sha256 = "0" * 64

    with pytest.raises(PreparationError, match="vector hash"):
        seal_prepared_revision(
            session_factory,
            handle=result.handle,
            completeness=PreflightCompleteness(True, True, True),
        )

    with session_factory() as session:
        revision = session.get(PreparedRevision, result.handle.revision_id)
        assert revision is not None
        assert revision.status is RevisionStatus.PREPARING
        assert revision.manifest_sha256 is None
        assert revision.sealed_at is None


def test_seal_incorporates_existing_candidate_and_validated_evidence(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    incoming_id = _add_document(session_factory)
    matched_id = _add_document(
        session_factory,
        state=DocumentState.READY,
        filename="existing.pdf",
    )
    result, _ = _prepare(monkeypatch, session_factory, incoming_id)

    with session_factory.begin() as session:
        candidate = PreparedCandidate(
            prepared_revision_id=result.handle.revision_id,
            matched_document_id=matched_id,
            source=CandidateSource.ACTIVE,
            rank=1,
            reasons=["dense", "bm25"],
            max_cosine=0.9,
            bm25_score=2.0,
            fused_score=0.95,
            matched_chunk_pairs=[[str(result.points[0].chunk_id), "matched-chunk"]],
            document_snapshot={"state": "READY"},
        )
        session.add(candidate)
        session.flush()
        for kind, model_id in (
            (EvidenceKind.DETERMINISTIC, None),
            (EvidenceKind.CLASSIFIER, "classifier-model"),
            (EvidenceKind.VERIFIER, "verifier-model"),
        ):
            evidence = [{"reference": "chunk-0"}]
            session.add(
                CandidateEvidence(
                    prepared_revision_id=result.handle.revision_id,
                    candidate_id=candidate.id,
                    kind=kind,
                    model_id=model_id,
                    valid=True,
                    label="near_duplicate",
                    summary="Validated evidence.",
                    evidence=evidence,
                    evidence_sha256=candidate_evidence_sha256(
                        kind=kind,
                        model_id=model_id,
                        valid=True,
                        label="near_duplicate",
                        summary="Validated evidence.",
                        evidence=evidence,
                        failure_code=None,
                    ),
                )
            )

    sealed = seal_prepared_revision(
        session_factory,
        handle=result.handle,
        completeness=PreflightCompleteness(True, True, False),
    )
    assert sealed.clear_for_publication is False
    assert sealed.evidence_manifest_sha256
    with session_factory() as session:
        revision = session.get(PreparedRevision, result.handle.revision_id)
        assert revision is not None
        assert revision.evidence_manifest_sha256 == sealed.evidence_manifest_sha256
        assert revision.advisory_complete is True


def test_record_preflight_candidates_validates_scope_and_builds_public_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    incoming_id = _add_document(session_factory)
    matched_id = _add_document(
        session_factory,
        state=DocumentState.READY,
        filename="retained.pdf",
    )
    result, _ = _prepare(monkeypatch, session_factory, incoming_id)
    candidate_ids = record_preflight_candidates(
        session_factory,
        handle=result.handle,
        candidates=(
            CandidateInput(
                matched_document_id=matched_id,
                source=CandidateSource.ACTIVE,
                reasons=("cosine-strong-single-chunk",),
                max_cosine=0.91,
                bm25_score=2.0,
                fused_score=0.04,
                matched_chunk_pairs=((0, result.points[0].chunk_id),),
                evidence=(
                    EvidenceInput(
                        kind=EvidenceKind.DETERMINISTIC,
                        model_id=None,
                        valid=True,
                        label="deterministic_match",
                        summary="Deterministic threshold matched.",
                    ),
                ),
            ),
        ),
    )
    append_advisory_evidence(
        session_factory,
        handle=result.handle,
        evidence_by_candidate={
            candidate_ids[0]: (
                EvidenceInput(
                    kind=EvidenceKind.CLASSIFIER,
                    model_id="classifier-v1",
                    valid=True,
                    label="near_duplicate",
                    summary="Classifier evidence validated.",
                ),
                EvidenceInput(
                    kind=EvidenceKind.VERIFIER,
                    model_id="verifier-v1",
                    valid=True,
                    label="near_duplicate",
                    summary="Verifier evidence validated.",
                ),
            )
        },
    )

    with session_factory() as session:
        candidate = session.get(PreparedCandidate, candidate_ids[0])
        assert candidate is not None
        assert candidate.document_snapshot == {
            "id": str(matched_id),
            "collection_key": "customer",
            "original_filename": "retained.pdf",
            "state": "READY",
            "sha256": hashlib.sha256(b"retained.pdf").hexdigest(),
        }
        assert {item.kind for item in candidate.evidence} == {
            EvidenceKind.DETERMINISTIC,
            EvidenceKind.CLASSIFIER,
            EvidenceKind.VERIFIER,
        }

    sealed = seal_prepared_revision(
        session_factory,
        handle=result.handle,
        completeness=PreflightCompleteness(True, True, False),
    )
    assert sealed.clear_for_publication is False


def test_seal_rejects_nonclear_complete_candidate_free_revision(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    document_id = _add_document(session_factory)
    result, _ = _prepare(monkeypatch, session_factory, document_id)

    with pytest.raises(PreparationError, match="clear-for-publication"):
        seal_prepared_revision(
            session_factory,
            handle=result.handle,
            completeness=PreflightCompleteness(True, True, False),
        )


def test_preparation_does_not_hide_failure_checkpoint_errors(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    document_id = _add_document(session_factory)

    def fail_checkpoint(*_args: object, **_kwargs: object) -> None:
        raise PreparationError("catalog_write_failed", "failure checkpoint unavailable")

    monkeypatch.setattr(
        "pdf_bridge.services.preparation.fail_preparation",
        fail_checkpoint,
    )
    with pytest.raises(PreparationError) as raised:
        _prepare(
            monkeypatch,
            session_factory,
            document_id,
            formatter_client=FormatterClient(invalid=True),
        )
    assert raised.value.code == "failure_persistence_failed"
