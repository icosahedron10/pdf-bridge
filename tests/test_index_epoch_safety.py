from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import pdf_bridge.managers.worker as worker_module
from pdf_bridge.core.config import Settings
from pdf_bridge.managers.worker import AnalysisWorker, WorkerProviders
from pdf_bridge.persistence.models import (
    AnalysisStatus,
    CollectionEpoch,
    Document,
    DocumentAnalysis,
    DocumentState,
    IndexAction,
    IndexOutboxEntry,
    IndexTarget,
    OutboxState,
    ScanState,
    utc_now,
)
from pdf_bridge.services import analysis as analysis_steps
from pdf_bridge.services.bm25 import SparseVectorData
from pdf_bridge.services.candidates import ChunkHit, evaluate_candidates
from pdf_bridge.services.chunking import Chunk
from pdf_bridge.services.vector_index import (
    SCREENING_COLLECTION,
    VectorIndexError,
    delete_document_points_if_collection_exists,
)


def _document(state: DocumentState = DocumentState.ANALYZING) -> Document:
    document_id = uuid.uuid4()
    return Document(
        id=document_id,
        original_filename=f"{document_id}.pdf",
        normalized_filename=f"{document_id}.pdf",
        size_bytes=100,
        sha256=hashlib.sha256(document_id.bytes).hexdigest(),
        idempotency_key=f"epoch-{document_id}",
        state=state,
        collection_key="customer",
        collection_epoch=1,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="epoch-test",
    )


def _analysis(
    session: Session,
    document: Document,
    *,
    revision: int,
    epoch: int,
) -> DocumentAnalysis:
    analysis = DocumentAnalysis(
        document=document,
        revision=revision,
        status=AnalysisStatus.COMPLETE,
        pipeline_fingerprint="epoch-test-v1",
        collection_epoch=epoch,
        page_count=1,
        chunk_count=0,
        text_sha256=hashlib.sha256(f"analysis-{revision}".encode()).hexdigest(),
        semantic_complete=True,
        classification_complete=True,
        completed_at=utc_now(),
    )
    session.add(analysis)
    session.flush()
    return analysis


def _worker(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    qdrant: object = object(),
) -> AnalysisWorker:
    return AnalysisWorker(
        settings=settings.model_copy(update={"embedding_dimension": 2}),
        session_factory=session_factory,
        providers=WorkerProviders(qdrant=qdrant),  # type: ignore[arg-type]
    )


def test_outbox_snapshots_analysis_or_current_epoch_and_delete_is_explicit(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        document = _document()
        session.add_all([document, CollectionEpoch(collection_key="customer", epoch=3)])
        session.flush()
        analysis = _analysis(session, document, revision=1, epoch=2)

        analysis_write = analysis_steps.enqueue_index_entry(
            session,
            document=document,
            analysis_id=analysis.id,
            target=IndexTarget.ACTIVE,
            action=IndexAction.UPSERT,
            expected_points=0,
        )
        current_write = analysis_steps.enqueue_index_entry(
            session,
            document=document,
            analysis_id=None,
            target=IndexTarget.SCREENING,
            action=IndexAction.UPSERT,
            expected_points=0,
        )
        with pytest.raises(ValueError, match="explicit collection epoch"):
            analysis_steps.enqueue_index_entry(
                session,
                document=document,
                analysis_id=None,
                target=IndexTarget.ACTIVE,
                action=IndexAction.DELETE,
                expected_points=0,
            )
        removal = analysis_steps.enqueue_index_entry(
            session,
            document=document,
            analysis_id=None,
            target=IndexTarget.ACTIVE,
            action=IndexAction.DELETE,
            expected_points=0,
            collection_epoch=1,
        )

        assert analysis_write.collection_epoch == 2
        assert current_write.collection_epoch == 3
        assert removal.collection_epoch == 1


def test_stale_active_write_fails_before_alias_or_point_mutation(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory)
    with session_factory() as session:
        document = _document(DocumentState.INGESTING)
        session.add_all([document, CollectionEpoch(collection_key="customer", epoch=2)])
        session.flush()
        analysis = _analysis(session, document, revision=1, epoch=1)
        entry = analysis_steps.enqueue_index_entry(
            session,
            document=document,
            analysis_id=analysis.id,
            target=IndexTarget.ACTIVE,
            action=IndexAction.UPSERT,
            expected_points=0,
        )
        session.commit()
        document_id = document.id
        entry_id = entry.id

    monkeypatch.setattr(
        worker_module,
        "ensure_active_collection",
        lambda *_args, **_kwargs: pytest.fail("stale write repointed the alias"),
    )
    monkeypatch.setattr(
        worker,
        "_load_points",
        lambda *_args, **_kwargs: pytest.fail("stale write loaded index points"),
    )

    with pytest.raises(VectorIndexError, match="refusing stale active index mutation"):
        worker._drain_outbox(document_id)

    with session_factory() as session:
        entry = session.get(IndexOutboxEntry, entry_id)
        assert entry is not None and entry.state == OutboxState.PENDING
        assert entry.attempts == 1
        assert "current epoch 2" in (entry.last_error or "")


def test_removal_purges_every_active_epoch_without_repointing_alias(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory)
    with session_factory() as session:
        document = _document(DocumentState.DELETING)
        session.add_all([document, CollectionEpoch(collection_key="customer", epoch=3)])
        session.flush()
        first = _analysis(session, document, revision=1, epoch=1)
        second = _analysis(session, document, revision=2, epoch=2)
        for analysis, target, action in (
            (first, IndexTarget.ACTIVE, IndexAction.UPSERT),
            (second, IndexTarget.ACTIVE, IndexAction.PUBLISH),
            (second, IndexTarget.SCREENING, IndexAction.UPSERT),
        ):
            entry = analysis_steps.enqueue_index_entry(
                session,
                document=document,
                analysis_id=analysis.id,
                target=target,
                action=action,
                expected_points=0,
            )
            entry.state = OutboxState.DONE
            entry.completed_at = utc_now()
        worker._queue_verified_index_deletions(
            session,
            document,
            required_targets=(IndexTarget.ACTIVE,),
        )
        session.commit()
        document_id = document.id

    deleted: list[str] = []
    monkeypatch.setattr(
        worker_module,
        "ensure_active_collection",
        lambda *_args, **_kwargs: pytest.fail("removal changed the active alias"),
    )
    monkeypatch.setattr(
        worker_module,
        "delete_document_points_if_collection_exists",
        lambda _client, collection, _document_id: deleted.append(collection),
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

    worker._drain_outbox(document_id)

    assert deleted == [
        "pdf-bridge-customer-v1",
        "pdf-bridge-customer-v2",
        "pdf-bridge-customer-v3",
        SCREENING_COLLECTION,
    ]


def test_absent_historical_collection_is_already_clean() -> None:
    class MissingCollectionClient:
        def __init__(self) -> None:
            self.lookups: list[str] = []

        def collection_exists(self, collection: str) -> bool:
            self.lookups.append(collection)
            return False

        def delete(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("an absent collection cannot be deleted")

    client = MissingCollectionClient()
    delete_document_points_if_collection_exists(
        client,  # type: ignore[arg-type]
        "pdf-bridge-customer-v1",
        uuid.uuid4(),
    )
    assert client.lookups == ["pdf-bridge-customer-v1"]


def test_reanalysis_deletes_screening_points_before_new_revision_upsert(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory)
    with session_factory() as session:
        document = _document()
        session.add_all([document, CollectionEpoch(collection_key="customer", epoch=2)])
        session.flush()
        old_analysis = _analysis(session, document, revision=1, epoch=1)
        new_analysis = _analysis(session, document, revision=2, epoch=2)
        old_write = analysis_steps.enqueue_index_entry(
            session,
            document=document,
            analysis_id=old_analysis.id,
            target=IndexTarget.SCREENING,
            action=IndexAction.UPSERT,
            expected_points=1,
        )
        old_delete = analysis_steps.enqueue_index_entry(
            session,
            document=document,
            analysis_id=old_analysis.id,
            target=IndexTarget.SCREENING,
            action=IndexAction.DELETE,
            expected_points=0,
            collection_epoch=old_analysis.collection_epoch,
        )
        worker._queue_screening_revision_upsert(
            session,
            document,
            new_analysis,
            expected_points=0,
        )
        session.commit()
        document_id = document.id
        old_ids = (old_write.id, old_delete.id)

    events: list[str] = []
    monkeypatch.setattr(
        worker_module,
        "ensure_screening_collection",
        lambda _client, *, dimension: None,
    )
    monkeypatch.setattr(
        worker_module,
        "delete_document_points",
        lambda *_args: events.append("delete-verified"),
    )
    monkeypatch.setattr(worker, "_load_points", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        worker_module,
        "upsert_chunk_points",
        lambda *_args, **_kwargs: events.append("upsert"),
    )
    monkeypatch.setattr(
        worker_module,
        "verify_document_point_count",
        lambda *_args, **_kwargs: events.append("upsert-verified"),
    )

    worker._drain_outbox(document_id)

    assert events == ["delete-verified", "upsert", "upsert-verified"]
    with session_factory() as session:
        old_entries = [session.get(IndexOutboxEntry, entry_id) for entry_id in old_ids]
        pending_sequence = session.scalars(
            select(IndexOutboxEntry)
            .where(
                IndexOutboxEntry.document_id == document_id,
                IndexOutboxEntry.state == OutboxState.DONE,
            )
            .order_by(IndexOutboxEntry.id)
        ).all()
        assert all(
            entry is not None and entry.state == OutboxState.SUPERSEDED for entry in old_entries
        )
        assert [entry.action for entry in pending_sequence] == [
            IndexAction.DELETE,
            IndexAction.UPSERT,
        ]
        assert [entry.collection_epoch for entry in pending_sequence] == [2, 2]


def test_discovery_retains_30_per_source_and_rrf_uses_separate_rankings(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory)
    active_ids = [uuid.uuid4() for _ in range(30)]
    screening_ids = [uuid.uuid4() for _ in range(30)]

    def hits(source: str) -> list[ChunkHit]:
        document_ids = active_ids if source == "active" else screening_ids
        return [
            ChunkHit(
                document_id=document_id,
                source=source,  # type: ignore[arg-type]
                chunk_id=f"{source}-{rank}",
                score=1.0 - rank / 100,
                rank=rank,
            )
            for rank, document_id in enumerate(document_ids, start=1)
        ]

    monkeypatch.setattr(
        worker_module,
        "ensure_active_collection",
        lambda *_args, **_kwargs: "active-customer-v1",
    )
    monkeypatch.setattr(
        worker_module,
        "ensure_screening_collection",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        worker_module,
        "query_dense",
        lambda _client, collection, **_kwargs: hits(
            "screening" if collection == SCREENING_COLLECTION else "active"
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "query_bm25",
        lambda _client, collection, **_kwargs: hits(
            "screening" if collection == SCREENING_COLLECTION else "active"
        ),
    )
    chunk = Chunk(
        index=0,
        text="substantive incoming chunk",
        page_start=1,
        page_end=1,
        token_count=3,
        text_hash="f" * 64,
    )

    dense, bm25 = worker._discover(
        uuid.uuid4(),
        "customer",
        1,
        chunks=[chunk],
        dense_vectors=[[0.25, 0.75]],
        sparse_vectors=[SparseVectorData(indices=(1,), values=(1.0,))],
    )

    assert len(dense[0]) == 60
    assert len(bm25[0]) == 60
    for result in (dense[0], bm25[0]):
        assert [hit.rank for hit in result if hit.source == "active"] == list(range(1, 31))
        assert [hit.rank for hit in result if hit.source == "screening"] == list(range(1, 31))

    active = active_ids[0]
    screening = screening_ids[0]
    fused = evaluate_candidates(
        dense_results=[
            [
                ChunkHit(active, "active", "active-1", 0.9, 1),
                ChunkHit(screening, "screening", "screening-1", 0.9, 1),
            ]
        ],
        bm25_results=[],
        filename_family_ids=set(),
        identical_text_ids=set(),
        sources={active: "active", screening: "screening"},
    )
    by_id = {candidate.document_id: candidate for candidate in fused}
    assert by_id[active].fused_score == by_id[screening].fused_score
    assert by_id[active].fused_score == round(1 / 61, 6)
