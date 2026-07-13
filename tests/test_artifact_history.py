from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import pdf_bridge.managers.worker as worker_module
from pdf_bridge.core.config import Settings
from pdf_bridge.managers.worker import (
    AnalysisWorker,
    ProviderUnavailableError,
    WorkerProviders,
)
from pdf_bridge.persistence.models import (
    AnalysisChunk,
    AnalysisStatus,
    AuditEvent,
    CollectionEpoch,
    Document,
    DocumentAnalysis,
    DocumentArtifact,
    DocumentState,
    OperationPhase,
    OperationState,
    OperationType,
    ScanState,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services import analysis as analysis_steps
from pdf_bridge.services import artifacts as artifact_store
from pdf_bridge.services.chunking import Chunk, PageText
from pdf_bridge.services.extraction import ExtractedDocument
from pdf_bridge.services.storage import (
    StorageLayout,
    resolve_storage_key,
    storage_key_for,
)
from pdf_bridge.services.vector_index import VectorIndexError


def _document(
    state: DocumentState = DocumentState.ANALYZING,
    *,
    storage_key: str | None = None,
) -> Document:
    document_id = uuid.uuid4()
    return Document(
        id=document_id,
        original_filename=f"{document_id}.pdf",
        normalized_filename=f"{document_id}.pdf",
        storage_key=storage_key,
        size_bytes=100,
        sha256=hashlib.sha256(document_id.bytes).hexdigest(),
        idempotency_key=f"artifact-{document_id}",
        state=state,
        collection_key="customer",
        collection_epoch=1,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="artifact-test",
    )


def _analysis(
    document: Document,
    *,
    revision: int,
    epoch: int = 1,
    status: AnalysisStatus = AnalysisStatus.COMPLETE,
) -> DocumentAnalysis:
    return DocumentAnalysis(
        document=document,
        revision=revision,
        status=status,
        pipeline_fingerprint=f"artifact-history-v{revision}",
        collection_epoch=epoch,
        page_count=1,
        chunk_count=1,
        text_sha256=hashlib.sha256(f"revision-{revision}".encode()).hexdigest(),
        semantic_complete=status == AnalysisStatus.COMPLETE,
        classification_complete=status == AnalysisStatus.COMPLETE,
        completed_at=utc_now() if status != AnalysisStatus.RUNNING else None,
    )


def _worker(settings: Settings, session_factory: sessionmaker[Session]) -> AnalysisWorker:
    return AnalysisWorker(
        settings=settings.model_copy(update={"embedding_dimension": 2}),
        session_factory=session_factory,
        providers=WorkerProviders(qdrant=object()),  # type: ignore[arg-type]
    )


def _write(
    layout: StorageLayout,
    document_id: uuid.UUID,
    analysis_id: uuid.UUID,
    kind: str,
    payload: dict[str, object],
) -> artifact_store.ArtifactRecord:
    return artifact_store.write_artifact(
        layout,
        document_id,
        analysis_id,
        kind,
        {"analysis_id": str(analysis_id), **payload},
    )


def test_artifacts_are_revision_scoped_immutable_and_verified(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    layout = StorageLayout.from_root(settings.storage_root)
    with session_factory() as session:
        document = _document()
        first_analysis = _analysis(document, revision=1)
        second_analysis = _analysis(document, revision=2)
        session.add_all([document, first_analysis, second_analysis])
        session.flush()

        first = _write(
            layout,
            document.id,
            first_analysis.id,
            "findings",
            {"raw_output": "first revision"},
        )
        second = _write(
            layout,
            document.id,
            second_analysis.id,
            "findings",
            {"raw_output": "second revision"},
        )
        analysis_steps.record_artifact(session, first_analysis, first)
        analysis_steps.record_artifact(session, second_analysis, second)
        with pytest.raises(ValueError, match="another analysis"):
            analysis_steps.record_artifact(session, second_analysis, first)
        session.commit()

    assert first.storage_key != second.storage_key
    assert f"/{first.analysis_id}/" in first.storage_key
    assert f"/{second.analysis_id}/" in second.storage_key
    assert (
        artifact_store.write_artifact(
            layout,
            first.document_id,
            first.analysis_id,
            first.kind,
            {"analysis_id": str(first.analysis_id), "raw_output": "first revision"},
        )
        == first
    )
    with pytest.raises(ValueError, match="different content"):
        _write(
            layout,
            first.document_id,
            first.analysis_id,
            first.kind,
            {"raw_output": "mutated revision"},
        )

    first_payload = artifact_store.read_artifact(
        layout,
        first.storage_key,
        expected_sha256=first.sha256,
        expected_size_bytes=first.size_bytes,
    )
    second_payload = artifact_store.read_artifact(
        layout,
        second.storage_key,
        expected_sha256=second.sha256,
        expected_size_bytes=second.size_bytes,
    )
    assert first_payload["raw_output"] == "first revision"
    assert second_payload["raw_output"] == "second revision"

    first_path = resolve_storage_key(layout, first.storage_key)
    corrupted = bytearray(first_path.read_bytes())
    corrupted[-1] ^= 1
    first_path.write_bytes(corrupted)
    with pytest.raises(ValueError, match="hash does not match"):
        artifact_store.read_artifact(
            layout,
            first.storage_key,
            expected_sha256=first.sha256,
            expected_size_bytes=first.size_bytes,
        )

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DocumentArtifact)) == 2


def test_manifest_v2_is_canonical_and_binds_every_revision() -> None:
    document_id = uuid.uuid4()
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    history = [
        {
            "analysis_id": str(second_id),
            "revision": 2,
            "status": "COMPLETE",
            "collection_epoch": 2,
            "pipeline_fingerprint": "pipeline-v2",
            "text_sha256": "2" * 64,
            "created_at": "2026-01-02T00:00:00+00:00",
            "completed_at": "2026-01-02T00:01:00+00:00",
            "artifacts": [
                {"kind": "vectors", "sha256": "b" * 64, "size_bytes": 20},
                {"kind": "chunks", "sha256": "a" * 64, "size_bytes": 10},
            ],
        },
        {
            "analysis_id": str(first_id),
            "revision": 1,
            "status": "FAILED",
            "collection_epoch": 1,
            "pipeline_fingerprint": "pipeline-v1",
            "text_sha256": "1" * 64,
            "created_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:01:00+00:00",
            "artifacts": [{"kind": "chunks", "sha256": "c" * 64, "size_bytes": 30}],
        },
    ]
    arguments = {
        "document_id": document_id,
        "content_sha256": "d" * 64,
        "text_hash": "e" * 64,
        "decision_action": "keep",
        "decision_actor": "operator",
        "decision_target": None,
        "uploaded_at": "2026-01-01T00:00:00+00:00",
        "decided_at": "2026-01-03T00:00:00+00:00",
    }

    manifest_hash = artifact_store.analysis_manifest_hash(
        analysis_history=history,
        **arguments,  # type: ignore[arg-type]
    )
    reversed_hash = artifact_store.analysis_manifest_hash(
        analysis_history=[
            {**history[1], "artifacts": list(reversed(history[1]["artifacts"]))},
            {**history[0], "artifacts": list(reversed(history[0]["artifacts"]))},
        ],
        **arguments,  # type: ignore[arg-type]
    )
    changed_history = [dict(item) for item in history]
    changed_history[1] = {
        **changed_history[1],
        "artifacts": [{"kind": "chunks", "sha256": "f" * 64, "size_bytes": 30}],
    }

    assert manifest_hash == reversed_hash
    assert manifest_hash != artifact_store.analysis_manifest_hash(
        analysis_history=changed_history,
        **arguments,  # type: ignore[arg-type]
    )


def test_interrupted_analysis_is_retained_before_next_revision(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(settings, session_factory)
    layout = StorageLayout.from_root(settings.storage_root)
    with session_factory() as session:
        document = _document(storage_key="objects/test-input.pdf")
        stale_analysis = _analysis(
            document,
            revision=1,
            status=AnalysisStatus.RUNNING,
        )
        document.analysis_revision = 1
        operation = WorkOperation(
            document=document,
            operation_type=OperationType.ANALYZE,
            state=OperationState.RUNNING,
            phase=OperationPhase.EXTRACTING,
            attempt=1,
            worker_id=worker.worker_id,
        )
        session.add_all([document, stale_analysis, operation])
        session.flush()
        stale_record = _write(
            layout,
            document.id,
            stale_analysis.id,
            "findings",
            {"raw_output": "partial call"},
        )
        analysis_steps.record_artifact(session, stale_analysis, stale_record)
        session.commit()
        document_id = document.id
        operation_id = operation.id
        stale_analysis_id = stale_analysis.id

    page = PageText(number=1, text="A sufficiently long recovered analysis sentence. " * 3)
    chunk = Chunk(
        index=0,
        text=page.text,
        page_start=1,
        page_end=1,
        token_count=18,
        text_hash=hashlib.sha256(page.text.encode()).hexdigest(),
    )
    monkeypatch.setattr(
        worker_module,
        "extract_pdf_text",
        lambda *_args, **_kwargs: ExtractedDocument(page_count=1, pages=[page]),
    )
    monkeypatch.setattr(worker_module, "chunk_pages", lambda *_args, **_kwargs: [chunk])
    monkeypatch.setattr(worker, "_run_comparison", lambda *_args, **_kwargs: None)

    worker._run_analyze(operation_id, document_id)

    with session_factory() as session:
        analyses = list(
            session.scalars(
                select(DocumentAnalysis)
                .where(DocumentAnalysis.document_id == document_id)
                .order_by(DocumentAnalysis.revision)
            ).all()
        )
        assert [analysis.revision for analysis in analyses] == [1, 2]
        assert analyses[0].id == stale_analysis_id
        assert analyses[0].status == AnalysisStatus.FAILED
        assert analyses[0].completed_at is not None
        assert analyses[0].incomplete_reasons == ["analysis-interrupted"]
        assert [artifact.kind for artifact in analyses[0].artifacts] == ["findings"]
        assert {artifact.kind for artifact in analyses[1].artifacts} == {
            "chunks",
            "extracted_text",
        }
    assert resolve_storage_key(layout, stale_record.storage_key).exists()


def test_latest_vectors_do_not_reuse_history_and_are_epoch_correlated(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _worker(settings, session_factory)
    layout = StorageLayout.from_root(settings.storage_root)
    with session_factory() as session:
        document = _document(DocumentState.REPLACING)
        first_analysis = _analysis(document, revision=1, epoch=1)
        second_analysis = _analysis(document, revision=2, epoch=2)
        second_analysis.pipeline_fingerprint = worker._fingerprint()
        session.add_all(
            [
                document,
                first_analysis,
                second_analysis,
                CollectionEpoch(collection_key="customer", epoch=2),
            ]
        )
        session.flush()
        for analysis, text in (
            (first_analysis, "first revision chunk"),
            (second_analysis, "second revision chunk"),
        ):
            session.add(
                AnalysisChunk(
                    id=uuid.uuid4(),
                    analysis=analysis,
                    document_id=document.id,
                    chunk_index=0,
                    page_start=1,
                    page_end=1,
                    token_count=3,
                    text_hash=hashlib.sha256(text.encode()).hexdigest(),
                    text=text,
                )
            )
        first_vectors = _write(
            layout,
            document.id,
            first_analysis.id,
            "vectors",
            {
                "pipeline_fingerprint": first_analysis.pipeline_fingerprint,
                "embedding_model_id": settings.embedding_model_id,
                "dimension": 2,
                "dense": [[1.0, 0.0]],
                "sparse": [{"indices": [1], "values": [1.0]}],
            },
        )
        analysis_steps.record_artifact(session, first_analysis, first_vectors)
        session.commit()
        document_id = document.id
        first_analysis_id = first_analysis.id
        second_analysis_id = second_analysis.id

    with pytest.raises(ProviderUnavailableError):
        worker._ensure_vectors_ready(document_id)

    with session_factory() as session:
        second_analysis = session.get(DocumentAnalysis, second_analysis_id)
        assert second_analysis is not None
        second_vectors = _write(
            layout,
            document_id,
            second_analysis_id,
            "vectors",
            {
                "pipeline_fingerprint": second_analysis.pipeline_fingerprint,
                "embedding_model_id": settings.embedding_model_id,
                "dimension": 2,
                "dense": [[0.0, 1.0]],
                "sparse": [{"indices": [2], "values": [2.0]}],
            },
        )
        analysis_steps.record_artifact(session, second_analysis, second_vectors)
        session.commit()

    first_points = worker._load_points(
        document_id,
        first_analysis_id,
        collection_epoch=1,
    )
    second_points = worker._load_points(
        document_id,
        second_analysis_id,
        collection_epoch=2,
    )
    assert first_points[0].analysis_id == first_analysis_id
    assert first_points[0].dense == (1.0, 0.0)
    assert second_points[0].analysis_id == second_analysis_id
    assert second_points[0].dense == (0.0, 1.0)
    with pytest.raises(VectorIndexError, match="epoch does not match"):
        worker._load_points(
            document_id,
            second_analysis_id,
            collection_epoch=1,
        )

    vectors_path = resolve_storage_key(layout, second_vectors.storage_key)
    corrupted = bytearray(vectors_path.read_bytes())
    corrupted[-1] ^= 1
    vectors_path.write_bytes(corrupted)
    with pytest.raises(VectorIndexError, match="integrity checks"):
        worker._ensure_vectors_ready(document_id)


def test_purge_removes_all_revision_directories_and_keeps_content_free_history(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _worker(settings, session_factory)
    layout = StorageLayout.from_root(settings.storage_root)
    secret = "FULL-HISTORY-SECRET"
    document_id = uuid.uuid4()
    storage_key = storage_key_for(document_id)
    canonical_path = resolve_storage_key(layout, storage_key)
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_bytes(b"%PDF-1.4\nprivate bytes\n%%EOF\n")

    with session_factory() as session:
        document = _document(DocumentState.CLEANUP_PENDING, storage_key=storage_key)
        document.id = document_id
        document.cleanup_target = DocumentState.CANCELLED
        first_analysis = _analysis(document, revision=1, status=AnalysisStatus.FAILED)
        second_analysis = _analysis(document, revision=2)
        session.add_all([document, first_analysis, second_analysis])
        session.flush()
        for analysis in (first_analysis, second_analysis):
            record = _write(
                layout,
                document.id,
                analysis.id,
                "findings",
                {"raw_output": f"{secret}-{analysis.revision}"},
            )
            analysis_steps.record_artifact(session, analysis, record)
        session.commit()

    with session_factory() as session:
        document = session.get(Document, document_id)
        assert document is not None
        worker._purge_document_content(
            session,
            document,
            tombstone=DocumentState.CANCELLED,
        )
        session.commit()

    assert not canonical_path.exists()
    assert not resolve_storage_key(layout, f"analysis/{document_id}").exists()
    with session_factory() as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.analysis_manifest_hash is not None
        assert session.scalar(select(func.count()).select_from(DocumentAnalysis)) == 0
        assert session.scalar(select(func.count()).select_from(DocumentArtifact)) == 0
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "analysis_data_purged")
        )
        assert event is not None
        assert event.details["analysis_count"] == 2
        assert event.details["artifact_count"] == 2
        assert event.details["artifact_kinds"] == ["findings"]
        assert secret not in json.dumps(event.details, sort_keys=True)
