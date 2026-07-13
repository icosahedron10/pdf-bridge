from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from litestar.testing import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import pdf_bridge.managers.worker as worker_module
from pdf_bridge.managers.worker import AnalysisWorker, WorkerProviders
from pdf_bridge.persistence.models import (
    AnalysisCandidate,
    AnalysisChunk,
    AnalysisStatus,
    AuditEvent,
    DecisionAction,
    Document,
    DocumentAnalysis,
    DocumentArtifact,
    DocumentState,
    IndexAction,
    IndexOutboxEntry,
    IndexTarget,
    OperationPhase,
    OperationState,
    OperationType,
    OutboxState,
    ScanState,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services import artifacts as artifact_store
from pdf_bridge.services import intake
from pdf_bridge.services.embeddings import EmbeddingConfig, EmbeddingError, embed_texts
from pdf_bridge.services.storage import StorageLayout, resolve_storage_key, storage_key_for
from pdf_bridge.services.vector_index import (
    INDEX_SCHEMA_VERSION,
    SCREENING_COLLECTION,
    VectorIndexError,
    query_dense,
)


def _document(state: DocumentState, *, storage_key: str | None = None) -> Document:
    document_id = uuid.uuid4()
    return Document(
        id=document_id,
        original_filename="safety.pdf",
        normalized_filename="safety.pdf",
        storage_key=storage_key,
        size_bytes=100,
        sha256=document_id.hex * 2,
        idempotency_key=f"safety-{document_id}",
        state=state,
        collection_key="customer",
        collection_epoch=1,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="safety-test",
        ingested_at=(
            utc_now() if state in {DocumentState.INGESTED, DocumentState.DELETING} else None
        ),
    )


def _worker(settings, session_factory: sessionmaker[Session]) -> AnalysisWorker:
    return AnalysisWorker(
        settings=settings.model_copy(update={"embedding_dimension": 2}),
        session_factory=session_factory,
        providers=WorkerProviders(qdrant=object()),  # type: ignore[arg-type]
    )


def _install_delete_fakes(
    monkeypatch: pytest.MonkeyPatch,
    deleted: list[str],
) -> None:
    monkeypatch.setattr(
        worker_module,
        "ensure_active_collection",
        lambda _client, *, collection_key, epoch, dimension: f"active-{collection_key}-v{epoch}",
    )
    monkeypatch.setattr(
        worker_module,
        "ensure_screening_collection",
        lambda _client, *, dimension: None,
    )
    monkeypatch.setattr(
        worker_module,
        "delete_document_points",
        lambda _client, collection, _document_id: deleted.append(collection),
    )
    monkeypatch.setattr(
        worker_module,
        "delete_document_points_if_collection_exists",
        lambda _client, collection, _document_id: deleted.append(collection),
    )
    monkeypatch.setattr(
        worker_module,
        "upsert_chunk_points",
        lambda *_args, **_kwargs: pytest.fail("cleanup replayed an obsolete UPSERT"),
    )
    monkeypatch.setattr(
        worker_module,
        "publish_document_points",
        lambda *_args, **_kwargs: pytest.fail("cleanup replayed an obsolete PUBLISH"),
    )


def test_cleanup_supersedes_writes_then_deletes_every_touched_index_and_purges_content(
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory)
    layout = StorageLayout.from_root(settings.storage_root)
    document_id = uuid.uuid4()
    storage_key = storage_key_for(document_id)
    canonical_path = resolve_storage_key(layout, storage_key)
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_bytes(b"%PDF-1.4\nsecret source bytes\n%%EOF\n")
    secret = "SECRET-EXCERPT-MUST-NOT-SURVIVE"
    analysis_id = uuid.uuid4()
    artifact = artifact_store.write_artifact(
        layout,
        document_id,
        analysis_id,
        "findings",
        {
            "analysis_id": str(analysis_id),
            "prompt": secret,
            "raw_output": secret,
            "vectors": [[0.1, 0.2]],
        },
    )

    with session_factory() as session:
        document = _document(DocumentState.CLEANUP_PENDING, storage_key=storage_key)
        document.id = document_id
        document.cleanup_target = DocumentState.CANCELLED
        analysis = DocumentAnalysis(
            id=analysis_id,
            document=document,
            revision=1,
            status=AnalysisStatus.COMPLETE,
            collection_epoch=1,
            pipeline_fingerprint="safety-v1",
            page_count=1,
            chunk_count=1,
            text_sha256="b" * 64,
            completed_at=utc_now(),
        )
        session.add_all([document, analysis])
        session.flush()
        chunk = AnalysisChunk(
            id=uuid.uuid4(),
            analysis=analysis,
            document_id=document.id,
            chunk_index=0,
            page_start=1,
            page_end=1,
            token_count=10,
            text_hash="c" * 64,
            text=secret,
        )
        stored_artifact = DocumentArtifact(
            analysis=analysis,
            kind=artifact.kind,
            storage_key=artifact.storage_key,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
        )
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.CLEANUP,
            state=OperationState.RUNNING,
            phase=OperationPhase.CLEANING_UP,
            attempt=1,
            worker_id=worker.worker_id,
        )
        screening_write = IndexOutboxEntry(
            document_id=document.id,
            analysis_id=analysis.id,
            collection_key="customer",
            collection_epoch=1,
            target=IndexTarget.SCREENING,
            action=IndexAction.UPSERT,
            expected_points=1,
            state=OutboxState.PENDING,
        )
        active_publish = IndexOutboxEntry(
            document_id=document.id,
            analysis_id=analysis.id,
            collection_key="customer",
            collection_epoch=1,
            target=IndexTarget.ACTIVE,
            action=IndexAction.PUBLISH,
            expected_points=1,
            state=OutboxState.PENDING,
        )
        session.add_all([chunk, stored_artifact, operation, screening_write, active_publish])
        session.commit()
        analysis_id = analysis.id
        operation_id = operation.id
        obsolete_ids = (screening_write.id, active_publish.id)

    deleted: list[str] = []
    _install_delete_fakes(monkeypatch, deleted)
    worker._run_cleanup(operation_id, document_id)

    assert deleted == ["pdf-bridge-customer-v1", SCREENING_COLLECTION]
    assert not canonical_path.exists()
    assert not resolve_storage_key(layout, f"analysis/{document_id}").exists()
    with session_factory() as session:
        document = session.get(Document, document_id)
        operation = session.get(WorkOperation, operation_id)
        obsolete = [session.get(IndexOutboxEntry, entry_id) for entry_id in obsolete_ids]
        deletes = session.scalars(
            select(IndexOutboxEntry).where(
                IndexOutboxEntry.document_id == document_id,
                IndexOutboxEntry.action == IndexAction.DELETE,
            )
        ).all()
        assert document is not None and document.state == DocumentState.CANCELLED
        assert document.storage_key is None
        assert document.analysis_manifest_hash is not None
        assert operation is not None and operation.state == OperationState.SUCCEEDED
        assert all(entry is not None for entry in obsolete)
        assert all(entry.state == OutboxState.SUPERSEDED for entry in obsolete if entry)
        assert all(entry.attempts == 0 for entry in obsolete if entry)
        assert all(entry.completed_at is not None for entry in obsolete if entry)
        assert all("Superseded" in (entry.last_error or "") for entry in obsolete if entry)
        assert {(entry.target, entry.state) for entry in deletes} == {
            (IndexTarget.ACTIVE, OutboxState.DONE),
            (IndexTarget.SCREENING, OutboxState.DONE),
        }
        assert session.get(DocumentAnalysis, analysis_id) is None
        assert session.scalar(select(func.count()).select_from(AnalysisChunk)) == 0
        assert session.scalar(select(func.count()).select_from(DocumentArtifact)) == 0
        audit_json = json.dumps(
            [event.details for event in session.scalars(select(AuditEvent)).all()],
            sort_keys=True,
        )
        assert secret not in audit_json
        assert not any(
            forbidden in audit_json
            for forbidden in ('"prompt"', '"raw_output"', '"vectors"', '"text"')
        )


def test_deletion_supersedes_pending_publication_and_deletes_screening_history(
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory)
    with session_factory() as session:
        document = _document(DocumentState.DELETING)
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.DELETE,
            state=OperationState.RUNNING,
            phase=OperationPhase.CLEANING_UP,
            attempt=1,
            worker_id=worker.worker_id,
        )
        publication = IndexOutboxEntry(
            document_id=document.id,
            analysis_id=uuid.uuid4(),
            collection_key="customer",
            collection_epoch=1,
            target=IndexTarget.ACTIVE,
            action=IndexAction.PUBLISH,
            expected_points=1,
            state=OutboxState.PENDING,
        )
        screening_history = IndexOutboxEntry(
            document_id=document.id,
            analysis_id=uuid.uuid4(),
            collection_key="customer",
            collection_epoch=1,
            target=IndexTarget.SCREENING,
            action=IndexAction.UPSERT,
            expected_points=1,
            state=OutboxState.DONE,
            completed_at=utc_now(),
        )
        session.add_all([document, operation, publication, screening_history])
        session.commit()
        document_id = document.id
        operation_id = operation.id
        publication_id = publication.id

    deleted: list[str] = []
    _install_delete_fakes(monkeypatch, deleted)
    worker._run_delete(operation_id, document_id)

    assert deleted == ["pdf-bridge-customer-v1", SCREENING_COLLECTION]
    with session_factory() as session:
        document = session.get(Document, document_id)
        publication = session.get(IndexOutboxEntry, publication_id)
        assert document is not None and document.state == DocumentState.DELETED
        assert publication is not None and publication.state == OutboxState.SUPERSEDED


def test_parser_rejection_removes_canonical_bytes_without_unused_qdrant(
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory)
    layout = StorageLayout.from_root(settings.storage_root)
    document_id = uuid.uuid4()
    storage_key = storage_key_for(document_id)
    canonical_path = resolve_storage_key(layout, storage_key)
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_bytes(b"%PDF-1.4\nmalformed\n%%EOF\n")
    with session_factory() as session:
        document = _document(DocumentState.ANALYZING, storage_key=storage_key)
        document.id = document_id
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.ANALYZE,
            state=OperationState.RUNNING,
            phase=OperationPhase.EXTRACTING,
            attempt=1,
            worker_id=worker.worker_id,
        )
        session.add_all([document, operation])
        session.commit()
        operation_id = operation.id

    worker._reject_document(operation_id, document_id, "malformed", "parser rejected input")
    with session_factory() as session:
        cleanup = session.scalar(
            select(WorkOperation).where(
                WorkOperation.document_id == document_id,
                WorkOperation.operation_type == OperationType.CLEANUP,
            )
        )
        assert cleanup is not None
        cleanup.state = OperationState.RUNNING
        cleanup.worker_id = worker.worker_id
        session.commit()
        cleanup_id = cleanup.id

    monkeypatch.setattr(
        worker,
        "_drain_outbox",
        lambda _document_id: pytest.fail("parser-only rejection contacted Qdrant"),
    )
    worker._run_cleanup(cleanup_id, document_id)

    assert not canonical_path.exists()
    with session_factory() as session:
        document = session.get(Document, document_id)
        assert document is not None and document.state == DocumentState.REJECTED
        assert document.rejection_reason == "malformed"
        assert document.storage_key is None


def test_cleanup_retry_reuses_the_pending_verified_delete(
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory)
    with session_factory() as session:
        document = _document(DocumentState.CLEANUP_PENDING)
        document.cleanup_target = DocumentState.CANCELLED
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.CLEANUP,
            state=OperationState.RUNNING,
            phase=OperationPhase.CLEANING_UP,
            attempt=1,
            worker_id=worker.worker_id,
        )
        screening_history = IndexOutboxEntry(
            document_id=document.id,
            analysis_id=uuid.uuid4(),
            collection_key="customer",
            collection_epoch=1,
            target=IndexTarget.SCREENING,
            action=IndexAction.UPSERT,
            expected_points=1,
            state=OutboxState.DONE,
            completed_at=utc_now(),
        )
        session.add_all([document, operation, screening_history])
        session.commit()
        document_id = document.id
        operation_id = operation.id

    monkeypatch.setattr(
        worker_module,
        "ensure_screening_collection",
        lambda _client, *, dimension: None,
    )
    delete_attempts = 0

    def flaky_delete(_client: object, _collection: str, _document_id: uuid.UUID) -> None:
        nonlocal delete_attempts
        delete_attempts += 1
        if delete_attempts == 1:
            raise VectorIndexError("first verified deletion failed")

    monkeypatch.setattr(worker_module, "delete_document_points", flaky_delete)
    worker._run_cleanup(operation_id, document_id)

    with session_factory() as session:
        document = session.get(Document, document_id)
        first_operation = session.get(WorkOperation, operation_id)
        deletes = session.scalars(
            select(IndexOutboxEntry).where(
                IndexOutboxEntry.document_id == document_id,
                IndexOutboxEntry.action == IndexAction.DELETE,
            )
        ).all()
        assert document is not None and document.state == DocumentState.CLEANUP_FAILED
        assert first_operation is not None and first_operation.state == OperationState.FAILED
        assert len(deletes) == 1
        assert deletes[0].state == OutboxState.PENDING
        assert deletes[0].attempts == 1
        delete_id = deletes[0].id
        retry = intake.retry_upload(
            session,
            document=document,
            actor_type="session",
            actor_id="safety-test",
        )
        session.commit()
        retry_id = retry.id

    with session_factory() as session:
        retry = session.get(WorkOperation, retry_id)
        assert retry is not None
        retry.state = OperationState.RUNNING
        retry.worker_id = worker.worker_id
        session.commit()

    worker._run_cleanup(retry_id, document_id)

    with session_factory() as session:
        document = session.get(Document, document_id)
        deletes = session.scalars(
            select(IndexOutboxEntry).where(
                IndexOutboxEntry.document_id == document_id,
                IndexOutboxEntry.action == IndexAction.DELETE,
            )
        ).all()
        assert document is not None and document.state == DocumentState.CANCELLED
        assert len(deletes) == 1 and deletes[0].id == delete_id
        assert deletes[0].state == OutboxState.DONE
        assert deletes[0].attempts == 2


def test_ingested_document_with_open_ingest_work_cannot_be_deleted_or_replaced(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        target = _document(DocumentState.INGESTED)
        target_operation = WorkOperation(
            document=target,
            operation_type=OperationType.INGEST,
            state=OperationState.RUNNING,
            phase=OperationPhase.INGESTING,
            attempt=1,
        )
        incoming = _document(DocumentState.REVIEW_REQUIRED)
        analysis = DocumentAnalysis(
            document=incoming,
            revision=1,
            status=AnalysisStatus.COMPLETE,
            collection_epoch=1,
            candidate_count=1,
            completed_at=utc_now(),
        )
        session.add_all([target, target_operation, incoming, analysis])
        session.flush()
        session.add(
            AnalysisCandidate(
                analysis=analysis,
                matched_document_id=target.id,
                source="active",
                rank=1,
                reasons=["filename_family"],
                document_snapshot={"document_id": str(target.id)},
            )
        )
        session.commit()

        with pytest.raises(intake.LifecycleError) as deletion:
            intake.request_deletion(
                session,
                document=target,
                actor_type="session",
                actor_id="safety-test",
            )
        assert deletion.value.code == "document-busy"
        session.rollback()

        with pytest.raises(intake.LifecycleError) as replacement:
            intake.record_decision(
                session,
                document=incoming,
                analysis_revision=1,
                action=DecisionAction.REPLACE,
                target_document_id=target.id,
                idempotency_key="replacement-safety-key",
                actor_type="session",
                actor_id="safety-test",
            )
        assert replacement.value.code == "replacement-target-busy"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"data": [{"index": True, "embedding": [0.1, 0.2]}]},
        {"data": [{"index": 0, "embedding": [False, 0.2]}]},
        {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 0, "embedding": [0.3, 0.4]},
            ]
        },
    ],
    ids=["top-level-array", "boolean-index", "boolean-component", "duplicate-index"],
)
def test_embedding_responses_require_strict_object_index_and_vector_correlation(
    payload: object,
) -> None:
    config = EmbeddingConfig(
        api_url="https://models.internal/v1",
        model_id="embed-v1",
        dimension=2,
    )
    input_count = 2 if isinstance(payload, dict) and len(payload.get("data", [])) == 2 else 1
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )
    try:
        with pytest.raises(EmbeddingError):
            embed_texts(config, ["text"] * input_count, client=client)
    finally:
        client.close()


def test_qdrant_response_cannot_escape_the_requested_visibility_boundary() -> None:
    class ForgedQueryClient:
        def query_points(self, _collection: str, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                points=[
                    SimpleNamespace(
                        payload={
                            "schema_version": INDEX_SCHEMA_VERSION,
                            "document_id": str(uuid.uuid4()),
                            "chunk_id": str(uuid.uuid4()),
                            "collection_key": "customer",
                            "published": True,
                            "screening": True,
                        },
                        score=0.9,
                    )
                ]
            )

    with pytest.raises(VectorIndexError, match="visibility boundary"):
        query_dense(
            ForgedQueryClient(),  # type: ignore[arg-type]
            "active-customer-v1",
            vector=[0.1, 0.2],
            top_k=1,
            collection_key="customer",
        )


def test_pending_catalog_document_is_denied_from_search_results(
    app,
    client: TestClient,
    csrf_headers: dict[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        pending = _document(DocumentState.REVIEW_REQUIRED)
        session.add(pending)
        session.commit()
        pending_id = pending.id

    response_payload = {
        "query": "pending",
        "mode": "hybrid",
        "groups": [
            {
                "collection_key": "customer",
                "total": 1,
                "hits": [
                    {
                        "document_id": str(pending_id),
                        "score": 0.99,
                        "snippet": "must not be returned",
                    }
                ],
            }
        ],
    }
    search_client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response_payload))
    )
    app.state.search_http_client = search_client
    try:
        response = client.post(
            "/api/v1/search",
            headers=csrf_headers,
            json={
                "query": "pending",
                "mode": "hybrid",
                "collections": ["customer"],
                "include_hits": True,
                "page_size": 10,
            },
        )
        assert response.status_code == 502
        assert response.json()["code"] == "search-catalog-mismatch"
    finally:
        search_client.close()
