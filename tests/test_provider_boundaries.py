"""Adversarial coverage for model responses and retained evidence boundaries."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import pdf_bridge.managers.worker as worker_module
from pdf_bridge.managers.worker import AnalysisWorker, WorkerProviders
from pdf_bridge.persistence.models import (
    AnalysisCandidate,
    AnalysisChunk,
    AnalysisStatus,
    CandidateFindingRecord,
    Document,
    DocumentAnalysis,
    DocumentArtifact,
    DocumentState,
    ScanState,
    utc_now,
)
from pdf_bridge.services import analysis as analysis_steps
from pdf_bridge.services import artifacts as artifact_store
from pdf_bridge.services import catalog, classification
from pdf_bridge.services.storage import StorageLayout

LLM_CONFIG = classification.LlmConfig(
    api_url="https://models.internal/v1",
    classifier_model="bridge-classify-1",
    verifier_model="bridge-verify-1",
)
INCOMING_EXCERPTS = [
    classification.SourceExcerpt(
        reference="incoming:0",
        pages="1",
        text="The travel policy allows economy flights.",
    )
]
CANDIDATE_EXCERPTS = [
    classification.SourceExcerpt(
        reference="candidate:0",
        pages="2",
        text="The travel policy allows business flights.",
    )
]


def _model_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _chat_payload(content: str, *, model: str = "bridge-classify-1") -> dict[str, object]:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"choices": True},
        {"choices": [{"message": []}]},
        _chat_payload("{}", model="unexpected-model"),
    ],
)
def test_classification_rejects_malformed_or_mismatched_response_envelopes(
    payload: object,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with (
        _model_client(handler) as client,
        pytest.raises(classification.ClassificationUnavailableError),
    ):
        classification.classify_candidate(
            LLM_CONFIG,
            role="classifier",
            incoming_excerpts=INCOMING_EXCERPTS,
            candidate_excerpts=CANDIDATE_EXCERPTS,
            client=client,
        )


def test_classification_request_is_deterministic_toolless_and_strict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == LLM_CONFIG.classifier_model
        assert body["n"] == 1
        assert body["temperature"] == 0
        assert "tools" not in body
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(
            200,
            json=_chat_payload(
                json.dumps(
                    {
                        "label": "potential_contradiction",
                        "summary": "The permitted flight class differs.",
                        "evidence": [
                            {
                                "chunk_reference": "candidate:0",
                                "quote": "business flights",
                            }
                        ],
                    }
                )
            ),
        )

    with _model_client(handler) as client:
        result = classification.classify_candidate(
            LLM_CONFIG,
            role="classifier",
            incoming_excerpts=INCOMING_EXCERPTS,
            candidate_excerpts=CANDIDATE_EXCERPTS,
            client=client,
        )

    assert result.valid is True


def test_classification_retries_once_and_retains_every_raw_output() -> None:
    responses = [
        json.dumps(
            {
                "label": "potential_contradiction",
                "summary": "The permitted flight class differs.",
                "evidence": [
                    {
                        "chunk_reference": "candidate:0",
                        "quote": "Business flights",
                    }
                ],
            }
        ),
        json.dumps(
            {
                "label": "potential_contradiction",
                "summary": "The permitted flight class differs.",
                "evidence": [
                    {
                        "chunk_reference": "candidate:0",
                        "quote": "business flights",
                    }
                ],
            }
        ),
    ]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return httpx.Response(200, json=_chat_payload(response))

    with _model_client(handler) as client:
        result = classification.classify_candidate(
            LLM_CONFIG,
            role="classifier",
            incoming_excerpts=INCOMING_EXCERPTS,
            candidate_excerpts=CANDIDATE_EXCERPTS,
            client=client,
        )

    assert calls == 2
    assert result.valid is True
    assert result.attempts == 2
    assert result.raw_outputs == tuple(responses)
    assert result.raw_output == responses[-1]


@pytest.mark.parametrize(
    "malformed",
    [
        True,
        {"label": True, "summary": "x", "evidence": []},
        {"label": "uncertain", "summary": True, "evidence": []},
        {"label": "uncertain", "summary": "x", "evidence": True},
    ],
)
def test_classification_rejects_boolean_structured_values(malformed: object) -> None:
    calls = 0
    raw = json.dumps(malformed)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_chat_payload(raw))

    with _model_client(handler) as client:
        result = classification.classify_candidate(
            LLM_CONFIG,
            role="classifier",
            incoming_excerpts=INCOMING_EXCERPTS,
            candidate_excerpts=CANDIDATE_EXCERPTS,
            client=client,
        )

    assert calls == 2
    assert result.valid is False
    assert result.raw_outputs == (raw, raw)


@pytest.mark.parametrize(
    ("incoming", "candidate"),
    [
        ([], CANDIDATE_EXCERPTS),
        (INCOMING_EXCERPTS, []),
        (
            INCOMING_EXCERPTS,
            [
                classification.SourceExcerpt(
                    reference="incoming:0",
                    pages="2",
                    text="A duplicate reference must fail closed.",
                )
            ],
        ),
    ],
)
def test_classification_requires_correlated_excerpts_before_request(
    incoming: list[classification.SourceExcerpt],
    candidate: list[classification.SourceExcerpt],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("malformed evidence must be rejected before an HTTP request")

    with (
        _model_client(handler) as client,
        pytest.raises(classification.ClassificationUnavailableError),
    ):
        classification.classify_candidate(
            LLM_CONFIG,
            role="classifier",
            incoming_excerpts=incoming,
            candidate_excerpts=candidate,
            client=client,
        )


def _document(state: DocumentState, *, filename: str) -> Document:
    document_id = uuid.uuid4()
    return Document(
        id=document_id,
        original_filename=filename,
        normalized_filename=filename.casefold(),
        size_bytes=100,
        sha256=hashlib.sha256(document_id.bytes).hexdigest(),
        idempotency_key=f"provider-boundary-{document_id}",
        state=state,
        collection_key="customer",
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="operator@example.test",
        ingested_at=utc_now() if state == DocumentState.INGESTED else None,
        collection_epoch=1,
    )


def _analysis(
    document: Document,
    *,
    revision: int,
    status: AnalysisStatus = AnalysisStatus.COMPLETE,
) -> DocumentAnalysis:
    analysis = DocumentAnalysis(
        id=uuid.uuid4(),
        document=document,
        revision=revision,
        status=status,
        collection_epoch=1,
    )
    document.analysis_revision = max(document.analysis_revision or 0, revision)
    return analysis


def _chunk(analysis: DocumentAnalysis, document: Document, text: str) -> AnalysisChunk:
    return AnalysisChunk(
        id=uuid.uuid4(),
        analysis=analysis,
        document_id=document.id,
        chunk_index=0,
        page_start=1,
        page_end=1,
        token_count=len(text.split()),
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )


def test_candidate_evidence_is_pinned_to_snapshot_document_and_revision(
    session_factory: sessionmaker[Session],
) -> None:
    incoming = _document(DocumentState.ANALYZING, filename="incoming.pdf")
    matched = _document(DocumentState.INGESTED, filename="matched.pdf")
    intruder = _document(DocumentState.INGESTED, filename="intruder.pdf")
    incoming_analysis = _analysis(incoming, revision=1, status=AnalysisStatus.RUNNING)
    matched_revision_one = _analysis(matched, revision=1)
    matched_revision_two = _analysis(matched, revision=2)
    intruder_analysis = _analysis(intruder, revision=1)
    incoming_chunk = _chunk(incoming_analysis, incoming, "retained incoming text")
    matched_chunk_one = _chunk(matched_revision_one, matched, "matched revision one text")
    matched_chunk_two = _chunk(matched_revision_two, matched, "matched revision two text")
    intruder_chunk = _chunk(intruder_analysis, intruder, "unrelated intruder text")

    with session_factory() as session:
        session.add_all([incoming, matched, intruder])
        session.flush()
        candidate = AnalysisCandidate(
            analysis=incoming_analysis,
            matched_document_id=matched.id,
            source="active",
            rank=1,
            reasons=["cosine_strong"],
            classified=True,
            matched_chunk_pairs=[[0, str(intruder_chunk.id)]],
            document_snapshot={"analysis_revision": 1},
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id
        analysis_id = incoming_analysis.id

    with session_factory() as session:
        candidate = session.get(AnalysisCandidate, candidate_id)
        analysis = session.get(DocumentAnalysis, analysis_id)
        assert candidate is not None and analysis is not None

        incoming_evidence, matched_evidence = analysis_steps.candidate_excerpts(
            session, analysis, candidate
        )
        api_incoming, api_matched = catalog._candidate_excerpt_pairs(session, candidate)
        assert [item.text for item in incoming_evidence] == [incoming_chunk.text]
        assert [item.text for item in api_incoming] == [incoming_chunk.text]
        assert matched_evidence == []
        assert api_matched == []

        candidate.matched_chunk_pairs = [[0, str(matched_chunk_two.id)]]
        session.flush()
        _, wrong_revision_evidence = analysis_steps.candidate_excerpts(session, analysis, candidate)
        _, wrong_revision_api = catalog._candidate_excerpt_pairs(session, candidate)
        assert wrong_revision_evidence == []
        assert wrong_revision_api == []

        candidate.matched_chunk_pairs = [[0, str(matched_chunk_one.id)]]
        session.flush()
        _, correlated_evidence = analysis_steps.candidate_excerpts(session, analysis, candidate)
        _, correlated_api = catalog._candidate_excerpt_pairs(session, candidate)
        assert [item.text for item in correlated_evidence] == [matched_chunk_one.text]
        assert [item.text for item in correlated_api] == [matched_chunk_one.text]


def test_finding_rows_never_store_prompts_or_raw_model_output() -> None:
    columns = set(CandidateFindingRecord.__table__.columns.keys())
    assert columns.isdisjoint({"prompt", "raw_output", "raw_outputs"})


def test_invalid_finding_keeps_classification_incomplete_and_preserves_retry_outputs(
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incoming = _document(DocumentState.ANALYZING, filename="incoming.pdf")
    matched = _document(DocumentState.INGESTED, filename="matched.pdf")
    incoming_analysis = _analysis(incoming, revision=1, status=AnalysisStatus.RUNNING)
    matched_analysis = _analysis(matched, revision=1)
    _chunk(incoming_analysis, incoming, "retained incoming text")
    matched_chunk = _chunk(matched_analysis, matched, "retained matched text")
    with session_factory() as session:
        session.add_all([incoming, matched])
        session.flush()
        candidate = AnalysisCandidate(
            analysis=incoming_analysis,
            matched_document_id=matched.id,
            source="active",
            rank=1,
            reasons=["cosine_strong"],
            classified=True,
            matched_chunk_pairs=[[0, str(matched_chunk.id)]],
            document_snapshot=analysis_steps.document_snapshot(matched),
        )
        session.add(candidate)
        session.commit()
        document_id = incoming.id
        analysis_id = incoming_analysis.id
        candidate_id = candidate.id

    invalid = classification.FindingResult(
        role="classifier",
        model_id=LLM_CONFIG.classifier_model,
        finding=None,
        valid=False,
        error="evidence quote was invalid",
        attempts=2,
        raw_output="second invalid response",
        raw_outputs=("first invalid response", "second invalid response"),
        prompt="private classifier prompt",
    )
    valid = classification.FindingResult(
        role="verifier",
        model_id=LLM_CONFIG.verifier_model,
        finding=classification.CandidateFinding(
            label="uncertain",
            summary="The retained excerpts do not establish a relationship.",
            evidence=[],
        ),
        valid=True,
        error=None,
        attempts=1,
        raw_output="valid verifier response",
        raw_outputs=("valid verifier response",),
        prompt="private verifier prompt",
    )
    calls: list[str] = []

    def classify_once(*_args: object, role: str, **_kwargs: object):
        calls.append(role)
        return invalid if role == "classifier" else valid

    monkeypatch.setattr(worker_module, "classify_candidate", classify_once)
    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("the patched classifier must own the response")
        )
    )
    try:
        worker = AnalysisWorker(
            settings=settings,
            session_factory=session_factory,
            providers=WorkerProviders(llm=LLM_CONFIG, http_client=http_client),
        )
        incomplete_reasons: list[str] = []
        classification_complete = worker._classify_candidates(
            document_id,
            analysis_id,
            [candidate_id],
            layout=StorageLayout.from_root(settings.storage_root),
            incomplete_reasons=incomplete_reasons,
            semantic_complete=True,
        )
    finally:
        http_client.close()

    assert calls == ["classifier", "verifier"]
    assert classification_complete is False
    assert incomplete_reasons == ["classification-invalid-output"]

    layout = StorageLayout.from_root(settings.storage_root)
    with session_factory() as session:
        findings = list(
            session.scalars(
                select(CandidateFindingRecord)
                .where(CandidateFindingRecord.candidate_id == candidate_id)
                .order_by(CandidateFindingRecord.role)
            ).all()
        )
        assert [(item.role, item.valid) for item in findings] == [
            ("classifier", False),
            ("verifier", True),
        ]
        artifact = session.scalar(
            select(DocumentArtifact).where(
                DocumentArtifact.analysis_id == analysis_id,
                DocumentArtifact.kind == "findings",
            )
        )
        assert artifact is not None
        payload = artifact_store.read_artifact(
            layout,
            artifact.storage_key,
            expected_sha256=artifact.sha256,
            expected_size_bytes=artifact.size_bytes,
        )
        assert payload["calls"][0]["raw_output"] == "second invalid response"
        assert payload["calls"][0]["raw_outputs"] == [
            "first invalid response",
            "second invalid response",
        ]

        analysis = session.get(DocumentAnalysis, analysis_id)
        assert analysis is not None
        analysis_steps.finalize_analysis(
            session,
            analysis,
            filename_warnings=[],
            semantic_complete=True,
            classification_complete=classification_complete,
            incomplete_reasons=incomplete_reasons,
            screening_indexed=True,
        )
        detail = catalog.analysis_detail(
            session,
            upload_id=document_id,
            page=1,
            page_size=10,
        )
        assert detail.analysis.classification_complete is False
        assert detail.analysis.incomplete_reasons == ["classification-invalid-output"]
