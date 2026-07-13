from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import pdf_bridge.managers.worker as worker_module
from pdf_bridge.core.config import Settings
from pdf_bridge.managers.worker import AnalysisWorker, WorkerProviders
from pdf_bridge.persistence.models import (
    AnalysisStatus,
    Document,
    DocumentAnalysis,
    DocumentArtifact,
    DocumentState,
    IndexOutboxEntry,
    OperationPhase,
    OperationState,
    OperationType,
    OutboxState,
    ScanState,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services.chunking import PageText
from pdf_bridge.services.embeddings import EmbeddingConfig
from pdf_bridge.services.extraction import ExtractedDocument
from pdf_bridge.services.vector_index import VectorIndexUnavailableError


def _queued_analysis(session: Session) -> tuple[Document, WorkOperation]:
    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        original_filename="Quarterly operating policy.pdf",
        normalized_filename="quarterly operating policy.pdf",
        storage_key=f"objects/{document_id}.pdf",
        size_bytes=512,
        sha256=hashlib.sha256(document_id.bytes).hexdigest(),
        content_type="application/pdf",
        idempotency_key=f"analysis-orchestration-{document_id}",
        state=DocumentState.ANALYZING,
        collection_key="customer",
        collection_epoch=1,
        scan_state=ScanState.CLEAN,
        scan_engine="test-clamd",
        scanned_at=utc_now(),
        uploader_identity="analysis-test@example.test",
    )
    operation = WorkOperation(
        document=document,
        operation_type=OperationType.ANALYZE,
        state=OperationState.RUNNING,
        phase=OperationPhase.EXTRACTING,
        attempt=1,
        worker_id="analysis-orchestration-test",
        started_at=utc_now(),
    )
    session.add_all([document, operation])
    session.flush()
    return document, operation


def _providers() -> tuple[WorkerProviders, httpx.Client]:
    client = httpx.Client()
    return (
        WorkerProviders(
            qdrant=object(),  # type: ignore[arg-type]
            embedding=EmbeddingConfig(
                api_url="https://embedding.invalid/v1",
                model_id="embedding-test-v1",
                dimension=2,
            ),
            http_client=client,
        ),
        client,
    )


def _worker(
    settings: Settings,
    session_factory: sessionmaker[Session],
    providers: WorkerProviders,
) -> AnalysisWorker:
    configured = settings.model_copy(
        update={
            "embedding_model_id": "embedding-test-v1",
            "embedding_dimension": 2,
        }
    )
    return AnalysisWorker(
        settings=configured,
        session_factory=session_factory,
        providers=providers,
        worker_id="analysis-orchestration-test",
    )


def _install_extraction_and_embedding_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    text = (
        "Durable bridge ingestion verifies every document before publication. "
        "Operators review semantic overlap, contradictions, revisions, and dependencies. "
        "Private screening content never becomes available to external retrieval. "
        "Replacement deletes and verifies old points before publishing new content."
    )
    monkeypatch.setattr(
        worker_module,
        "extract_pdf_text",
        lambda _path, _limits: ExtractedDocument(
            page_count=1,
            pages=[PageText(number=1, text=text)],
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "embed_texts",
        lambda _config, texts, *, client: [[0.25, 0.75] for _text in texts],
    )


def _complete_pending_outbox(
    session_factory: sessionmaker[Session], document_id: uuid.UUID
) -> None:
    with session_factory() as session:
        entries = session.scalars(
            select(IndexOutboxEntry).where(
                IndexOutboxEntry.document_id == document_id,
                IndexOutboxEntry.state == OutboxState.PENDING,
            )
        ).all()
        for entry in entries:
            entry.state = OutboxState.DONE
            entry.completed_at = utc_now()
        session.commit()


def _raise_qdrant_unavailable(_worker: AnalysisWorker, _document_id: uuid.UUID) -> None:
    raise VectorIndexUnavailableError("Qdrant is unavailable")


def test_clear_analysis_queues_ingestion_automatically(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers, http_client = _providers()
    worker = _worker(settings, session_factory, providers)
    _install_extraction_and_embedding_fakes(monkeypatch)
    monkeypatch.setattr(
        AnalysisWorker,
        "_drain_outbox",
        lambda _self, document_id: _complete_pending_outbox(session_factory, document_id),
    )
    monkeypatch.setattr(
        AnalysisWorker,
        "_discover",
        lambda _self, _document_id, _collection_key, _epoch, **_kwargs: ([[]], [[]]),
    )

    with session_factory() as session:
        document, operation = _queued_analysis(session)
        session.commit()
        document_id = document.id
        operation_id = operation.id

    try:
        worker._run_analyze(operation_id, document_id)
    finally:
        http_client.close()

    with session_factory() as session:
        document = session.get(Document, document_id)
        analysis_operation = session.get(WorkOperation, operation_id)
        analysis = session.scalar(
            select(DocumentAnalysis).where(DocumentAnalysis.document_id == document_id)
        )
        ingest_operations = list(
            session.scalars(
                select(WorkOperation).where(
                    WorkOperation.document_id == document_id,
                    WorkOperation.operation_type == OperationType.INGEST,
                )
            ).all()
        )

        assert document is not None
        assert analysis_operation is not None
        assert analysis is not None
        assert document.state == DocumentState.INGESTING
        assert analysis.status == AnalysisStatus.COMPLETE
        assert analysis.semantic_complete is True
        assert analysis.classification_complete is True
        assert analysis.auto_ingest_eligible is True
        assert analysis_operation.state == OperationState.SUCCEEDED
        assert analysis_operation.phase == OperationPhase.COMPLETE
        assert len(ingest_operations) == 1
        assert ingest_operations[0].state == OperationState.QUEUED


def test_qdrant_outage_retains_analysis_and_requires_review(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers, http_client = _providers()
    worker = _worker(settings, session_factory, providers)
    _install_extraction_and_embedding_fakes(monkeypatch)
    monkeypatch.setattr(
        AnalysisWorker,
        "_drain_outbox",
        _raise_qdrant_unavailable,
    )

    with session_factory() as session:
        document, operation = _queued_analysis(session)
        session.commit()
        document_id = document.id
        operation_id = operation.id

    try:
        worker._run_analyze(operation_id, document_id)
    finally:
        http_client.close()

    with session_factory() as session:
        document = session.get(Document, document_id)
        analysis_operation = session.get(WorkOperation, operation_id)
        analysis = session.scalar(
            select(DocumentAnalysis).where(DocumentAnalysis.document_id == document_id)
        )
        ingest_count = (
            session.scalar(
                select(WorkOperation)
                .where(
                    WorkOperation.document_id == document_id,
                    WorkOperation.operation_type == OperationType.INGEST,
                )
                .limit(1)
            )
            is not None
        )

        assert document is not None
        assert analysis_operation is not None
        assert analysis is not None
        artifact_kinds = set(
            session.scalars(
                select(DocumentArtifact.kind).where(DocumentArtifact.analysis_id == analysis.id)
            ).all()
        )
        assert document.state == DocumentState.REVIEW_REQUIRED
        assert analysis.status == AnalysisStatus.COMPLETE
        assert analysis.semantic_complete is False
        assert analysis.classification_complete is False
        assert analysis.screening_indexed is False
        assert analysis.auto_ingest_eligible is False
        assert analysis.incomplete_reasons == ["semantic-check-unavailable: Qdrant is unavailable"]
        assert analysis_operation.state == OperationState.SUCCEEDED
        assert analysis_operation.phase == OperationPhase.AWAITING_DECISION
        assert artifact_kinds == {"extracted_text", "chunks", "vectors", "candidates"}
        assert ingest_count is False
