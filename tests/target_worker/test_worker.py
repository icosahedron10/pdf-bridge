from __future__ import annotations

import gzip
import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.core.config import Settings
from pdf_bridge.managers import worker as worker_module
from pdf_bridge.managers.worker import (
    AnalysisWorker,
    WorkerProviders,
    providers_from_settings,
)
from pdf_bridge.persistence.models import (
    DeletionPhase,
    DeletionProgress,
    Document,
    DocumentState,
    IndexAction,
    IndexOutboxEntry,
    IndexTarget,
    OperationPhase,
    OperationPriority,
    OperationState,
    OperationType,
    OutboxState,
    PreparedRevision,
    PublicationRecord,
    PublicationStatus,
    RevisionArtifact,
    RevisionStatus,
    ScanState,
    TerminalDisposition,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services.candidates import (
    BM25_STRONG_MIN_CHUNKS,
    BM25_STRONG_PLACEMENT_RANK,
    COSINE_MULTI_MIN_CHUNKS,
    COSINE_MULTI_THRESHOLD,
    COSINE_STRONG_THRESHOLD,
    MAX_CLASSIFIED_CANDIDATES,
    RRF_K,
)
from pdf_bridge.services.classification import LlmConfig
from pdf_bridge.services.extraction import LANGUAGE_PROFILE
from pdf_bridge.services.filenames import (
    JARO_WINKLER_THRESHOLD,
    MIN_FAMILY_SUBSTANTIVE_TOKENS,
    TOKEN_SET_SIMILARITY_THRESHOLD,
)
from pdf_bridge.services.local_embeddings import SparseVector
from pdf_bridge.services.markdown_chunking import CHUNKER_PROFILE
from pdf_bridge.services.markdown_formatter import FormatterConfig
from pdf_bridge.services.storage import StorageLayout, resolve_storage_key
from pdf_bridge.services.vector_index import ChunkPoint

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
ACTIVE = "customer-active"
SCREENING = "private-screening"


def document(
    *,
    state: DocumentState = DocumentState.PREFLIGHTING,
    storage_key: str | None = None,
    terminal: TerminalDisposition | None = None,
) -> Document:
    identifier = uuid.uuid4()
    return Document(
        id=identifier,
        collection_key="customer",
        original_filename=f"{identifier}.pdf",
        normalized_filename=f"{identifier}.pdf",
        size_bytes=100,
        sha256=HASH_A,
        storage_key=storage_key,
        state=state,
        terminal_disposition=terminal,
        scan_state=ScanState.CLEAN,
        scan_engine="test",
        scanned_at=utc_now(),
        created_by="operator@test",
    )


def revision(
    owner: Document,
    *,
    status: RevisionStatus,
    expected_points: int,
    number: int = 1,
) -> PreparedRevision:
    sealed = status is RevisionStatus.SEALED
    return PreparedRevision(
        document=owner,
        revision_number=number,
        status=status,
        active_qdrant_collection=ACTIVE,
        content_profile_id="sha256:" + HASH_A,
        index_profile_id="sha256:" + HASH_B,
        preflight_policy_id="sha256:" + HASH_C,
        formatter_model_id="formatter-model@formatter-commit-1",
        dense_model_id="dense@commit",
        sparse_model_id="sparse@commit",
        native_text_eligible=sealed,
        formatter_complete=sealed,
        vector_complete=sealed,
        candidate_discovery_complete=sealed,
        advisory_complete=sealed,
        clear_for_publication=False,
        incomplete_reasons=[],
        page_count=1 if expected_points else 0,
        chunk_count=expected_points,
        expected_point_count=expected_points,
        extraction_sha256=HASH_A if sealed else None,
        markdown_sha256=HASH_B if sealed else None,
        vector_manifest_sha256=HASH_C if sealed else None,
        evidence_manifest_sha256=HASH_A if sealed else None,
        manifest_sha256=HASH_B if sealed else None,
        completed_at=utc_now() if sealed else None,
        sealed_at=utc_now() if sealed else None,
        failure_code="failed" if status is RevisionStatus.FAILED else None,
        failure_message="Preparation failed." if status is RevisionStatus.FAILED else None,
    )


class ReadinessResponse:
    status_code = 200

    def __init__(self, models: list[str]) -> None:
        self.models = models

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"data": [{"id": item} for item in self.models]}


class TokenizerReadinessResponse(ReadinessResponse):
    def __init__(self, tokenizer_class: str) -> None:
        super().__init__([])
        self.tokenizer_class = tokenizer_class

    def json(self) -> dict[str, object]:
        return {
            "model": "formatter-model",
            "tokenizer_class": self.tokenizer_class,
        }


class ReadinessClient:
    def __init__(self, *, tokenizer_class: str = "TestTokenizer") -> None:
        self.urls: list[str] = []
        self.tokenizer_class = tokenizer_class

    def get(self, url: str, **_: object) -> ReadinessResponse:
        self.urls.append(url)
        if url.endswith("/tokenizer_info"):
            return TokenizerReadinessResponse(self.tokenizer_class)
        return ReadinessResponse(["formatter-model", "classifier-model", "verifier-model"])


def advisory_config() -> LlmConfig:
    return LlmConfig(
        api_url="https://advisory.test/v1",
        classifier_model="classifier-model",
        classifier_model_revision="classifier-commit-1",
        classifier_prompt_revision="classifier-prompt-v1",
        verifier_model="verifier-model",
        verifier_model_revision="verifier-commit-1",
        verifier_prompt_revision="verifier-prompt-v1",
        max_input_tokens=100,
        max_output_tokens=50,
        max_attempts=2,
    )


def test_readiness_uses_provider_specific_model_urls(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    client = ReadinessClient()
    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(
            formatter=FormatterConfig(
                api_url="https://formatter.test",
                model_id="formatter-model",
                expected_tokenizer_class="TestTokenizer",
            ),
            advisory=advisory_config(),
            http_client=client,
        ),
    )

    checks = worker.readiness_checks()

    assert checks["formatter"] == (True, None)
    assert checks["advisory"] == (True, None)
    assert client.urls == [
        "https://formatter.test/v1/models",
        "https://formatter.test/tokenizer_info",
        "https://advisory.test/v1/models",
    ]


def test_readiness_fails_with_stable_code_when_formatter_tokenizer_drifts(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    client = ReadinessClient(tokenizer_class="WrongTokenizer")
    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(
            formatter=FormatterConfig(
                api_url="https://formatter.test",
                model_id="formatter-model",
                expected_tokenizer_class="TestTokenizer",
            ),
            advisory=advisory_config(),
            http_client=client,
        ),
    )

    checks = worker.readiness_checks()

    assert checks["formatter"] == (False, "formatter_unavailable")


def test_provider_construction_rejects_formatter_prompt_or_schema_drift(
    settings: Settings,
) -> None:
    without_other_providers = {
        "dense_model_revision": None,
        "sparse_model_revision": None,
        "model_cache_dir": None,
        "llm_api_url": None,
        "llm_api_token": None,
        "llm_classifier_model": None,
        "llm_classifier_model_revision": None,
        "llm_classifier_prompt_revision": None,
        "llm_verifier_model": None,
        "llm_verifier_model_revision": None,
        "llm_verifier_prompt_revision": None,
        "qdrant_url": None,
        "qdrant_api_key": None,
        "qdrant_screening_collection_name": None,
    }
    formatter_only = settings.model_copy(update=without_other_providers)
    providers = providers_from_settings(formatter_only)
    assert providers.formatter is not None
    assert providers.formatter.expected_tokenizer_class == "TestTokenizer"

    for field, value in (
        ("formatter_prompt_revision", "formatter-prompt-v2"),
        ("formatter_schema_revision", "formatter-schema-v2"),
    ):
        drifted = settings.model_copy(update={**without_other_providers, field: value})
        try:
            providers_from_settings(drifted)
        except ValueError as exc:
            assert "revision does not match" in str(exc)
        else:
            raise AssertionError("formatter implementation drift was accepted")


def test_unconfigured_readiness_uses_stable_failure_codes(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(),
    )

    assert worker.readiness_checks() == {
        "formatter": (False, "formatter_not_configured"),
        "advisory": (False, "advisory_not_configured"),
        "local_models": (False, "local_models_not_configured"),
        "qdrant": (False, "qdrant_not_configured"),
    }


def test_non_2xx_formatter_exchange_is_persisted_without_parsing_response(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    class FailureResponse:
        status_code = 503
        text = "provider overloaded"

        def json(self) -> object:
            raise AssertionError("non-2xx response body must not be parsed")

    class FailureClient:
        def post(self, *_args: object, **_kwargs: object) -> FailureResponse:
            return FailureResponse()

    owner = document()
    prepared = revision(owner, status=RevisionStatus.FAILED, expected_points=0)
    with session_factory.begin() as session:
        session.add_all([owner, prepared])
    capturing = worker_module._CapturingFormatterClient(FailureClient())
    response = capturing.post(
        "https://formatter.test/v1/chat/completions",
        json={"model": "formatter-model", "messages": [{"content": "protected"}]},
        headers={"Accept": "application/json"},
        timeout=5,
    )
    assert response.status_code == 503
    assert len(capturing.exchanges) == 1

    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(),
    )
    worker._persist_formatter_exchanges(prepared.id, capturing.exchanges)

    with session_factory() as session:
        artifact = session.scalar(select(RevisionArtifact))
        assert artifact is not None
        path = resolve_storage_key(
            StorageLayout.from_root(settings.storage_root), artifact.storage_key
        )
        with gzip.open(path, "rt", encoding="utf-8") as stored:
            payload = json.load(stored)
    material = payload["material"]
    assert material["failure"] == "provider_failure_status"
    assert material["response"] == {
        "status_code": 503,
        "raw_text": "provider overloaded",
    }
    assert material["request"]["messages"] == [{"content": "protected"}]


def test_two_slots_claim_same_collection_but_not_the_same_document(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    first = document()
    second = document()
    now = utc_now()
    first_operation = WorkOperation(
        document=first,
        operation_type=OperationType.PREFLIGHT,
        priority=int(OperationPriority.NORMAL),
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        phase_started_at=now,
        attempt=1,
        created_at=now,
    )
    second_operation = WorkOperation(
        document=second,
        operation_type=OperationType.PREFLIGHT,
        priority=int(OperationPriority.NORMAL),
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        phase_started_at=now + timedelta(seconds=1),
        attempt=1,
        created_at=now + timedelta(seconds=1),
    )
    with session_factory.begin() as session:
        session.add_all([first, second, first_operation, second_operation])

    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(),
        worker_id="claim-test",
    )
    claimed_first = worker._claim_next()
    assert claimed_first is not None
    assert claimed_first.document_id == first.id

    with session_factory.begin() as session:
        session.add(
            WorkOperation(
                document_id=first.id,
                operation_type=OperationType.PREFLIGHT,
                priority=int(OperationPriority.NORMAL),
                state=OperationState.QUEUED,
                phase=OperationPhase.QUEUED,
                phase_started_at=now + timedelta(milliseconds=500),
                attempt=2,
                created_at=now + timedelta(milliseconds=500),
            )
        )

    claimed_second = worker._claim_next()
    assert claimed_second is not None
    assert claimed_second.document_id == second.id
    assert claimed_second.collection_key == claimed_first.collection_key
    assert worker._claim_next() is None

    worker._release_claim(claimed_first)
    worker._release_claim(claimed_second)


def test_heartbeat_preserves_phase_start_and_phase_transition_advances_it(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    class SingleHeartbeatStop:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, *, timeout: float) -> bool:
            assert timeout == settings.worker_heartbeat_seconds
            self.calls += 1
            return self.calls > 1

    owner = document()
    old_phase_start = utc_now().replace(tzinfo=None) - timedelta(minutes=5)
    operation = WorkOperation(
        document=owner,
        operation_type=OperationType.PREFLIGHT,
        priority=int(OperationPriority.NORMAL),
        state=OperationState.RUNNING,
        phase=OperationPhase.EXTRACTING,
        phase_started_at=old_phase_start,
        attempt=1,
        worker_id="phase-clock-test",
        created_at=old_phase_start - timedelta(seconds=1),
        updated_at=old_phase_start,
    )
    with session_factory.begin() as session:
        session.add_all([owner, operation])

    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(),
        worker_id="phase-clock-test",
    )
    worker._stop = SingleHeartbeatStop()  # type: ignore[assignment]
    worker._heartbeat_loop()

    with session_factory() as session:
        heartbeated = session.get(WorkOperation, operation.id)
        assert heartbeated is not None
        assert heartbeated.heartbeat_at is not None
        assert heartbeated.updated_at > old_phase_start
        assert heartbeated.phase_started_at == old_phase_start

    worker._set_phase(operation.id, OperationPhase.CHECKING_ELIGIBILITY)
    with session_factory() as session:
        transitioned = session.get(WorkOperation, operation.id)
        assert transitioned is not None
        new_phase_start = transitioned.phase_started_at
        assert transitioned.phase is OperationPhase.CHECKING_ELIGIBILITY
        assert new_phase_start > old_phase_start

    worker._set_phase(operation.id, OperationPhase.CHECKING_ELIGIBILITY)
    with session_factory() as session:
        repeated = session.get(WorkOperation, operation.id)
        assert repeated is not None
        assert repeated.phase_started_at == new_phase_start


def test_preflight_failure_keeps_last_durable_preparation_phase(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    owner = document(storage_key="objects/pr/progress.pdf")
    operation = WorkOperation(
        document=owner,
        operation_type=OperationType.PREFLIGHT,
        priority=int(OperationPriority.NORMAL),
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        attempt=1,
    )
    with session_factory.begin() as session:
        session.add_all([owner, operation])

    source = resolve_storage_key(
        StorageLayout.from_root(settings.storage_root), owner.storage_key or ""
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4\n")

    def fail_during_validation(*_args: object, **kwargs: object) -> None:
        callback = kwargs["progress_callback"]
        assert callable(callback)
        callback(OperationPhase.CHECKING_ELIGIBILITY)
        callback(OperationPhase.PACKING_FORMATTER_BATCHES)
        callback(OperationPhase.FORMATTING_MARKDOWN)
        callback(OperationPhase.VALIDATING_MARKDOWN)
        raise worker_module.preparation.PreparationError(
            "formatter_failed",
            "formatter response did not satisfy validation",
        )

    monkeypatch.setattr(
        worker_module.preparation,
        "prepare_revision_content",
        fail_during_validation,
    )
    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(
            local_models=object(),  # type: ignore[arg-type]
            language_detector=object(),  # type: ignore[arg-type]
            formatter=FormatterConfig(
                api_url="https://formatter.test",
                model_id="formatter-model",
                expected_tokenizer_class="TestTokenizer",
            ),
            http_client=object(),
        ),
        worker_id="preflight-progress-test",
    )

    assert worker.run_available(max_operations=1) == 1

    with session_factory() as session:
        persisted = session.get(Document, owner.id)
        failed = session.get(WorkOperation, operation.id)
        assert persisted is not None and persisted.state is DocumentState.PREFLIGHT_FAILED
        assert failed is not None and failed.state is OperationState.FAILED
        assert failed.phase is OperationPhase.VALIDATING_MARKDOWN
        assert failed.failure_code == "formatter_failed"


def test_concurrent_preflight_is_not_candidate_until_vector_content_commits(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    incoming = document()
    peer = document()
    incoming.original_filename = "quarterly-financial-report.pdf"
    incoming.normalized_filename = incoming.original_filename
    peer.original_filename = incoming.original_filename
    peer.normalized_filename = incoming.original_filename
    incoming_revision = revision(
        incoming,
        status=RevisionStatus.PREPARING,
        expected_points=0,
    )
    peer_incomplete_revision = revision(
        peer,
        status=RevisionStatus.PREPARING,
        expected_points=0,
    )
    now = utc_now()
    incoming_operation = WorkOperation(
        document=incoming,
        operation_type=OperationType.PREFLIGHT,
        priority=int(OperationPriority.NORMAL),
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        phase_started_at=now,
        attempt=1,
        created_at=now,
    )
    peer_operation = WorkOperation(
        document=peer,
        operation_type=OperationType.PREFLIGHT,
        priority=int(OperationPriority.NORMAL),
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        phase_started_at=now + timedelta(seconds=1),
        attempt=1,
        created_at=now + timedelta(seconds=1),
    )
    with session_factory.begin() as session:
        session.add_all(
            [
                incoming,
                peer,
                incoming_revision,
                peer_incomplete_revision,
                incoming_operation,
                peer_operation,
            ]
        )

    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(),
        worker_id="candidate-race-test",
    )
    first_claim = worker._claim_next()
    second_claim = worker._claim_next()
    assert first_claim is not None
    assert second_claim is not None
    assert {first_claim.document_id, second_claim.document_id} == {incoming.id, peer.id}

    handle = worker_module.preparation.PreparationHandle(
        revision_id=incoming_revision.id,
        document_id=incoming.id,
        revision_number=incoming_revision.revision_number,
        collection_key=incoming.collection_key,
        identity=worker._pipeline_identity(settings.collections[0]),
    )
    filename_ids, identical_ids, sources, revisions = worker._catalog_candidate_context(
        handle=handle,
        markdown_sha256=HASH_B,
        qdrant_sources={},
        qdrant_revisions={},
    )

    assert peer.id not in filename_ids
    assert peer.id not in identical_ids
    assert peer.id not in sources
    assert peer.id not in revisions

    peer_complete_revision = revision(
        peer,
        status=RevisionStatus.SEALED,
        expected_points=1,
        number=2,
    )
    with session_factory.begin() as session:
        session.add(peer_complete_revision)

    filename_ids, identical_ids, sources, revisions = worker._catalog_candidate_context(
        handle=handle,
        markdown_sha256=HASH_B,
        qdrant_sources={},
        qdrant_revisions={},
    )

    assert peer.id in filename_ids
    assert peer.id in identical_ids
    assert sources[peer.id] == "screening"
    assert revisions[peer.id] == peer_complete_revision.id

    worker._release_claim(first_claim)
    worker._release_claim(second_claim)


def test_profile_binds_complete_chunk_language_and_candidate_policy(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(),
    )
    identity = worker._pipeline_identity(settings.collections[0])
    content = json.loads(identity.profiles.content.canonical_json)["inputs"]
    policy = json.loads(identity.profiles.preflight_policy.canonical_json)["inputs"]

    assert content["chunker_profile"] == CHUNKER_PROFILE
    assert content["language_profile"] == LANGUAGE_PROFILE
    assert content["formatter_tokenizer_class"] == "TestTokenizer"
    assert identity.formatter_tokenizer_class == "TestTokenizer"
    assert policy["cosine_strong_threshold"] == COSINE_STRONG_THRESHOLD
    assert policy["cosine_multi_threshold"] == COSINE_MULTI_THRESHOLD
    assert policy["cosine_multi_min_chunks"] == COSINE_MULTI_MIN_CHUNKS
    assert policy["bm25_strong_placement_rank"] == BM25_STRONG_PLACEMENT_RANK
    assert policy["bm25_strong_min_chunks"] == BM25_STRONG_MIN_CHUNKS
    assert policy["rrf_k"] == RRF_K
    assert policy["max_classified_candidates"] == MAX_CLASSIFIED_CANDIDATES
    assert policy["filename_token_set_similarity_threshold"] == TOKEN_SET_SIMILARITY_THRESHOLD
    assert policy["filename_jaro_winkler_threshold"] == JARO_WINKLER_THRESHOLD
    assert policy["filename_min_family_substantive_tokens"] == MIN_FAMILY_SUBSTANTIVE_TOKENS


def test_verified_deletion_purges_source_artifacts_and_content_rows(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    owner = document(
        state=DocumentState.DELETING,
        storage_key="objects/de/deletion.pdf",
        terminal=TerminalDisposition.DELETED,
    )
    prepared = revision(owner, status=RevisionStatus.FAILED, expected_points=0)
    progress = DeletionProgress(
        document=owner,
        prepared_revision=prepared,
        terminal_disposition=TerminalDisposition.DELETED,
        active_qdrant_collection=ACTIVE,
        screening_qdrant_collection=SCREENING,
        phase=DeletionPhase.DELETE_ACTIVE_POINTS,
    )
    operation = WorkOperation(
        document=owner,
        prepared_revision=prepared,
        operation_type=OperationType.DELETE,
        priority=int(OperationPriority.HIGH),
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        attempt=1,
    )
    with session_factory.begin() as session:
        session.add_all([owner, prepared, progress, operation])

    layout = StorageLayout.from_root(settings.storage_root)
    source = resolve_storage_key(layout, owner.storage_key or "")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4\n")

    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(qdrant=object()),  # type: ignore[arg-type]
        worker_id="delete-test",
    )
    worker._persist_revision_artifact(
        revision_id=prepared.id,
        kind="formatter_exchange",
        payload={"request": {"messages": ["protected"]}, "response": "raw"},
    )
    with session_factory() as session:
        artifact = session.scalar(select(RevisionArtifact))
        assert artifact is not None
        artifact_path = resolve_storage_key(layout, artifact.storage_key)
        assert artifact_path.is_file()

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        worker_module,
        "delete_document_points",
        lambda _client, *, collection, document_id: calls.append(("delete", collection)),
    )
    monkeypatch.setattr(
        worker_module,
        "verify_document_zero",
        lambda _client, *, collection, document_id: calls.append(("verify", collection)),
    )

    assert worker.run_available(max_operations=1) == 1

    with session_factory() as session:
        persisted = session.get(Document, owner.id)
        persisted_operation = session.get(WorkOperation, operation.id)
        assert persisted is not None and persisted.deletion_progress is not None
        assert persisted.state is DocumentState.DELETED
        assert persisted.storage_key is None
        assert persisted.tombstone is not None
        assert persisted.deletion_progress.active_zero_verified_at is not None
        assert persisted.deletion_progress.screening_zero_verified_at is not None
        assert persisted.deletion_progress.storage_purged_at is not None
        assert persisted.deletion_progress.tombstoned_at is not None
        assert persisted_operation is not None
        assert persisted_operation.state is OperationState.SUCCEEDED
        assert session.scalar(select(func.count(RevisionArtifact.id))) == 0
        assert session.get(PreparedRevision, prepared.id) is not None
        outbox = session.scalars(select(IndexOutboxEntry)).all()
        assert {(item.target, item.action, item.state) for item in outbox} == {
            (IndexTarget.ACTIVE, IndexAction.DELETE, OutboxState.APPLIED),
            (IndexTarget.SCREENING, IndexAction.DELETE, OutboxState.APPLIED),
        }
    assert calls == [
        ("delete", ACTIVE),
        ("verify", ACTIVE),
        ("delete", SCREENING),
        ("verify", SCREENING),
    ]
    assert not source.exists()
    assert not artifact_path.exists()


def test_first_purge_attempt_fails_hard_when_source_is_missing(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    owner = document(
        state=DocumentState.DELETING,
        storage_key="objects/mi/missing.pdf",
        terminal=TerminalDisposition.DELETED,
    )
    progress = DeletionProgress(
        document=owner,
        terminal_disposition=TerminalDisposition.DELETED,
        active_qdrant_collection=ACTIVE,
        screening_qdrant_collection=SCREENING,
        phase=DeletionPhase.DELETE_ACTIVE_POINTS,
    )
    operation = WorkOperation(
        document=owner,
        operation_type=OperationType.DELETE,
        priority=int(OperationPriority.HIGH),
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        attempt=1,
    )
    with session_factory.begin() as session:
        session.add_all([owner, progress, operation])

    monkeypatch.setattr(worker_module, "delete_document_points", lambda *a, **k: None)
    monkeypatch.setattr(worker_module, "verify_document_zero", lambda *a, **k: None)
    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(qdrant=object()),  # type: ignore[arg-type]
        worker_id="missing-source-test",
    )

    assert worker.run_available(max_operations=1) == 1

    with session_factory() as session:
        persisted = session.get(Document, owner.id)
        failed = session.get(WorkOperation, operation.id)
        assert persisted is not None and persisted.deletion_progress is not None
        assert persisted.state is DocumentState.DELETE_FAILED
        assert persisted.storage_key == "objects/mi/missing.pdf"
        assert persisted.deletion_progress.storage_purged_at is None
        assert failed is not None and failed.state is OperationState.FAILED
        assert failed.failure_code == "content_missing_before_purge"
        assert not failed.retryable


def test_restart_accepts_missing_source_after_durable_purge_checkpoint(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    owner = document(
        state=DocumentState.DELETING,
        storage_key="objects/re/restart.pdf",
        terminal=TerminalDisposition.DELETED,
    )
    progress = DeletionProgress(
        document=owner,
        terminal_disposition=TerminalDisposition.DELETED,
        active_qdrant_collection=ACTIVE,
        screening_qdrant_collection=SCREENING,
        phase=DeletionPhase.PURGE_STORAGE,
        active_zero_verified_at=utc_now(),
        screening_zero_verified_at=utc_now(),
    )
    operation = WorkOperation(
        document=owner,
        operation_type=OperationType.DELETE,
        priority=int(OperationPriority.HIGH),
        state=OperationState.QUEUED,
        phase=OperationPhase.PURGE_STORAGE,
        attempt=2,
    )
    with session_factory.begin() as session:
        session.add_all([owner, progress, operation])

    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(qdrant=object()),  # type: ignore[arg-type]
        worker_id="purge-restart-test",
    )
    assert worker.run_available(max_operations=1) == 1

    with session_factory() as session:
        persisted = session.get(Document, owner.id)
        completed = session.get(WorkOperation, operation.id)
        assert persisted is not None and persisted.state is DocumentState.DELETED
        assert persisted.storage_key is None
        assert persisted.tombstone is not None
        assert completed is not None and completed.state is OperationState.SUCCEEDED


def point(document_id: uuid.UUID, revision_id: uuid.UUID) -> ChunkPoint:
    return ChunkPoint(
        chunk_id=uuid.uuid5(document_id, f"{revision_id}:0"),
        document_id=document_id,
        prepared_revision_id=revision_id,
        collection_key="customer",
        active_qdrant_collection=ACTIVE,
        chunk_index=0,
        page_start=1,
        page_end=1,
        heading_path=("Guide",),
        text_sha256=HASH_A,
        markdown="## Guide\n\nPublished content.",
        content_profile_id="sha256:" + HASH_A,
        index_profile_id="sha256:" + HASH_B,
        dense=(1.0, *([0.0] * 767)),
        sparse=SparseVector(indices=(1,), values=(1.0,)),
    )


def test_replacement_tombstones_old_document_before_incoming_publication(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    incoming = document(state=DocumentState.PUBLISHING)
    incoming_revision = revision(incoming, status=RevisionStatus.SEALED, expected_points=1)
    incoming_publication = PublicationRecord(
        document=incoming,
        prepared_revision=incoming_revision,
        active_qdrant_collection=ACTIVE,
        status=PublicationStatus.PENDING,
        expected_points=1,
    )
    old = document(
        state=DocumentState.DELETING,
        storage_key="objects/ol/old.pdf",
        terminal=TerminalDisposition.DELETED,
    )
    old.replaced_by_document_id = incoming.id
    old_revision = revision(old, status=RevisionStatus.SEALED, expected_points=0)
    old_publication = PublicationRecord(
        document=old,
        prepared_revision=old_revision,
        active_qdrant_collection=ACTIVE,
        status=PublicationStatus.VERIFIED,
        expected_points=0,
        verified_points=0,
        payload_revision_verified=True,
        vector_schema_verified=True,
        screening_zero_verified=True,
        verified_at=utc_now(),
    )
    old_progress = DeletionProgress(
        document=old,
        prepared_revision=old_revision,
        publication_record=old_publication,
        terminal_disposition=TerminalDisposition.DELETED,
        active_qdrant_collection=ACTIVE,
        screening_qdrant_collection=SCREENING,
        phase=DeletionPhase.DELETE_ACTIVE_POINTS,
    )
    operation = WorkOperation(
        document=incoming,
        prepared_revision=incoming_revision,
        replacement_target_document_id=old.id,
        operation_type=OperationType.PUBLISH,
        priority=int(OperationPriority.REPLACEMENT),
        state=OperationState.QUEUED,
        phase=OperationPhase.QUEUED,
        attempt=1,
    )
    with session_factory.begin() as session:
        session.add_all(
            [
                incoming,
                incoming_revision,
                incoming_publication,
                old,
                old_revision,
                old_publication,
                old_progress,
                operation,
            ]
        )

    layout = StorageLayout.from_root(settings.storage_root)
    old_source = resolve_storage_key(layout, old.storage_key or "")
    old_source.parent.mkdir(parents=True, exist_ok=True)
    old_source.write_bytes(b"%PDF-1.4\n")

    calls: list[str] = []
    monkeypatch.setattr(
        worker_module.preparation,
        "reconstruct_chunk_points",
        lambda *args, **kwargs: (point(incoming.id, incoming_revision.id),),
    )
    monkeypatch.setattr(
        worker_module,
        "delete_document_points",
        lambda _client, *, collection, document_id: calls.append(
            f"delete:{document_id}:{collection}"
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "verify_document_zero",
        lambda _client, *, collection, document_id: calls.append(
            f"zero:{document_id}:{collection}"
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "stage_active_points",
        lambda _client, *, active_collection, points: calls.append(
            f"stage:{points[0].document_id}:{active_collection}"
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "verify_prepared_points",
        lambda _client, *, collection, points, published, visibility: calls.append(
            f"points:{points[0].document_id}:{visibility}"
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "activate_prepared_points",
        lambda _client, *, collection, document_id, prepared_revision_id: calls.append(
            f"activate:{document_id}:{collection}"
        ),
    )

    worker = AnalysisWorker(
        settings=settings,
        session_factory=session_factory,
        providers=WorkerProviders(qdrant=object()),  # type: ignore[arg-type]
        worker_id="replacement-test",
    )
    assert worker.run_available(max_operations=1) == 1

    first_incoming_index_call = next(
        index for index, item in enumerate(calls) if str(incoming.id) in item
    )
    assert all(str(old.id) in item for item in calls[:first_incoming_index_call])
    with session_factory() as session:
        persisted_old = session.get(Document, old.id)
        persisted_incoming = session.get(Document, incoming.id)
        publication = session.get(PublicationRecord, incoming_publication.id)
        persisted_operation = session.get(WorkOperation, operation.id)
        assert persisted_old is not None and persisted_old.state is DocumentState.DELETED
        assert persisted_old.tombstone is not None
        assert persisted_incoming is not None
        assert persisted_incoming.state is DocumentState.READY
        assert publication is not None and publication.status is PublicationStatus.VERIFIED
        assert publication.verified_points == 1
        assert publication.screening_zero_verified
        assert persisted_operation is not None
        assert persisted_operation.state is OperationState.SUCCEEDED
    assert not old_source.exists()
