from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from qdrant_client import models as qdrant_models
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import pdf_bridge.managers.worker as worker_module
from pdf_bridge.core.config import Settings
from pdf_bridge.managers.worker import AnalysisWorker, WorkerProviders
from pdf_bridge.persistence.models import (
    AnalysisChunk,
    AnalysisStatus,
    CollectionEpoch,
    DecisionAction,
    Document,
    DocumentAnalysis,
    DocumentState,
    IndexAction,
    IndexOutboxEntry,
    IndexTarget,
    IntakeDecision,
    OperationPhase,
    OperationState,
    OperationType,
    OutboxState,
    ReplacementState,
    ReplacementWorkflow,
    ScanState,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services import analysis as analysis_steps
from pdf_bridge.services import artifacts as artifact_store
from pdf_bridge.services.bm25 import SparseVectorData
from pdf_bridge.services.storage import StorageLayout, resolve_storage_key
from pdf_bridge.services.vector_index import (
    INDEX_SCHEMA_VERSION,
    SCREENING_COLLECTION,
    ChunkPoint,
    VectorIndexError,
    VectorIndexUnavailableError,
    delete_document_points,
    publish_document_points,
    query_dense,
    upsert_chunk_points,
    verify_document_point_count,
)


def _document(state: DocumentState, *, collection_key: str = "customer") -> Document:
    document_id = uuid.uuid4()
    digest = hashlib.sha256(str(document_id).encode()).hexdigest()
    return Document(
        id=document_id,
        original_filename=f"{document_id}.pdf",
        normalized_filename=f"{document_id}.pdf",
        size_bytes=100,
        sha256=digest,
        idempotency_key=f"upload-{document_id}",
        state=state,
        collection_key=collection_key,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="operator@example.test",
        ingested_at=utc_now() if state == DocumentState.INGESTED else None,
        collection_epoch=1,
    )


def _analysis(session: Session, document: Document) -> DocumentAnalysis:
    analysis = DocumentAnalysis(
        id=uuid.uuid4(),
        document=document,
        revision=1,
        status=AnalysisStatus.COMPLETE,
        pipeline_fingerprint="pl1-worker-test",
        collection_epoch=1,
        page_count=1,
        chunk_count=1,
        text_sha256="c" * 64,
        semantic_complete=True,
        classification_complete=True,
        screening_indexed=True,
        completed_at=utc_now(),
    )
    document.analysis_revision = 1
    document.page_count = 1
    document.chunk_count = 1
    document.text_sha256 = "c" * 64
    session.add(analysis)
    session.flush()
    return analysis


def _chunk_point(document_id: uuid.UUID, analysis_id: uuid.UUID) -> ChunkPoint:
    return ChunkPoint(
        document_id=document_id,
        analysis_id=analysis_id,
        chunk_index=0,
        collection_key="customer",
        page_start=1,
        page_end=1,
        text_hash="d" * 64,
        text="A substantive test chunk used for durable index lifecycle coverage.",
        dense=(0.25, 0.75),
        sparse=SparseVectorData(indices=(1, 2), values=(0.4, 0.6)),
    )


def _indexed_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"embedding_dimension": 2})


def _worker(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    qdrant: object | None = None,
    worker_id: str | None = None,
) -> AnalysisWorker:
    return AnalysisWorker(
        settings=_indexed_settings(settings),
        session_factory=session_factory,
        providers=WorkerProviders(qdrant=qdrant),  # type: ignore[arg-type]
        worker_id=worker_id,
    )


def _install_worker_index_fakes(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[Any, ...]],
    *,
    old_document_id: uuid.UUID | None = None,
    fail_active_upsert: bool = False,
    fail_publish: bool = False,
) -> None:
    monkeypatch.setattr(
        AnalysisWorker,
        "_ensure_vectors_ready",
        lambda _self, _document_id: None,
    )

    def load_points(
        _self: AnalysisWorker,
        document_id: uuid.UUID,
        analysis_id: uuid.UUID,
        *,
        collection_epoch: int,
    ) -> list[ChunkPoint]:
        assert collection_epoch >= 1
        return [_chunk_point(document_id, analysis_id)]

    monkeypatch.setattr(AnalysisWorker, "_load_points", load_points)
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

    def upsert(
        _client: object,
        collection: str,
        points: list[ChunkPoint],
        *,
        published: bool,
        screening: bool | None = None,
    ) -> None:
        events.append(
            (
                "upsert",
                points[0].document_id,
                collection,
                published,
                screening,
            )
        )
        if fail_active_upsert and collection != SCREENING_COLLECTION:
            raise VectorIndexUnavailableError("simulated active upsert outage")

    def verify(
        _client: object,
        collection: str,
        document_id: uuid.UUID,
        *,
        expected: int,
        published: bool | None = None,
    ) -> None:
        events.append(("verify", document_id, collection, expected, published))

    def delete(_client: object, collection: str, document_id: uuid.UUID) -> None:
        event = "old-delete-verified" if document_id == old_document_id else "delete"
        events.append((event, document_id, collection))

    def publish(
        _client: object,
        collection: str,
        document_id: uuid.UUID,
        *,
        expected: int,
    ) -> None:
        events.append(("publish", document_id, collection, expected))
        if fail_publish:
            raise VectorIndexUnavailableError("simulated publication outage")

    monkeypatch.setattr(worker_module, "upsert_chunk_points", upsert)
    monkeypatch.setattr(worker_module, "verify_document_point_count", verify)
    monkeypatch.setattr(worker_module, "delete_document_points", delete)
    monkeypatch.setattr(worker_module, "delete_document_points_if_collection_exists", delete)
    monkeypatch.setattr(worker_module, "publish_document_points", publish)


def test_workers_get_unique_process_instance_ids(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    first = _worker(settings, session_factory)
    second = _worker(settings, session_factory)

    assert first.worker_id.startswith("bridge-worker-")
    assert second.worker_id.startswith("bridge-worker-")
    assert first.worker_id != second.worker_id


def test_normal_polling_reclaims_and_claims_an_expired_running_lease(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _worker(settings, session_factory)
    with session_factory() as session:
        document = _document(DocumentState.ANALYZING)
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.ANALYZE,
            state=OperationState.RUNNING,
            phase=OperationPhase.EXTRACTING,
            attempt=1,
            worker_id="dead-process",
            heartbeat_at=utc_now() - timedelta(minutes=10),
            lease_expires_at=utc_now() - timedelta(seconds=1),
        )
        session.add_all([document, operation])
        session.commit()
        operation_id = operation.id
        document_id = document.id

    claimed = worker._claim_next()

    assert claimed == (
        operation_id,
        document_id,
        "customer",
        OperationType.ANALYZE,
    )
    with session_factory() as session:
        operation = session.get(WorkOperation, operation_id)
        assert operation is not None
        assert operation.state == OperationState.RUNNING
        assert operation.worker_id == worker.worker_id
        assert operation.heartbeat_at is not None
        assert operation.lease_expires_at is not None
    worker._release_collection("customer")


@pytest.mark.parametrize(
    ("case", "operation_type", "initial_state", "failed_state", "method_name"),
    [
        (
            "analyze",
            OperationType.ANALYZE,
            DocumentState.ANALYZING,
            DocumentState.ANALYZING,
            "_run_analyze",
        ),
        (
            "ingest",
            OperationType.INGEST,
            DocumentState.INGESTING,
            DocumentState.INGEST_FAILED,
            "_run_ingest",
        ),
        (
            "replace",
            OperationType.INGEST,
            DocumentState.REPLACING,
            DocumentState.REPLACE_FAILED,
            "_run_ingest",
        ),
        (
            "delete",
            OperationType.DELETE,
            DocumentState.DELETING,
            DocumentState.DELETE_FAILED,
            "_run_delete",
        ),
        (
            "cleanup",
            OperationType.CLEANUP,
            DocumentState.CLEANUP_PENDING,
            DocumentState.CLEANUP_FAILED,
            "_run_cleanup",
        ),
    ],
)
def test_generic_executor_crashes_enter_retryable_document_states(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    operation_type: OperationType,
    initial_state: DocumentState,
    failed_state: DocumentState,
    method_name: str,
) -> None:
    worker = _worker(settings, session_factory)
    replacement_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None
    with session_factory() as session:
        document = _document(initial_state)
        session.add(document)
        session.flush()
        if case == "replace":
            old_document = _document(DocumentState.INGESTED)
            session.add(old_document)
            session.flush()
            decision = IntakeDecision(
                document=document,
                analysis_id=uuid.uuid4(),
                analysis_revision=1,
                action=DecisionAction.REPLACE,
                target_document_id=old_document.id,
                idempotency_key=f"decision-{document.id}",
                advisory_override=False,
                actor_type="session",
                actor_id="operator@example.test",
            )
            session.add(decision)
            session.flush()
            workflow = ReplacementWorkflow(
                new_document_id=document.id,
                old_document_id=old_document.id,
                decision_id=decision.id,
                state=ReplacementState.PREPARING,
            )
            session.add(workflow)
            session.flush()
            replacement_id = workflow.id
            workflow_id = workflow.id
        operation = WorkOperation(
            document=document,
            operation_type=operation_type,
            state=OperationState.RUNNING,
            phase=OperationPhase.QUEUED,
            attempt=1,
            replacement_id=replacement_id,
            worker_id=worker.worker_id,
            heartbeat_at=utc_now(),
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
        session.add(operation)
        session.commit()
        document_id = document.id
        operation_id = operation.id

    def crash(*_args: object) -> None:
        raise RuntimeError("simulated unexpected executor crash")

    monkeypatch.setattr(worker, method_name, crash)
    worker._execute((operation_id, document_id, "customer", operation_type))

    with session_factory() as session:
        document = session.get(Document, document_id)
        operation = session.get(WorkOperation, operation_id)
        assert document is not None and operation is not None
        assert document.state == failed_state
        assert operation.state == OperationState.FAILED
        assert operation.retryable is True
        assert operation.completed_at is not None
        assert operation.worker_id is None
        assert operation.lease_expires_at is None
        if workflow_id is not None:
            workflow = session.get(ReplacementWorkflow, workflow_id)
            assert workflow is not None
            assert workflow.error == "The worker crashed while executing this operation."


def test_ingest_prepares_unpublished_then_recovers_durable_publication(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    _install_worker_index_fakes(monkeypatch, events)
    qdrant = object()
    first_worker = _worker(settings, session_factory, qdrant=qdrant)
    with session_factory() as session:
        document = _document(DocumentState.INGESTING)
        session.add(document)
        session.flush()
        analysis = _analysis(session, document)
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.INGEST,
            state=OperationState.RUNNING,
            phase=OperationPhase.INGESTING,
            attempt=1,
            worker_id=first_worker.worker_id,
            heartbeat_at=utc_now(),
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
        session.add(operation)
        session.commit()
        document_id = document.id
        analysis_id = analysis.id
        operation_id = operation.id

    first_worker._prepare_publication(document_id)
    with session_factory() as session:
        document = session.get(Document, document_id)
        entries = session.scalars(
            select(IndexOutboxEntry)
            .where(IndexOutboxEntry.document_id == document_id)
            .order_by(IndexOutboxEntry.id)
        ).all()
        assert document is not None and document.state == DocumentState.INGESTING
        assert [(entry.target, entry.action, entry.state) for entry in entries] == [
            (IndexTarget.ACTIVE, IndexAction.UPSERT, OutboxState.DONE),
            (IndexTarget.SCREENING, IndexAction.DELETE, OutboxState.DONE),
        ]
    active_upsert = next(event for event in events if event[0] == "upsert")
    assert active_upsert[1:] == (
        document_id,
        "active-customer-v1",
        False,
        False,
    )
    assert not any(event[0] == "publish" for event in events)

    first_worker._queue_publication(document_id)
    with session_factory() as session:
        document = session.get(Document, document_id)
        publication = session.scalar(
            select(IndexOutboxEntry).where(
                IndexOutboxEntry.document_id == document_id,
                IndexOutboxEntry.analysis_id == analysis_id,
                IndexOutboxEntry.action == IndexAction.PUBLISH,
            )
        )
        assert document is not None and document.state == DocumentState.INGESTING
        assert publication is not None and publication.state == OutboxState.PENDING
    assert not any(event[0] == "publish" for event in events)

    recovered_worker = _worker(settings, session_factory, qdrant=qdrant)
    with session_factory() as session:
        operation = session.get(WorkOperation, operation_id)
        assert operation is not None
        operation.worker_id = recovered_worker.worker_id
        operation.heartbeat_at = utc_now()
        operation.lease_expires_at = utc_now() + timedelta(minutes=5)
        session.commit()
    recovered_worker._run_ingest(operation_id, document_id)

    with session_factory() as session:
        document = session.get(Document, document_id)
        operation = session.get(WorkOperation, operation_id)
        publication = session.scalar(
            select(IndexOutboxEntry).where(
                IndexOutboxEntry.document_id == document_id,
                IndexOutboxEntry.action == IndexAction.PUBLISH,
            )
        )
        assert document is not None and document.state == DocumentState.INGESTED
        assert operation is not None and operation.state == OperationState.SUCCEEDED
        assert publication is not None and publication.state == OutboxState.DONE
    publish_event = next(event for event in events if event[0] == "publish")
    assert publish_event[1:] == (document_id, "active-customer-v1", 1)
    assert events.index(publish_event) > events.index(active_upsert)


def test_publication_outage_keeps_plain_ingest_open_and_retryable(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    _install_worker_index_fakes(monkeypatch, events, fail_publish=True)
    worker = _worker(settings, session_factory, qdrant=object())
    with session_factory() as session:
        document = _document(DocumentState.INGESTING)
        session.add(document)
        session.flush()
        _analysis(session, document)
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.INGEST,
            state=OperationState.RUNNING,
            phase=OperationPhase.INGESTING,
            attempt=1,
            worker_id=worker.worker_id,
        )
        session.add(operation)
        session.commit()
        document_id = document.id
        operation_id = operation.id

    worker._run_plain_ingest(operation_id, document_id)

    with session_factory() as session:
        document = session.get(Document, document_id)
        operation = session.get(WorkOperation, operation_id)
        publication = session.scalar(
            select(IndexOutboxEntry).where(
                IndexOutboxEntry.document_id == document_id,
                IndexOutboxEntry.action == IndexAction.PUBLISH,
            )
        )
        retry = session.scalar(
            select(WorkOperation).where(
                WorkOperation.document_id == document_id,
                WorkOperation.attempt == 2,
            )
        )
        assert document is not None and document.state == DocumentState.INGESTING
        assert operation is not None and operation.state == OperationState.FAILED
        assert publication is not None and publication.state == OutboxState.PENDING
        assert publication.attempts == 1
        assert retry is not None and retry.state == OperationState.QUEUED
    assert any(event[0] == "publish" for event in events)


class _QueryClient:
    def __init__(self) -> None:
        self.filters: list[qdrant_models.Filter] = []

    def query_points(self, _collection: str, **kwargs: Any) -> SimpleNamespace:
        self.filters.append(kwargs["query_filter"])
        return SimpleNamespace(points=[])


def _filter_values(query_filter: qdrant_models.Filter) -> dict[str, Any]:
    return {
        condition.key: condition.match.value
        for condition in query_filter.must or []
        if isinstance(condition, qdrant_models.FieldCondition)
    }


def test_active_and_screening_queries_enforce_visibility_payloads() -> None:
    client = _QueryClient()

    query_dense(
        client,  # type: ignore[arg-type]
        "active-customer-v1",
        vector=[0.1, 0.2],
        top_k=3,
        collection_key="customer",
    )
    query_dense(
        client,  # type: ignore[arg-type]
        SCREENING_COLLECTION,
        vector=[0.1, 0.2],
        top_k=3,
        collection_key="customer",
    )

    active = _filter_values(client.filters[0])
    screening = _filter_values(client.filters[1])
    assert active["schema_version"] == INDEX_SCHEMA_VERSION
    assert active["published"] is True
    assert active["screening"] is False
    assert active["collection_key"] == "customer"
    assert screening["schema_version"] == INDEX_SCHEMA_VERSION
    assert screening["screening"] is True
    assert screening["published"] is False
    assert screening["collection_key"] == "customer"


class _MutationClient:
    def __init__(self) -> None:
        self.count_value = 0
        self.upserts: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.payload_updates: list[dict[str, Any]] = []
        self.counts: list[dict[str, Any]] = []

    def upsert(self, collection: str, **kwargs: Any) -> None:
        self.upserts.append({"collection": collection, **kwargs})

    def delete(self, collection: str, **kwargs: Any) -> None:
        self.deletes.append({"collection": collection, **kwargs})

    def set_payload(self, collection: str, **kwargs: Any) -> None:
        self.payload_updates.append({"collection": collection, **kwargs})

    def count(self, collection: str, **kwargs: Any) -> SimpleNamespace:
        self.counts.append({"collection": collection, **kwargs})
        return SimpleNamespace(count=self.count_value)


def test_index_mutations_use_strong_waits_and_exact_count_verification() -> None:
    client = _MutationClient()
    document_id = uuid.uuid4()
    point = _chunk_point(document_id, uuid.uuid4())

    upsert_chunk_points(
        client,  # type: ignore[arg-type]
        "active-customer-v1",
        [point],
        published=False,
        screening=False,
    )
    upsert_call = client.upserts[0]
    assert upsert_call["wait"] is True
    assert upsert_call["ordering"] == qdrant_models.WriteOrdering.STRONG
    assert upsert_call["points"][0].payload["published"] is False
    assert upsert_call["points"][0].payload["screening"] is False

    client.count_value = 1
    verify_document_point_count(
        client,  # type: ignore[arg-type]
        "active-customer-v1",
        document_id,
        expected=1,
    )
    assert client.counts[-1]["exact"] is True

    client.count_value = 0
    delete_document_points(
        client,  # type: ignore[arg-type]
        "active-customer-v1",
        document_id,
    )
    delete_call = client.deletes[0]
    assert delete_call["wait"] is True
    assert delete_call["ordering"] == qdrant_models.WriteOrdering.STRONG
    assert client.counts[-1]["exact"] is True

    client.count_value = 1
    publish_document_points(
        client,  # type: ignore[arg-type]
        "active-customer-v1",
        document_id,
        expected=1,
    )
    publish_call = client.payload_updates[0]
    assert publish_call["payload"] == {"published": True, "screening": False}
    assert publish_call["wait"] is True
    assert publish_call["ordering"] == qdrant_models.WriteOrdering.STRONG
    published_count_filter = client.counts[-1]["count_filter"]
    published_filter = _filter_values(published_count_filter)
    assert published_filter["published"] is True
    assert published_filter["screening"] is False
    assert published_filter["schema_version"] == INDEX_SCHEMA_VERSION
    assert client.counts[-1]["exact"] is True

    client.count_value = 0
    with pytest.raises(VectorIndexError, match="expected exactly 1"):
        verify_document_point_count(
            client,  # type: ignore[arg-type]
            "active-customer-v1",
            document_id,
            expected=1,
        )


def _replacement_records(
    session: Session,
    *,
    worker_id: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    old_document = _document(DocumentState.INGESTED)
    new_document = _document(DocumentState.REPLACING)
    session.add_all([old_document, new_document])
    session.flush()
    analysis = _analysis(session, new_document)
    decision = IntakeDecision(
        document=new_document,
        analysis_id=analysis.id,
        analysis_revision=analysis.revision,
        action=DecisionAction.REPLACE,
        target_document_id=old_document.id,
        idempotency_key=f"decision-{new_document.id}",
        advisory_override=False,
        actor_type="session",
        actor_id="operator@example.test",
    )
    session.add(decision)
    session.flush()
    workflow = ReplacementWorkflow(
        new_document_id=new_document.id,
        old_document_id=old_document.id,
        decision_id=decision.id,
        state=ReplacementState.PREPARING,
    )
    session.add(workflow)
    session.flush()
    operation = WorkOperation(
        document=new_document,
        operation_type=OperationType.INGEST,
        state=OperationState.RUNNING,
        phase=OperationPhase.QUEUED,
        attempt=1,
        replacement_id=workflow.id,
        worker_id=worker_id,
        heartbeat_at=utc_now(),
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    session.add(operation)
    session.commit()
    return old_document.id, new_document.id, workflow.id, operation.id


def test_plain_ingest_recovers_after_publication_before_operation_checkpoint(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    _install_worker_index_fakes(monkeypatch, events)
    worker = _worker(settings, session_factory, qdrant=object())
    with session_factory() as session:
        document = _document(DocumentState.INGESTING)
        session.add(document)
        session.flush()
        _analysis(session, document)
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.INGEST,
            state=OperationState.RUNNING,
            phase=OperationPhase.INGESTING,
            attempt=1,
            worker_id=worker.worker_id,
        )
        session.add(operation)
        session.commit()
        document_id = document.id
        operation_id = operation.id

    worker._publish_prepared_document(document_id)

    with session_factory() as session:
        document = session.get(Document, document_id)
        operation = session.get(WorkOperation, operation_id)
        assert document is not None and document.state == DocumentState.INGESTED
        assert operation is not None and operation.state == OperationState.RUNNING

    worker._run_ingest(operation_id, document_id)

    with session_factory() as session:
        operation = session.get(WorkOperation, operation_id)
        assert operation is not None and operation.state == OperationState.SUCCEEDED
        assert operation.phase == OperationPhase.COMPLETE


def test_replacement_recovers_after_publication_before_workflow_checkpoint(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[Any, ...]] = []
    _install_worker_index_fakes(monkeypatch, events)
    worker = _worker(settings, session_factory, qdrant=object())
    with session_factory() as session:
        old_id, new_id, workflow_id, operation_id = _replacement_records(
            session,
            worker_id=worker.worker_id,
        )
        old_document = session.get(Document, old_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        assert old_document is not None and workflow is not None
        old_document.state = DocumentState.DELETED
        old_document.deleted_at = utc_now()
        workflow.state = ReplacementState.INGESTING_NEW
        session.commit()

    worker._publish_prepared_document(new_id)

    with session_factory() as session:
        new_document = session.get(Document, new_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        operation = session.get(WorkOperation, operation_id)
        assert new_document is not None and new_document.state == DocumentState.INGESTED
        assert workflow is not None and workflow.state == ReplacementState.INGESTING_NEW
        assert operation is not None and operation.state == OperationState.RUNNING

    worker._run_ingest(operation_id, new_id)

    with session_factory() as session:
        workflow = session.get(ReplacementWorkflow, workflow_id)
        operation = session.get(WorkOperation, operation_id)
        assert workflow is not None and workflow.state == ReplacementState.SUCCEEDED
        assert operation is not None and operation.state == OperationState.SUCCEEDED
        assert operation.phase == OperationPhase.COMPLETE


def test_replacement_stale_epoch_fails_before_old_deletion(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _worker(settings, session_factory, qdrant=object())
    with session_factory() as session:
        old_id, new_id, workflow_id, operation_id = _replacement_records(
            session, worker_id=worker.worker_id
        )
    with session_factory() as session:
        session.add(CollectionEpoch(collection_key="customer", epoch=2))
        session.commit()

    worker._run_replacement(operation_id, new_id, workflow_id)

    with session_factory() as session:
        old_document = session.get(Document, old_id)
        new_document = session.get(Document, new_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        operation = session.get(WorkOperation, operation_id)
        assert old_document is not None and old_document.state == DocumentState.INGESTED
        assert new_document is not None and new_document.state == DocumentState.REPLACE_FAILED
        assert workflow is not None and workflow.state == ReplacementState.PREPARING
        assert operation is not None and operation.state == OperationState.FAILED
        assert "stale" in (operation.error or "")
        assert (
            session.scalar(
                select(IndexOutboxEntry.id).where(
                    IndexOutboxEntry.document_id == old_id,
                    IndexOutboxEntry.action == IndexAction.DELETE,
                )
            )
            is None
        )


def test_replacement_corrupt_vectors_fail_before_old_deletion(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _worker(settings, session_factory, qdrant=object())
    layout = StorageLayout.from_root(settings.storage_root)
    with session_factory() as session:
        old_id, new_id, workflow_id, operation_id = _replacement_records(
            session, worker_id=worker.worker_id
        )
    with session_factory() as session:
        new_document = session.get(Document, new_id)
        assert new_document is not None
        analysis = session.scalar(
            select(DocumentAnalysis).where(DocumentAnalysis.document_id == new_id)
        )
        assert analysis is not None
        analysis.pipeline_fingerprint = worker._fingerprint()
        chunk_text = "A retained replacement chunk with enough text for indexing."
        session.add(
            AnalysisChunk(
                id=uuid.uuid4(),
                analysis=analysis,
                document_id=new_id,
                chunk_index=0,
                page_start=1,
                page_end=1,
                token_count=10,
                text_hash=hashlib.sha256(chunk_text.encode()).hexdigest(),
                text=chunk_text,
            )
        )
        vectors = artifact_store.write_artifact(
            layout,
            new_id,
            analysis.id,
            "vectors",
            {
                "analysis_id": str(analysis.id),
                "pipeline_fingerprint": analysis.pipeline_fingerprint,
                "embedding_model_id": settings.embedding_model_id,
                "dimension": 2,
                "dense": [[0.25, 0.75]],
                "sparse": [{"indices": [1], "values": [1.0]}],
            },
        )
        analysis_steps.record_artifact(session, analysis, vectors)
        session.commit()

    vectors_path = resolve_storage_key(layout, vectors.storage_key)
    corrupted = bytearray(vectors_path.read_bytes())
    corrupted[-1] ^= 1
    vectors_path.write_bytes(corrupted)

    worker._run_replacement(operation_id, new_id, workflow_id)

    with session_factory() as session:
        old_document = session.get(Document, old_id)
        new_document = session.get(Document, new_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        operation = session.get(WorkOperation, operation_id)
        assert old_document is not None and old_document.state == DocumentState.INGESTED
        assert new_document is not None and new_document.state == DocumentState.REPLACE_FAILED
        assert workflow is not None and workflow.state == ReplacementState.PREPARING
        assert operation is not None and operation.state == OperationState.FAILED
        assert "integrity checks" in (operation.error or "")


def test_replacement_deletes_old_before_new_active_write_and_publish(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qdrant = object()
    worker = _worker(settings, session_factory, qdrant=qdrant)
    with session_factory() as session:
        old_id, new_id, workflow_id, operation_id = _replacement_records(
            session, worker_id=worker.worker_id
        )
    events: list[tuple[Any, ...]] = []
    _install_worker_index_fakes(monkeypatch, events, old_document_id=old_id)

    worker._run_replacement(operation_id, new_id, workflow_id)

    old_delete = next(event for event in events if event[0] == "old-delete-verified")
    new_upsert = next(event for event in events if event[0] == "upsert")
    new_publish = next(event for event in events if event[0] == "publish")
    assert old_delete[1] == old_id
    assert new_upsert[1] == new_id
    assert new_upsert[3:] == (False, False)
    assert new_publish[1] == new_id
    assert events.index(old_delete) < events.index(new_upsert) < events.index(new_publish)

    with session_factory() as session:
        old_document = session.get(Document, old_id)
        new_document = session.get(Document, new_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        operation = session.get(WorkOperation, operation_id)
        assert old_document is not None and old_document.state == DocumentState.DELETED
        assert old_document.replaced_by_document_id == new_id
        assert new_document is not None and new_document.state == DocumentState.INGESTED
        assert workflow is not None and workflow.state == ReplacementState.SUCCEEDED
        assert operation is not None and operation.state == OperationState.SUCCEEDED


def test_replacement_post_delete_index_failure_stays_retryable(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qdrant = object()
    worker = _worker(settings, session_factory, qdrant=qdrant)
    with session_factory() as session:
        old_id, new_id, workflow_id, operation_id = _replacement_records(
            session, worker_id=worker.worker_id
        )
    events: list[tuple[Any, ...]] = []
    _install_worker_index_fakes(
        monkeypatch,
        events,
        old_document_id=old_id,
        fail_active_upsert=True,
    )

    worker._run_replacement(operation_id, new_id, workflow_id)

    old_delete = next(event for event in events if event[0] == "old-delete-verified")
    failed_upsert = next(event for event in events if event[0] == "upsert")
    assert events.index(old_delete) < events.index(failed_upsert)
    assert not any(event[0] == "publish" for event in events)

    with session_factory() as session:
        old_document = session.get(Document, old_id)
        new_document = session.get(Document, new_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        failed = session.get(WorkOperation, operation_id)
        retry = session.scalar(
            select(WorkOperation).where(
                WorkOperation.document_id == new_id,
                WorkOperation.attempt == 2,
            )
        )
        assert old_document is not None and old_document.state == DocumentState.DELETED
        assert new_document is not None and new_document.state == DocumentState.REPLACING
        assert workflow is not None and workflow.state == ReplacementState.INGESTING_NEW
        assert workflow.error is not None and "active upsert outage" in workflow.error
        assert failed is not None and failed.state == OperationState.FAILED
        assert failed.retryable is True
        assert retry is not None and retry.state == OperationState.QUEUED
        assert retry.replacement_id == workflow_id
        publication = session.scalar(
            select(IndexOutboxEntry).where(
                IndexOutboxEntry.document_id == new_id,
                IndexOutboxEntry.action == IndexAction.PUBLISH,
            )
        )
        assert publication is None


def test_replacement_publication_outage_keeps_new_document_open(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory, qdrant=object())
    with session_factory() as session:
        old_id, new_id, workflow_id, operation_id = _replacement_records(
            session, worker_id=worker.worker_id
        )
    events: list[tuple[Any, ...]] = []
    _install_worker_index_fakes(
        monkeypatch,
        events,
        old_document_id=old_id,
        fail_publish=True,
    )

    worker._run_replacement(operation_id, new_id, workflow_id)

    with session_factory() as session:
        old_document = session.get(Document, old_id)
        new_document = session.get(Document, new_id)
        workflow = session.get(ReplacementWorkflow, workflow_id)
        failed = session.get(WorkOperation, operation_id)
        publication = session.scalar(
            select(IndexOutboxEntry).where(
                IndexOutboxEntry.document_id == new_id,
                IndexOutboxEntry.action == IndexAction.PUBLISH,
            )
        )
        retry = session.scalar(
            select(WorkOperation).where(
                WorkOperation.document_id == new_id,
                WorkOperation.attempt == 2,
            )
        )
        assert old_document is not None and old_document.state == DocumentState.DELETED
        assert new_document is not None and new_document.state == DocumentState.REPLACING
        assert workflow is not None
        assert workflow.state == ReplacementState.INGESTING_NEW
        assert failed is not None and failed.state == OperationState.FAILED
        assert publication is not None and publication.state == OutboxState.PENDING
        assert retry is not None and retry.replacement_id == workflow_id
    assert any(event[0] == "publish" for event in events)


def test_parser_rejection_cleanup_does_not_depend_on_unused_qdrant(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory, qdrant=object())
    with session_factory() as session:
        document = _document(DocumentState.CLEANUP_PENDING)
        document.cleanup_target = DocumentState.REJECTED
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.CLEANUP,
            state=OperationState.RUNNING,
            phase=OperationPhase.CLEANING_UP,
            attempt=1,
            worker_id=worker.worker_id,
        )
        session.add_all([document, operation])
        session.commit()
        document_id = document.id
        operation_id = operation.id

    monkeypatch.setattr(
        worker,
        "_drain_outbox",
        lambda _document_id: pytest.fail("Qdrant must not be called without index work"),
    )

    worker._run_cleanup(operation_id, document_id)

    with session_factory() as session:
        document = session.get(Document, document_id)
        operation = session.get(WorkOperation, operation_id)
        assert document is not None and document.state == DocumentState.REJECTED
        assert document.rejected_at is not None
        assert operation is not None and operation.state == OperationState.SUCCEEDED
