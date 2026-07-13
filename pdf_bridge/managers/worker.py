"""Lifespan-owned durable analysis worker.

The worker runs inside the single Litestar process with two execution slots,
durable SQL operations, leases with heartbeats, and per-collection locks. It
never holds a database transaction across parsing, model, or Qdrant calls:
each step opens a short session, commits, and releases before slow work.

Crash safety comes from resumability, not transactions: operations are
re-claimed after lease expiry, the index outbox is drained idempotently
(deterministic point IDs, ``wait=true``, exact count verification), and every
executor re-checks durable state before acting.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta

import httpx
from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.models import (
    AnalysisChunk,
    AnalysisStatus,
    Document,
    DocumentAnalysis,
    DocumentState,
    IndexAction,
    IndexOutboxEntry,
    IndexTarget,
    OperationPhase,
    OperationState,
    OperationType,
    OutboxState,
    ReplacementState,
    ReplacementWorkflow,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services import analysis as analysis_steps
from pdf_bridge.services import artifacts as artifact_store
from pdf_bridge.services import intake
from pdf_bridge.services.bm25 import SparseVectorData, bm25_document_vector
from pdf_bridge.services.candidates import (
    BM25_TOP_K,
    DENSE_TOP_K,
    CandidateSource,
    ChunkHit,
    evaluate_candidates,
)
from pdf_bridge.services.chunking import (
    Chunk,
    InsufficientTextError,
    TextBudgetExceededError,
    chunk_pages,
    document_text_hash,
)
from pdf_bridge.services.classification import (
    ClassificationUnavailableError,
    LlmConfig,
    classify_candidate,
)
from pdf_bridge.services.embeddings import EmbeddingConfig, EmbeddingError, embed_texts
from pdf_bridge.services.extraction import (
    ExtractionInfrastructureError,
    ExtractionLimits,
    ExtractionRejectedError,
    extract_pdf_text,
)
from pdf_bridge.services.fingerprint import pipeline_fingerprint
from pdf_bridge.services.storage import StorageLayout, remove_storage_key, resolve_storage_key
from pdf_bridge.services.vector_index import (
    SCREENING_COLLECTION,
    ChunkPoint,
    VectorIndexError,
    VectorIndexUnavailableError,
    delete_document_points,
    delete_document_points_if_collection_exists,
    ensure_active_collection,
    ensure_screening_collection,
    physical_collection_name,
    publish_document_points,
    query_bm25,
    query_dense,
    upsert_chunk_points,
    verify_document_point_count,
)

logger = logging.getLogger(__name__)

WORKER_SLOTS = 2
MAX_AUTOMATIC_INGEST_ATTEMPTS = 5


class ProviderUnavailableError(RuntimeError):
    """A required external provider was unavailable; the step is retryable."""


@dataclass(slots=True)
class WorkerProviders:
    """External providers the worker may use; any of them may be absent."""

    qdrant: QdrantClient | None = None
    embedding: EmbeddingConfig | None = None
    llm: LlmConfig | None = None
    http_client: httpx.Client | None = None

    def semantic_ready(self) -> bool:
        return (
            self.qdrant is not None and self.embedding is not None and self.http_client is not None
        )


def providers_from_settings(
    settings: Settings, *, http_client: httpx.Client | None
) -> WorkerProviders:
    """Build the provider bundle from validated deployment settings."""

    qdrant: QdrantClient | None = None
    if settings.qdrant_url:
        qdrant = QdrantClient(
            url=settings.qdrant_url,
            api_key=(
                settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
            ),
            timeout=settings.qdrant_timeout,
        )
    embedding: EmbeddingConfig | None = None
    if settings.embedding_api_url and settings.embedding_model_id:
        embedding = EmbeddingConfig(
            api_url=settings.embedding_api_url,
            model_id=settings.embedding_model_id,
            dimension=settings.embedding_dimension or 0,
            api_token=(
                settings.embedding_api_token.get_secret_value()
                if settings.embedding_api_token
                else None
            ),
            timeout=settings.embedding_timeout,
        )
    llm: LlmConfig | None = None
    if settings.llm_api_url and settings.llm_classifier_model and settings.llm_verifier_model:
        llm = LlmConfig(
            api_url=settings.llm_api_url,
            classifier_model=settings.llm_classifier_model,
            verifier_model=settings.llm_verifier_model,
            api_token=(
                settings.llm_api_token.get_secret_value() if settings.llm_api_token else None
            ),
            timeout=settings.llm_timeout,
        )
    return WorkerProviders(qdrant=qdrant, embedding=embedding, llm=llm, http_client=http_client)


class AnalysisWorker:
    """Durable two-slot worker owning analysis, ingestion, and cleanup."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        providers: WorkerProviders,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.providers = providers
        self.worker_id = worker_id or f"bridge-worker-{uuid.uuid4()}"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._claim_lock = threading.Lock()
        self._busy_lock = threading.Lock()
        self._busy_collections: set[str] = set()
        self._threads: list[threading.Thread] = []

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Recover durable state and launch the slot and heartbeat threads."""

        self.recover_leases()
        for slot in range(WORKER_SLOTS):
            thread = threading.Thread(
                target=self._slot_loop, name=f"{self.worker_id}-slot-{slot}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, name=f"{self.worker_id}-heartbeat", daemon=True
        )
        heartbeat.start()
        self._threads.append(heartbeat)
        self.notify()

    def stop(self) -> None:
        """Signal every thread to finish its current operation and exit."""

        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            # Provider/parser calls are bounded by their own configured
            # timeouts.  Do not close the shared engine or HTTP client while a
            # slot still owns them merely because an arbitrary join timeout
            # elapsed.
            thread.join()
        self._threads.clear()

    def notify(self) -> None:
        """Wake the slots because new durable work exists."""

        self._wake.set()

    # -- durable recovery ------------------------------------------------------

    def recover_leases(self) -> int:
        """Return expired RUNNING operations to the queue after a crash."""

        recovered = 0
        with self.session_factory() as session:
            now = utc_now()
            stale = session.scalars(
                select(WorkOperation).where(
                    WorkOperation.state == OperationState.RUNNING,
                    WorkOperation.lease_expires_at.is_not(None),
                    WorkOperation.lease_expires_at <= now,
                )
            ).all()
            for operation in stale:
                operation.state = OperationState.QUEUED
                operation.worker_id = None
                operation.lease_expires_at = None
                operation.heartbeat_at = None
                recovered += 1
            if recovered:
                session.commit()
            else:
                session.rollback()
        return recovered

    # -- synchronous draining (tests and CLI) -----------------------------------

    def run_available(self, *, max_operations: int | None = None) -> int:
        """Process queued operations synchronously until none remain."""

        completed = 0
        while max_operations is None or completed < max_operations:
            claimed = self._claim_next()
            if claimed is None:
                break
            self._execute(claimed)
            completed += 1
        return completed

    # -- threads ----------------------------------------------------------------

    def _slot_loop(self) -> None:
        while not self._stop.is_set():
            claimed = None
            try:
                claimed = self._claim_next()
                if claimed is None:
                    self._wake.wait(timeout=self.settings.worker_poll_seconds)
                    self._wake.clear()
                    continue
                self._execute(claimed)
            except Exception:
                logger.exception("worker slot failed unexpectedly")
                if claimed is not None:
                    self._release_collection(claimed[2])

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(timeout=self.settings.worker_heartbeat_seconds):
            try:
                with self.session_factory() as session:
                    now = utc_now()
                    running = session.scalars(
                        select(WorkOperation).where(
                            WorkOperation.state == OperationState.RUNNING,
                            WorkOperation.worker_id == self.worker_id,
                        )
                    ).all()
                    for operation in running:
                        operation.heartbeat_at = now
                        operation.lease_expires_at = now + timedelta(
                            seconds=self.settings.worker_lease_seconds
                        )
                    if running:
                        session.commit()
                    else:
                        session.rollback()
            except Exception:
                logger.exception("worker heartbeat failed")

    # -- claiming -----------------------------------------------------------------

    def _claim_next(self) -> tuple[uuid.UUID, uuid.UUID, str, OperationType] | None:
        """Atomically claim the oldest queued operation of a free collection."""

        # Lease expiry is not a startup-only event.  Reconcile on every polling
        # pass so a worker that dies after this process started cannot strand
        # its operation indefinitely.
        self.recover_leases()
        with self._claim_lock, self.session_factory() as session:
            with self._busy_lock:
                busy = set(self._busy_collections)
            query = (
                select(WorkOperation)
                .join(Document, WorkOperation.document_id == Document.id)
                .where(WorkOperation.state == OperationState.QUEUED)
                .order_by(WorkOperation.created_at, WorkOperation.id)
                .limit(1)
            )
            if busy:
                query = query.where(Document.collection_key.not_in(busy))
            operation = session.scalar(query)
            if operation is None:
                session.rollback()
                return None
            now = utc_now()
            operation.state = OperationState.RUNNING
            operation.worker_id = self.worker_id
            operation.started_at = operation.started_at or now
            operation.heartbeat_at = now
            operation.lease_expires_at = now + timedelta(seconds=self.settings.worker_lease_seconds)
            collection_key = operation.document.collection_key
            claimed = (
                operation.id,
                operation.document_id,
                collection_key,
                operation.operation_type,
            )
            session.commit()
            with self._busy_lock:
                self._busy_collections.add(collection_key)
            return claimed

    def _release_collection(self, collection_key: str) -> None:
        with self._busy_lock:
            self._busy_collections.discard(collection_key)

    # -- execution ------------------------------------------------------------------

    def _execute(self, claimed: tuple[uuid.UUID, uuid.UUID, str, OperationType]) -> None:
        operation_id, document_id, collection_key, operation_type = claimed
        try:
            if operation_type == OperationType.ANALYZE:
                self._run_analyze(operation_id, document_id)
            elif operation_type == OperationType.INGEST:
                self._run_ingest(operation_id, document_id)
            elif operation_type == OperationType.DELETE:
                self._run_delete(operation_id, document_id)
            else:
                self._run_cleanup(operation_id, document_id)
        except Exception:
            logger.exception(
                "operation execution crashed",
                extra={"operation_id": str(operation_id), "document_id": str(document_id)},
            )
            self._fail_crashed_operation(
                operation_id,
                document_id,
                operation_type=operation_type,
            )
        finally:
            self._release_collection(collection_key)

    # -- shared helpers ----------------------------------------------------------------

    def _layout(self) -> StorageLayout:
        return StorageLayout.from_root(self.settings.storage_root)

    def _fingerprint(self) -> str:
        return pipeline_fingerprint(
            embedding_model_id=self.settings.embedding_model_id,
            embedding_dimension=self.settings.embedding_dimension,
            classifier_model=self.settings.llm_classifier_model,
            verifier_model=self.settings.llm_verifier_model,
        )

    def _set_phase(self, operation_id: uuid.UUID, phase: OperationPhase) -> None:
        with self.session_factory() as session:
            operation = session.get(WorkOperation, operation_id)
            if operation is not None and operation.state == OperationState.RUNNING:
                operation.phase = phase
                session.commit()
            else:
                session.rollback()

    def _fail_operation(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        error: str,
        retryable: bool = True,
        document_state: DocumentState | None = None,
    ) -> None:
        with self.session_factory() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if operation is not None and operation.state == OperationState.RUNNING:
                operation.state = OperationState.FAILED
                operation.error = error[:4000]
                operation.retryable = retryable
                operation.completed_at = utc_now()
                operation.lease_expires_at = None
                operation.heartbeat_at = None
                operation.worker_id = None
                if operation.replacement_id is not None:
                    workflow = session.get(ReplacementWorkflow, operation.replacement_id)
                    if workflow is not None:
                        workflow.error = error[:2000]
            if document is not None:
                document.last_error = error[:4000]
                if document_state is not None:
                    document.state = document_state
            session.commit()

    def _fail_crashed_operation(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        operation_type: OperationType,
    ) -> None:
        """Make every unexpected executor failure visible and retryable."""

        if operation_type == OperationType.ANALYZE:
            failed_state = DocumentState.ANALYZING
        elif operation_type == OperationType.DELETE:
            failed_state = DocumentState.DELETE_FAILED
        elif operation_type == OperationType.CLEANUP:
            failed_state = DocumentState.CLEANUP_FAILED
        else:
            with self.session_factory() as session:
                operation = session.get(WorkOperation, operation_id)
                failed_state = (
                    DocumentState.REPLACE_FAILED
                    if operation is not None and operation.replacement_id is not None
                    else DocumentState.INGEST_FAILED
                )
                session.rollback()
        self._fail_operation(
            operation_id,
            document_id,
            error="The worker crashed while executing this operation.",
            document_state=failed_state,
        )

    def _succeed_operation(
        self, session: Session, operation: WorkOperation, *, phase: OperationPhase
    ) -> None:
        operation.state = OperationState.SUCCEEDED
        operation.phase = phase
        operation.error = None
        operation.completed_at = utc_now()
        operation.lease_expires_at = None
        operation.heartbeat_at = None
        operation.worker_id = None

    def _requeue_or_fail_ingest(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        error: str,
        failed_state: DocumentState,
    ) -> None:
        """Retry provider outages automatically up to the attempt budget."""

        with self.session_factory() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if operation is None or document is None:
                session.rollback()
                return
            operation.state = OperationState.FAILED
            operation.error = error[:4000]
            operation.retryable = True
            operation.completed_at = utc_now()
            operation.lease_expires_at = None
            operation.heartbeat_at = None
            operation.worker_id = None
            document.last_error = error[:4000]
            if operation.replacement_id is not None:
                workflow = session.get(ReplacementWorkflow, operation.replacement_id)
                if workflow is not None:
                    workflow.error = error[:2000]
            if operation.attempt < MAX_AUTOMATIC_INGEST_ATTEMPTS:
                intake.enqueue_operation(
                    session,
                    document,
                    operation.operation_type,
                    replacement_id=operation.replacement_id,
                )
            else:
                document.state = failed_state
            session.commit()
        self.notify()

    # -- extraction and analysis -----------------------------------------------------

    def _extraction_limits(self) -> ExtractionLimits:
        return ExtractionLimits(
            max_pages=self.settings.analysis_max_pages,
            max_characters=self.settings.analysis_max_characters,
            cpu_seconds=self.settings.parse_cpu_seconds,
            memory_bytes=self.settings.parse_memory_bytes,
            wall_clock_seconds=self.settings.parse_wall_clock_seconds,
        )

    def _run_analyze(self, operation_id: uuid.UUID, document_id: uuid.UUID) -> None:
        layout = self._layout()
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            if document is None or operation is None:
                session.rollback()
                return
            if document.state != DocumentState.ANALYZING or not document.storage_key:
                operation.state = OperationState.CANCELLED
                operation.completed_at = utc_now()
                session.commit()
                return
            # A revision left RUNNING by a crash is retained as failed history,
            # then superseded by a new revision rather than resumed in place.
            stale = session.scalars(
                select(DocumentAnalysis).where(
                    DocumentAnalysis.document_id == document.id,
                    DocumentAnalysis.status == AnalysisStatus.RUNNING,
                )
            ).all()
            interrupted_at = utc_now()
            for item in stale:
                item.status = AnalysisStatus.FAILED
                item.completed_at = interrupted_at
                if "analysis-interrupted" not in item.incomplete_reasons:
                    item.incomplete_reasons = [
                        *item.incomplete_reasons,
                        "analysis-interrupted",
                    ]
            operation.phase = OperationPhase.EXTRACTING
            storage_key = document.storage_key
            collection_key = document.collection_key
            epoch = intake.collection_epoch(session, collection_key)
            session.commit()

        pdf_path = resolve_storage_key(layout, storage_key)
        try:
            extracted = extract_pdf_text(pdf_path, self._extraction_limits())
            chunks = chunk_pages(
                extracted.pages,
                max_pages=self.settings.analysis_max_pages,
                max_chars=self.settings.analysis_max_characters,
                max_chunks=self.settings.analysis_max_chunks,
            )
        except ExtractionRejectedError as exc:
            self._reject_document(operation_id, document_id, exc.reason, exc.detail)
            return
        except TextBudgetExceededError as exc:
            self._reject_document(operation_id, document_id, exc.limit_name, str(exc))
            return
        except InsufficientTextError as exc:
            self._reject_document(operation_id, document_id, "text-insufficient", str(exc))
            return
        except ExtractionInfrastructureError as exc:
            self._fail_operation(operation_id, document_id, error=str(exc))
            return

        text_sha256 = document_text_hash(extracted.pages)

        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            if document is None or operation is None:
                session.rollback()
                return
            analysis = analysis_steps.create_analysis_revision(
                session, document, pipeline_fingerprint=self._fingerprint(), epoch=epoch
            )
            analysis_steps.record_extraction(
                session,
                analysis,
                document,
                page_count=extracted.page_count,
                text_sha256=text_sha256,
                chunks=chunks,
            )
            analysis_steps.record_artifact(
                session,
                analysis,
                artifact_store.write_artifact(
                    layout,
                    document.id,
                    analysis.id,
                    "extracted_text",
                    {
                        "analysis_id": str(analysis.id),
                        "page_count": extracted.page_count,
                        "pages": [
                            {"number": page.number, "text": page.text} for page in extracted.pages
                        ],
                    },
                ),
            )
            analysis_steps.record_artifact(
                session,
                analysis,
                artifact_store.write_artifact(
                    layout,
                    document.id,
                    analysis.id,
                    "chunks",
                    {
                        "analysis_id": str(analysis.id),
                        "chunks": [
                            {
                                "index": chunk.index,
                                "page_start": chunk.page_start,
                                "page_end": chunk.page_end,
                                "token_count": chunk.token_count,
                                "text_hash": chunk.text_hash,
                                "text": chunk.text,
                            }
                            for chunk in chunks
                        ],
                    },
                ),
            )
            operation.phase = OperationPhase.COMPARING
            analysis_id = analysis.id
            session.commit()

        self._run_comparison(
            operation_id,
            document_id,
            analysis_id,
            chunks=chunks,
            layout=layout,
            epoch=epoch,
        )

    def _run_comparison(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        analysis_id: uuid.UUID,
        *,
        chunks: list[Chunk],
        layout: StorageLayout,
        epoch: int,
    ) -> None:
        incomplete_reasons: list[str] = []
        semantic_complete = False
        classification_complete = False
        screening_indexed = False

        # Filename and identical-text checks are local and always complete.
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            assert document is not None
            warning_pairs = intake.find_filename_warnings(
                session,
                collection_key=document.collection_key,
                filename=document.original_filename,
                exclude_document_id=document.id,
            )
            filename_warnings = [
                {
                    "kind": match.kind,
                    "similarity": match.similarity,
                    "shared_tokens": list(match.shared_family_tokens),
                    "matched": analysis_steps.document_snapshot(matched),
                }
                for matched, match in warning_pairs
            ]
            filename_family_ids = {matched.id for matched, _ in warning_pairs}
            identical_ids = {
                item.id
                for item in intake.find_identical_text_documents(
                    session,
                    collection_key=document.collection_key,
                    text_sha256=document.text_sha256 or "",
                    exclude_document_id=document.id,
                )
            }
            collection_key = document.collection_key
            session.rollback()

        dense_results: list[list[ChunkHit]] = []
        bm25_results: list[list[ChunkHit]] = []
        sparse_vectors = [bm25_document_vector(chunk.text) for chunk in chunks]

        if not self.providers.semantic_ready():
            incomplete_reasons.append("semantic-providers-not-configured")
        else:
            try:
                dense_vectors = embed_texts(
                    self.providers.embedding,  # type: ignore[arg-type]
                    [chunk.text for chunk in chunks],
                    client=self.providers.http_client,  # type: ignore[arg-type]
                )
                with self.session_factory() as session:
                    document = session.get(Document, document_id)
                    analysis = session.get(DocumentAnalysis, analysis_id)
                    assert document is not None and analysis is not None
                    analysis_steps.record_artifact(
                        session,
                        analysis,
                        artifact_store.write_artifact(
                            layout,
                            document.id,
                            analysis.id,
                            "vectors",
                            {
                                "analysis_id": str(analysis_id),
                                "pipeline_fingerprint": analysis.pipeline_fingerprint,
                                "embedding_model_id": self.settings.embedding_model_id,
                                "dimension": self.settings.embedding_dimension,
                                "dense": [list(vector) for vector in dense_vectors],
                                "sparse": [
                                    {
                                        "indices": list(vector.indices),
                                        "values": list(vector.values),
                                    }
                                    for vector in sparse_vectors
                                ],
                            },
                        ),
                    )
                    self._queue_screening_revision_upsert(
                        session,
                        document,
                        analysis,
                        expected_points=len(chunks),
                    )
                    session.commit()
                self._drain_outbox(document_id)
                screening_indexed = True

                dense_results, bm25_results = self._discover(
                    document_id,
                    collection_key,
                    epoch,
                    chunks=chunks,
                    dense_vectors=dense_vectors,
                    sparse_vectors=sparse_vectors,
                )
                semantic_complete = True
            except (EmbeddingError, VectorIndexUnavailableError, VectorIndexError) as exc:
                incomplete_reasons.append(f"semantic-check-unavailable: {exc}")

        with self.session_factory() as session:
            document = session.get(Document, document_id)
            analysis = session.get(DocumentAnalysis, analysis_id)
            assert document is not None and analysis is not None
            if screening_indexed:
                analysis.screening_indexed = True
            sources: dict[uuid.UUID, CandidateSource] = {}
            referenced = (
                {hit.document_id for hits in dense_results for hit in hits}
                | {hit.document_id for hits in bm25_results for hit in hits}
                | filename_family_ids
                | identical_ids
            )
            documents_by_id: dict[uuid.UUID, Document] = {}
            for referenced_id in referenced:
                record = session.get(Document, referenced_id)
                if record is None:
                    continue
                documents_by_id[referenced_id] = record
                sources[referenced_id] = (
                    "active" if record.state == DocumentState.INGESTED else "screening"
                )
            evaluated = evaluate_candidates(
                dense_results=dense_results,
                bm25_results=bm25_results,
                filename_family_ids=filename_family_ids,
                identical_text_ids=identical_ids,
                sources=sources,
            )
            candidates = analysis_steps.apply_candidates(
                session, analysis, evaluated, documents_by_id
            )
            analysis_steps.record_artifact(
                session,
                analysis,
                artifact_store.write_artifact(
                    layout,
                    document.id,
                    analysis.id,
                    "candidates",
                    {
                        "analysis_id": str(analysis_id),
                        "candidates": [
                            {
                                "document_id": str(item.matched_document_id),
                                "rank": item.rank,
                                "source": item.source,
                                "reasons": item.reasons,
                                "max_cosine": item.max_cosine,
                                "fused_score": item.fused_score,
                                "snapshot": item.document_snapshot,
                            }
                            for item in candidates
                        ],
                    },
                ),
            )
            candidate_ids = [item.id for item in candidates if item.classified]
            session.commit()

        classification_complete = self._classify_candidates(
            document_id,
            analysis_id,
            candidate_ids,
            layout=layout,
            incomplete_reasons=incomplete_reasons,
            semantic_complete=semantic_complete,
        )

        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            analysis = session.get(DocumentAnalysis, analysis_id)
            assert document is not None and operation is not None and analysis is not None
            analysis_steps.finalize_analysis(
                session,
                analysis,
                filename_warnings=filename_warnings,
                semantic_complete=semantic_complete,
                classification_complete=classification_complete,
                incomplete_reasons=incomplete_reasons,
                screening_indexed=screening_indexed,
            )
            if analysis.auto_ingest_eligible:
                document.state = DocumentState.INGESTING
                intake.enqueue_operation(session, document, OperationType.INGEST)
                self._succeed_operation(session, operation, phase=OperationPhase.COMPLETE)
                event_type = "analysis_clear_auto_ingest"
            else:
                document.state = DocumentState.REVIEW_REQUIRED
                self._succeed_operation(session, operation, phase=OperationPhase.AWAITING_DECISION)
                event_type = "analysis_requires_review"
            intake.audit(
                session,
                event_type=event_type,
                actor_type="system",
                actor_id=self.worker_id,
                document=document,
                operation=operation,
                details={
                    "status": document.state.value,
                    "analysis_revision": analysis.revision,
                    "candidate_count": analysis.candidate_count,
                    "semantic_complete": analysis.semantic_complete,
                    "classification_complete": analysis.classification_complete,
                    "pipeline_fingerprint": analysis.pipeline_fingerprint,
                },
            )
            session.commit()
        self.notify()

    def _queue_screening_revision_upsert(
        self,
        session: Session,
        document: Document,
        analysis: DocumentAnalysis,
        *,
        expected_points: int,
    ) -> None:
        """Queue a revision's screening write after removing prior points.

        Deterministic point IDs contain the analysis UUID, so a reanalysis
        cannot overwrite the prior revision in place. If this document has
        screening history, take ownership of every pending old mutation and
        put a verified whole-document delete immediately before the new
        upsert. The first revision writes directly and remains independent of
        Qdrant when parsing rejects the document before comparison.
        """

        entries = list(
            session.scalars(
                select(IndexOutboxEntry)
                .where(
                    IndexOutboxEntry.document_id == document.id,
                    IndexOutboxEntry.target == IndexTarget.SCREENING,
                )
                .order_by(IndexOutboxEntry.id)
            ).all()
        )
        current_write = next(
            (
                entry
                for entry in entries
                if entry.analysis_id == analysis.id
                and entry.action == IndexAction.UPSERT
                and entry.state in (OutboxState.PENDING, OutboxState.DONE)
            ),
            None,
        )
        if current_write is not None:
            return

        prior_entries = [entry for entry in entries if entry.analysis_id != analysis.id]
        if prior_entries:
            now = utc_now()
            for entry in prior_entries:
                if entry.state != OutboxState.PENDING:
                    continue
                entry.state = OutboxState.SUPERSEDED
                entry.last_error = "Superseded by a newer screening analysis revision."
                entry.completed_at = now
            analysis_steps.enqueue_index_entry(
                session,
                document=document,
                analysis_id=None,
                target=IndexTarget.SCREENING,
                action=IndexAction.DELETE,
                expected_points=0,
                collection_epoch=analysis.collection_epoch,
            )

        analysis_steps.enqueue_index_entry(
            session,
            document=document,
            analysis_id=analysis.id,
            target=IndexTarget.SCREENING,
            action=IndexAction.UPSERT,
            expected_points=expected_points,
        )

    def _discover(
        self,
        document_id: uuid.UUID,
        collection_key: str,
        epoch: int,
        *,
        chunks: list[Chunk],
        dense_vectors: list[list[float]],
        sparse_vectors: list[SparseVectorData],
    ) -> tuple[list[list[ChunkHit]], list[list[ChunkHit]]]:
        """Query active and screening indexes for every incoming chunk."""

        client = self.providers.qdrant
        assert client is not None and self.settings.embedding_dimension
        with self.session_factory() as session:
            current_epoch = intake.collection_epoch(session, collection_key)
            session.commit()
        if epoch != current_epoch:
            raise VectorIndexError(
                "refusing stale active discovery for "
                f"collection {collection_key!r}: analysis epoch {epoch}, "
                f"current epoch {current_epoch}"
            )
        active = ensure_active_collection(
            client,
            collection_key=collection_key,
            epoch=current_epoch,
            dimension=self.settings.embedding_dimension,
        )
        ensure_screening_collection(client, dimension=self.settings.embedding_dimension)

        dense_results: list[list[ChunkHit]] = []
        bm25_results: list[list[ChunkHit]] = []
        for _chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors, strict=True):
            active_dense = query_dense(
                client,
                active,
                vector=dense,
                top_k=DENSE_TOP_K,
                collection_key=collection_key,
            )
            screening_dense = query_dense(
                client,
                SCREENING_COLLECTION,
                vector=dense,
                top_k=DENSE_TOP_K,
                exclude_document_id=document_id,
                collection_key=collection_key,
            )
            # Keep each collection's complete top-k ranking. Raw dense scores
            # are not calibrated across physical collections, and local ranks
            # are the inputs to reciprocal rank fusion.
            dense_results.append([*active_dense, *screening_dense])
            active_bm25 = query_bm25(
                client,
                active,
                sparse=sparse,
                top_k=BM25_TOP_K,
                collection_key=collection_key,
            )
            screening_bm25 = query_bm25(
                client,
                SCREENING_COLLECTION,
                sparse=sparse,
                top_k=BM25_TOP_K,
                exclude_document_id=document_id,
                collection_key=collection_key,
            )
            bm25_results.append([*active_bm25, *screening_bm25])
        return dense_results, bm25_results

    def _classify_candidates(
        self,
        document_id: uuid.UUID,
        analysis_id: uuid.UUID,
        candidate_ids: list[uuid.UUID],
        *,
        layout: StorageLayout,
        incomplete_reasons: list[str],
        semantic_complete: bool,
    ) -> bool:
        """Classify the top candidates; returns classification completeness."""

        if not candidate_ids:
            # Nothing qualified for classification. The check is vacuously
            # complete only when discovery itself completed.
            return semantic_complete

        if self.providers.llm is None or self.providers.http_client is None:
            incomplete_reasons.append("classification-not-configured")
            return False

        findings_log: list[dict[str, object]] = []
        classification_complete = True
        try:
            for candidate_id in candidate_ids:
                with self.session_factory() as session:
                    from pdf_bridge.persistence.models import AnalysisCandidate

                    candidate = session.get(AnalysisCandidate, candidate_id)
                    analysis = session.get(DocumentAnalysis, analysis_id)
                    assert candidate is not None and analysis is not None
                    incoming, matched = analysis_steps.candidate_excerpts(
                        session, analysis, candidate
                    )
                    session.rollback()
                for role in ("classifier", "verifier"):
                    result = classify_candidate(
                        self.providers.llm,
                        role=role,  # type: ignore[arg-type]
                        incoming_excerpts=incoming,
                        candidate_excerpts=matched,
                        client=self.providers.http_client,
                    )
                    findings_log.append(
                        {
                            "candidate_id": str(candidate_id),
                            "role": role,
                            "model_id": result.model_id,
                            "valid": result.valid,
                            "error": result.error,
                            "prompt": result.prompt,
                            "raw_output": result.raw_output,
                            "raw_outputs": list(result.raw_outputs),
                        }
                    )
                    if not result.valid:
                        classification_complete = False
                        if "classification-invalid-output" not in incomplete_reasons:
                            incomplete_reasons.append("classification-invalid-output")
                    with self.session_factory() as session:
                        from pdf_bridge.persistence.models import AnalysisCandidate

                        candidate = session.get(AnalysisCandidate, candidate_id)
                        assert candidate is not None
                        analysis_steps.record_finding(session, candidate, result)
                        session.commit()
        except ClassificationUnavailableError as exc:
            incomplete_reasons.append(f"classification-unavailable: {exc}")
            return False
        finally:
            if findings_log:
                with self.session_factory() as session:
                    document = session.get(Document, document_id)
                    analysis = session.get(DocumentAnalysis, analysis_id)
                    assert document is not None and analysis is not None
                    if analysis.document_id != document.id:
                        raise ValueError("classification analysis belongs to another document")
                    analysis_steps.record_artifact(
                        session,
                        analysis,
                        artifact_store.write_artifact(
                            layout,
                            document_id,
                            analysis_id,
                            "findings",
                            {"analysis_id": str(analysis_id), "calls": findings_log},
                        ),
                    )
                    session.commit()
        return classification_complete

    def _reject_document(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        reason: str,
        detail: str,
    ) -> None:
        """Terminally reject an unusable PDF and queue content cleanup."""

        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            if document is None or operation is None:
                session.rollback()
                return
            self._succeed_operation(session, operation, phase=OperationPhase.CLEANING_UP)
            document.state = DocumentState.CLEANUP_PENDING
            document.cleanup_target = DocumentState.REJECTED
            document.rejection_reason = reason
            document.last_error = detail[:4000]
            intake.enqueue_operation(session, document, OperationType.CLEANUP)
            intake.audit(
                session,
                event_type="analysis_rejected",
                actor_type="system",
                actor_id=self.worker_id,
                document=document,
                operation=operation,
                details={"reason": reason, "detail": detail[:500]},
            )
            session.commit()
        self.notify()

    # -- ingestion and replacement -------------------------------------------------------

    def _run_ingest(self, operation_id: uuid.UUID, document_id: uuid.UUID) -> None:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            if document is None or operation is None:
                session.rollback()
                return
            if document.state not in (
                DocumentState.INGESTING,
                DocumentState.REPLACING,
                # Publication is verified before the final operation/workflow
                # checkpoint. Lease recovery must finish that checkpoint if a
                # process exits after the document becomes retrievable.
                DocumentState.INGESTED,
            ):
                operation.state = OperationState.CANCELLED
                operation.completed_at = utc_now()
                session.commit()
                return
            replacement_id = operation.replacement_id
            session.rollback()

        if replacement_id is not None:
            self._run_replacement(operation_id, document_id, replacement_id)
        else:
            self._run_plain_ingest(operation_id, document_id)

    def _ensure_vectors_ready(self, document_id: uuid.UUID) -> None:
        """Guarantee dense and sparse vectors exist in artifact storage."""

        layout = self._layout()
        with self.session_factory() as session:
            from pdf_bridge.persistence.models import DocumentArtifact

            document = session.get(Document, document_id)
            assert document is not None
            analysis = intake.latest_analysis(session, document_id)
            if analysis is None:
                raise VectorIndexError("the document has no analysis to publish")
            current_epoch = intake.collection_epoch(session, document.collection_key)
            if analysis.collection_epoch != current_epoch:
                raise VectorIndexError(
                    "the document analysis is stale for the current collection epoch"
                )
            if analysis.pipeline_fingerprint != self._fingerprint():
                raise VectorIndexError(
                    "the document analysis is stale for the current pipeline fingerprint"
                )
            analysis_id = analysis.id
            analysis_epoch = analysis.collection_epoch
            existing = session.scalar(
                select(DocumentArtifact).where(
                    DocumentArtifact.analysis_id == analysis_id,
                    DocumentArtifact.kind == "vectors",
                )
            )
            chunk_rows = session.scalars(
                select(AnalysisChunk)
                .where(AnalysisChunk.analysis_id == analysis_id)
                .order_by(AnalysisChunk.chunk_index)
            ).all()
            chunk_texts = [row.text for row in chunk_rows]
            session.rollback()
        if existing is not None:
            self._load_points(
                document_id,
                analysis_id,
                collection_epoch=analysis_epoch,
            )
            return
        if not self.providers.semantic_ready():
            raise ProviderUnavailableError(
                "embedding and Qdrant providers are required to publish this document"
            )
        dense_vectors = embed_texts(
            self.providers.embedding,  # type: ignore[arg-type]
            chunk_texts,
            client=self.providers.http_client,  # type: ignore[arg-type]
        )
        sparse_vectors = [bm25_document_vector(text) for text in chunk_texts]
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            analysis = session.get(DocumentAnalysis, analysis_id)
            assert document is not None and analysis is not None
            if analysis.document_id != document.id:
                raise ValueError("vector analysis belongs to another document")
            analysis_steps.record_artifact(
                session,
                analysis,
                artifact_store.write_artifact(
                    layout,
                    document_id,
                    analysis_id,
                    "vectors",
                    {
                        "analysis_id": str(analysis_id),
                        "pipeline_fingerprint": analysis.pipeline_fingerprint,
                        "embedding_model_id": self.settings.embedding_model_id,
                        "dimension": self.settings.embedding_dimension,
                        "dense": [list(vector) for vector in dense_vectors],
                        "sparse": [
                            {
                                "indices": list(vector.indices),
                                "values": list(vector.values),
                            }
                            for vector in sparse_vectors
                        ],
                    },
                ),
            )
            session.commit()
        self._load_points(
            document_id,
            analysis_id,
            collection_epoch=analysis_epoch,
        )

    def _prepare_publication(self, document_id: uuid.UUID) -> None:
        """Prepare complete, non-retrievable active points idempotently."""

        self._ensure_vectors_ready(document_id)
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            assert document is not None
            analysis = intake.latest_analysis(session, document_id)
            assert analysis is not None
            existing = {
                (entry.target, entry.action)
                for entry in session.scalars(
                    select(IndexOutboxEntry).where(
                        IndexOutboxEntry.document_id == document_id,
                        IndexOutboxEntry.analysis_id == analysis.id,
                    )
                ).all()
            }
            if (IndexTarget.ACTIVE, IndexAction.UPSERT) not in existing:
                analysis_steps.enqueue_index_entry(
                    session,
                    document=document,
                    analysis_id=analysis.id,
                    target=IndexTarget.ACTIVE,
                    action=IndexAction.UPSERT,
                    expected_points=analysis.chunk_count,
                )
            if (IndexTarget.SCREENING, IndexAction.DELETE) not in existing:
                analysis_steps.enqueue_index_entry(
                    session,
                    document=document,
                    analysis_id=analysis.id,
                    target=IndexTarget.SCREENING,
                    action=IndexAction.DELETE,
                    expected_points=0,
                    collection_epoch=analysis.collection_epoch,
                )
            session.commit()
        self._drain_outbox(document_id)

    def _queue_publication(self, document_id: uuid.UUID) -> None:
        """Durably queue the visibility flip while intake remains open."""

        with self.session_factory() as session:
            document = session.get(Document, document_id)
            assert document is not None
            analysis = intake.latest_analysis(session, document_id)
            assert analysis is not None
            existing = session.scalar(
                select(IndexOutboxEntry.id).where(
                    IndexOutboxEntry.document_id == document_id,
                    IndexOutboxEntry.analysis_id == analysis.id,
                    IndexOutboxEntry.target == IndexTarget.ACTIVE,
                    IndexOutboxEntry.action == IndexAction.PUBLISH,
                )
            )
            if existing is None:
                analysis_steps.enqueue_index_entry(
                    session,
                    document=document,
                    analysis_id=analysis.id,
                    target=IndexTarget.ACTIVE,
                    action=IndexAction.PUBLISH,
                    expected_points=analysis.chunk_count,
                )
            session.commit()

    def _complete_publication(self, document_id: uuid.UUID) -> None:
        """Close intake only after the current epoch is visibly published."""

        with self.session_factory() as session:
            document = session.get(Document, document_id)
            assert document is not None
            analysis = intake.latest_analysis(session, document_id)
            if analysis is None:
                raise VectorIndexError("the document has no analysis to publish")
            publication = session.scalar(
                select(IndexOutboxEntry.id).where(
                    IndexOutboxEntry.document_id == document_id,
                    IndexOutboxEntry.analysis_id == analysis.id,
                    IndexOutboxEntry.target == IndexTarget.ACTIVE,
                    IndexOutboxEntry.action == IndexAction.PUBLISH,
                    IndexOutboxEntry.collection_epoch == analysis.collection_epoch,
                    IndexOutboxEntry.state == OutboxState.DONE,
                )
            )
            if publication is None:
                raise VectorIndexError(
                    "active publication was not durably verified for this analysis"
                )
            current_epoch = intake.collection_epoch(session, document.collection_key)
            if analysis.collection_epoch != current_epoch:
                raise VectorIndexError(
                    "refusing to complete publication from stale collection epoch "
                    f"{analysis.collection_epoch}; current epoch is {current_epoch}"
                )
            document.state = DocumentState.INGESTED
            document.ingested_at = document.ingested_at or utc_now()
            document.last_error = None
            session.commit()

    def _publish_prepared_document(self, document_id: uuid.UUID) -> None:
        """Prepare and expose active points before closing the intake row."""

        self._prepare_publication(document_id)
        self._queue_publication(document_id)
        self._drain_outbox(document_id)
        self._complete_publication(document_id)

    def _run_plain_ingest(self, operation_id: uuid.UUID, document_id: uuid.UUID) -> None:
        self._set_phase(operation_id, OperationPhase.INGESTING)
        try:
            self._publish_prepared_document(document_id)
        except (
            ProviderUnavailableError,
            EmbeddingError,
            VectorIndexUnavailableError,
        ) as exc:
            self._requeue_or_fail_ingest(
                operation_id,
                document_id,
                error=str(exc),
                failed_state=DocumentState.INGEST_FAILED,
            )
            return
        except VectorIndexError as exc:
            self._fail_operation(
                operation_id,
                document_id,
                error=str(exc),
                document_state=DocumentState.INGEST_FAILED,
            )
            return

        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            assert document is not None and operation is not None
            self._succeed_operation(session, operation, phase=OperationPhase.COMPLETE)
            intake.audit(
                session,
                event_type="ingestion_succeeded",
                actor_type="system",
                actor_id=self.worker_id,
                document=document,
                operation=operation,
                details={
                    "status": document.state.value,
                    "collection_key": document.collection_key,
                    "chunk_count": document.chunk_count,
                },
            )
            session.commit()

    def _run_replacement(
        self,
        operation_id: uuid.UUID,
        new_document_id: uuid.UUID,
        replacement_id: uuid.UUID,
    ) -> None:
        """Execute the strict replacement ordering, resumable at each stage.

        No new active point is written before the old document's points are
        verifiably gone; an old-delete failure blocks new ingestion; a
        new-ingest failure leaves the old document deleted and the workflow
        retryable.
        """

        with self.session_factory() as session:
            workflow = session.get(ReplacementWorkflow, replacement_id)
            assert workflow is not None
            state = workflow.state
            old_document_id = workflow.old_document_id
            session.rollback()

        if state == ReplacementState.PREPARING:
            try:
                self._ensure_vectors_ready(new_document_id)
            except (ProviderUnavailableError, EmbeddingError) as exc:
                self._requeue_or_fail_ingest(
                    operation_id,
                    new_document_id,
                    error=str(exc),
                    failed_state=DocumentState.REPLACE_FAILED,
                )
                return
            except VectorIndexError as exc:
                self._fail_operation(
                    operation_id,
                    new_document_id,
                    error=str(exc),
                    document_state=DocumentState.REPLACE_FAILED,
                )
                return
            with self.session_factory() as session:
                workflow = session.get(ReplacementWorkflow, replacement_id)
                assert workflow is not None
                workflow.state = ReplacementState.DELETING_OLD
                session.commit()
            state = ReplacementState.DELETING_OLD

        if state == ReplacementState.DELETING_OLD:
            self._set_phase(operation_id, OperationPhase.DELETING_EXISTING)
            with self.session_factory() as session:
                old_document = session.get(Document, old_document_id)
                assert old_document is not None
                if old_document.state in (DocumentState.INGESTED, DocumentState.DELETE_FAILED):
                    old_document.state = DocumentState.DELETING
                    self._queue_verified_index_deletions(
                        session,
                        old_document,
                        required_targets=(IndexTarget.ACTIVE,),
                    )
                session.commit()
            try:
                self._drain_outbox(old_document_id)
            except VectorIndexError as exc:
                # Old-delete failure blocks new ingestion entirely.
                with self.session_factory() as session:
                    workflow = session.get(ReplacementWorkflow, replacement_id)
                    old_document = session.get(Document, old_document_id)
                    assert workflow is not None and old_document is not None
                    workflow.error = str(exc)[:2000]
                    old_document.last_error = str(exc)[:4000]
                    session.commit()
                self._requeue_or_fail_ingest(
                    operation_id,
                    new_document_id,
                    error=f"old document removal failed: {exc}",
                    failed_state=DocumentState.REPLACE_FAILED,
                )
                return
            with self.session_factory() as session:
                workflow = session.get(ReplacementWorkflow, replacement_id)
                old_document = session.get(Document, old_document_id)
                assert workflow is not None and old_document is not None
                self._purge_document_content(session, old_document, tombstone=DocumentState.DELETED)
                old_document.replaced_by_document_id = new_document_id
                workflow.state = ReplacementState.INGESTING_NEW
                intake.audit(
                    session,
                    event_type="replacement_old_deleted",
                    actor_type="system",
                    actor_id=self.worker_id,
                    document=old_document,
                    details={
                        "replacement_id": str(replacement_id),
                        "new_document_id": str(new_document_id),
                    },
                )
                session.commit()
            state = ReplacementState.INGESTING_NEW

        if state == ReplacementState.INGESTING_NEW:
            self._set_phase(operation_id, OperationPhase.INGESTING)
            try:
                self._publish_prepared_document(new_document_id)
            except (
                ProviderUnavailableError,
                EmbeddingError,
                VectorIndexUnavailableError,
            ) as exc:
                self._requeue_or_fail_ingest(
                    operation_id,
                    new_document_id,
                    error=f"new document ingestion failed after old deletion: {exc}",
                    failed_state=DocumentState.REPLACE_FAILED,
                )
                return
            except VectorIndexError as exc:
                self._fail_operation(
                    operation_id,
                    new_document_id,
                    error=str(exc),
                    document_state=DocumentState.REPLACE_FAILED,
                )
                return

            with self.session_factory() as session:
                document = session.get(Document, new_document_id)
                operation = session.get(WorkOperation, operation_id)
                workflow = session.get(ReplacementWorkflow, replacement_id)
                assert document is not None and operation is not None
                assert workflow is not None
                workflow.state = ReplacementState.SUCCEEDED
                workflow.completed_at = utc_now()
                workflow.error = None
                self._succeed_operation(session, operation, phase=OperationPhase.COMPLETE)
                intake.audit(
                    session,
                    event_type="replacement_succeeded",
                    actor_type="system",
                    actor_id=self.worker_id,
                    document=document,
                    operation=operation,
                    details={
                        "replacement_id": str(replacement_id),
                        "old_document_id": str(old_document_id),
                        "collection_key": document.collection_key,
                    },
                )
                session.commit()

    # -- deletion and cleanup ---------------------------------------------------------------

    def _queue_verified_index_deletions(
        self,
        session: Session,
        document: Document,
        *,
        required_targets: tuple[IndexTarget, ...] = (),
    ) -> bool:
        """Take ownership and queue verified deletes for every touched index.

        Active collections are epoch-versioned. Historical outbox snapshots
        are the authoritative inventory of physical epochs this document may
        have touched; required active removal additionally checks the current
        epoch for legacy/imported documents without outbox history. Screening
        is one physical collection and therefore needs only one delete.
        """

        entries = list(
            session.scalars(
                select(IndexOutboxEntry)
                .where(IndexOutboxEntry.document_id == document.id)
                .order_by(IndexOutboxEntry.id)
            ).all()
        )
        active_epochs = {
            entry.collection_epoch for entry in entries if entry.target == IndexTarget.ACTIVE
        }
        screening_touched = any(entry.target == IndexTarget.SCREENING for entry in entries)
        if IndexTarget.ACTIVE in required_targets:
            active_epochs.add(intake.collection_epoch(session, document.collection_key))
        if IndexTarget.SCREENING in required_targets:
            screening_touched = True
        if not active_epochs and not screening_touched:
            return False

        now = utc_now()
        for entry in entries:
            if entry.state != OutboxState.PENDING or entry.action == IndexAction.DELETE:
                continue
            entry.state = OutboxState.SUPERSEDED
            entry.last_error = "Superseded by verified document removal."
            entry.completed_at = now

        for epoch in sorted(active_epochs):
            pending_delete = session.scalar(
                select(IndexOutboxEntry.id)
                .where(
                    IndexOutboxEntry.document_id == document.id,
                    IndexOutboxEntry.target == IndexTarget.ACTIVE,
                    IndexOutboxEntry.collection_epoch == epoch,
                    IndexOutboxEntry.action == IndexAction.DELETE,
                    IndexOutboxEntry.state == OutboxState.PENDING,
                )
                .limit(1)
            )
            if pending_delete is None:
                analysis_steps.enqueue_index_entry(
                    session,
                    document=document,
                    analysis_id=None,
                    target=IndexTarget.ACTIVE,
                    action=IndexAction.DELETE,
                    expected_points=0,
                    collection_epoch=epoch,
                )
        if screening_touched:
            pending_screening_delete = session.scalar(
                select(IndexOutboxEntry.id)
                .where(
                    IndexOutboxEntry.document_id == document.id,
                    IndexOutboxEntry.target == IndexTarget.SCREENING,
                    IndexOutboxEntry.action == IndexAction.DELETE,
                    IndexOutboxEntry.state == OutboxState.PENDING,
                )
                .limit(1)
            )
            if pending_screening_delete is None:
                analysis_steps.enqueue_index_entry(
                    session,
                    document=document,
                    analysis_id=None,
                    target=IndexTarget.SCREENING,
                    action=IndexAction.DELETE,
                    expected_points=0,
                    collection_epoch=intake.collection_epoch(session, document.collection_key),
                )
        return True

    def _run_delete(self, operation_id: uuid.UUID, document_id: uuid.UUID) -> None:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            if document is None or operation is None:
                session.rollback()
                return
            if document.state != DocumentState.DELETING:
                operation.state = OperationState.CANCELLED
                operation.completed_at = utc_now()
                session.commit()
                return
            operation.phase = OperationPhase.CLEANING_UP
            self._queue_verified_index_deletions(
                session,
                document,
                required_targets=(IndexTarget.ACTIVE,),
            )
            session.commit()
        try:
            self._drain_outbox(document_id)
        except VectorIndexError as exc:
            self._fail_operation(
                operation_id,
                document_id,
                error=str(exc),
                document_state=DocumentState.DELETE_FAILED,
            )
            return
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            assert document is not None and operation is not None
            self._purge_document_content(session, document, tombstone=DocumentState.DELETED)
            self._succeed_operation(session, operation, phase=OperationPhase.COMPLETE)
            intake.audit(
                session,
                event_type="deletion_succeeded",
                actor_type="system",
                actor_id=self.worker_id,
                document=document,
                operation=operation,
                details={"status": document.state.value},
            )
            session.commit()

    def _run_cleanup(self, operation_id: uuid.UUID, document_id: uuid.UUID) -> None:
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            if document is None or operation is None:
                session.rollback()
                return
            if document.state not in (
                DocumentState.CLEANUP_PENDING,
                DocumentState.CLEANUP_FAILED,
            ):
                operation.state = OperationState.CANCELLED
                operation.completed_at = utc_now()
                session.commit()
                return
            operation.phase = OperationPhase.CLEANING_UP
            tombstone = document.cleanup_target or DocumentState.CANCELLED
            # Parser rejection does not depend on Qdrant when the document
            # never produced index work. Otherwise, supersede every pending
            # write/publication before draining verified deletes only.
            has_index_work = self._queue_verified_index_deletions(session, document)
            session.commit()
        if has_index_work:
            try:
                self._drain_outbox(document_id)
            except VectorIndexError as exc:
                self._fail_operation(
                    operation_id,
                    document_id,
                    error=str(exc),
                    document_state=DocumentState.CLEANUP_FAILED,
                )
                return
        with self.session_factory() as session:
            document = session.get(Document, document_id)
            operation = session.get(WorkOperation, operation_id)
            assert document is not None and operation is not None
            self._purge_document_content(session, document, tombstone=tombstone)
            self._succeed_operation(session, operation, phase=OperationPhase.COMPLETE)
            intake.audit(
                session,
                event_type=(
                    "upload_rejected"
                    if tombstone == DocumentState.REJECTED
                    else "upload_cancellation_complete"
                ),
                actor_type="system",
                actor_id=self.worker_id,
                document=document,
                operation=operation,
                details={"status": document.state.value},
            )
            session.commit()

    def _purge_document_content(
        self, session: Session, document: Document, *, tombstone: DocumentState
    ) -> None:
        """Purge content, artifacts, and analyses; keep audit hashes only."""

        layout = self._layout()
        analyses = list(document.analyses)
        analysis_history = [
            {
                "analysis_id": str(analysis.id),
                "revision": analysis.revision,
                "status": analysis.status.value,
                "collection_epoch": analysis.collection_epoch,
                "pipeline_fingerprint": analysis.pipeline_fingerprint,
                "text_sha256": analysis.text_sha256,
                "created_at": analysis.created_at.isoformat(),
                "completed_at": (
                    analysis.completed_at.isoformat() if analysis.completed_at else None
                ),
                "artifacts": [
                    {
                        "kind": artifact.kind,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                    }
                    for artifact in analysis.artifacts
                ],
            }
            for analysis in analyses
        ]
        artifact_count = sum(len(analysis.artifacts) for analysis in analyses)
        artifact_kinds = sorted(
            {artifact.kind for analysis in analyses for artifact in analysis.artifacts}
        )
        latest_decision = document.decisions[-1] if document.decisions else None
        manifest_hash = artifact_store.analysis_manifest_hash(
            document_id=document.id,
            content_sha256=document.sha256,
            text_hash=document.text_sha256,
            analysis_history=analysis_history,
            decision_action=latest_decision.action.value if latest_decision else None,
            decision_actor=latest_decision.actor_id if latest_decision else None,
            decision_target=(
                str(latest_decision.target_document_id)
                if latest_decision and latest_decision.target_document_id
                else None
            ),
            uploaded_at=document.uploaded_at.isoformat(),
            decided_at=(latest_decision.created_at.isoformat() if latest_decision else None),
        )
        artifact_store.purge_document_artifacts(layout, document.id)
        for analysis in analyses:
            session.delete(analysis)
        if document.storage_key:
            remove_storage_key(layout, document.storage_key, missing_ok=True)
            document.storage_key = None
        document.analysis_manifest_hash = manifest_hash
        document.state = tombstone
        now = utc_now()
        if tombstone == DocumentState.DELETED:
            document.deleted_at = now
        elif tombstone == DocumentState.CANCELLED:
            document.cancelled_at = now
        else:
            document.rejected_at = now
        intake.audit(
            session,
            event_type="analysis_data_purged",
            actor_type="system",
            actor_id=self.worker_id,
            document=document,
            details={
                "manifest_hash": manifest_hash,
                "status": tombstone.value,
                "analysis_count": len(analyses),
                "artifact_count": artifact_count,
                "artifact_kinds": artifact_kinds,
            },
        )
        session.flush()

    # -- outbox -------------------------------------------------------------------------------

    def _drain_outbox(self, document_id: uuid.UUID) -> None:
        """Apply pending index mutations for one document in strict order."""

        client = self.providers.qdrant
        while True:
            with self.session_factory() as session:
                entry = session.scalar(
                    select(IndexOutboxEntry)
                    .where(
                        IndexOutboxEntry.state == OutboxState.PENDING,
                        IndexOutboxEntry.document_id == document_id,
                    )
                    .order_by(IndexOutboxEntry.id)
                    .limit(1)
                )
                if entry is None:
                    session.rollback()
                    return
                entry_id = entry.id
                collection_key = entry.collection_key
                collection_epoch = entry.collection_epoch
                target = entry.target
                action = entry.action
                expected = entry.expected_points
                analysis_id = entry.analysis_id
                session.rollback()

            if client is None or not self.settings.embedding_dimension:
                self._record_outbox_error(entry_id, "qdrant is not configured")
                raise VectorIndexUnavailableError(
                    "Qdrant is not configured; index mutations cannot be applied"
                )
            try:
                if target == IndexTarget.SCREENING:
                    ensure_screening_collection(client, dimension=self.settings.embedding_dimension)
                    collection = SCREENING_COLLECTION
                elif action == IndexAction.DELETE:
                    # Removal is pinned to a historical physical epoch and
                    # must never move the stable collection alias.
                    collection = physical_collection_name(collection_key, collection_epoch)
                else:
                    with self.session_factory() as session:
                        current_epoch = intake.collection_epoch(session, collection_key)
                        session.commit()
                    if collection_epoch != current_epoch:
                        raise VectorIndexError(
                            "refusing stale active index mutation for "
                            f"collection {collection_key!r}: outbox epoch "
                            f"{collection_epoch}, current epoch {current_epoch}"
                        )
                    collection = ensure_active_collection(
                        client,
                        collection_key=collection_key,
                        epoch=current_epoch,
                        dimension=self.settings.embedding_dimension,
                    )
                if action == IndexAction.DELETE:
                    if target == IndexTarget.ACTIVE:
                        delete_document_points_if_collection_exists(client, collection, document_id)
                    else:
                        delete_document_points(client, collection, document_id)
                elif action == IndexAction.PUBLISH:
                    if target != IndexTarget.ACTIVE or expected is None:
                        raise VectorIndexError(
                            "publication outbox entries require an active target "
                            "and an expected point count"
                        )
                    publish_document_points(
                        client,
                        collection,
                        document_id,
                        expected=expected,
                    )
                else:
                    if analysis_id is None:
                        raise VectorIndexError("index upsert requires an analysis id")
                    points = self._load_points(
                        document_id,
                        analysis_id,
                        collection_epoch=collection_epoch,
                    )
                    upsert_chunk_points(
                        client,
                        collection,
                        points,
                        published=False,
                        screening=target == IndexTarget.SCREENING,
                    )
                    verify_document_point_count(
                        client,
                        collection,
                        document_id,
                        expected=expected if expected is not None else len(points),
                    )
            except VectorIndexError as exc:
                self._record_outbox_error(entry_id, str(exc))
                raise

            with self.session_factory() as session:
                entry = session.get(IndexOutboxEntry, entry_id)
                if entry is not None:
                    entry.state = OutboxState.DONE
                    entry.attempts += 1
                    entry.last_error = None
                    entry.completed_at = utc_now()
                    session.commit()
                else:
                    session.rollback()

    def _record_outbox_error(self, entry_id: int, error: str) -> None:
        with self.session_factory() as session:
            entry = session.get(IndexOutboxEntry, entry_id)
            if entry is not None:
                entry.attempts += 1
                entry.last_error = error[:2000]
                session.commit()
            else:
                session.rollback()

    def _load_points(
        self,
        document_id: uuid.UUID,
        analysis_id: uuid.UUID,
        *,
        collection_epoch: int,
    ) -> list[ChunkPoint]:
        """Rebuild index points from retained chunks and the vectors artifact."""

        layout = self._layout()
        with self.session_factory() as session:
            from pdf_bridge.persistence.models import DocumentArtifact

            document = session.get(Document, document_id)
            assert document is not None
            analysis = session.get(DocumentAnalysis, analysis_id)
            if analysis is None:
                raise VectorIndexError("the outbox entry references a missing analysis")
            if analysis.document_id != document_id:
                raise VectorIndexError("the outbox analysis belongs to another document")
            if analysis.collection_epoch != collection_epoch:
                raise VectorIndexError("the outbox epoch does not match its analysis revision")
            vectors_row = session.scalar(
                select(DocumentArtifact).where(
                    DocumentArtifact.analysis_id == analysis.id,
                    DocumentArtifact.kind == "vectors",
                )
            )
            if vectors_row is None:
                raise VectorIndexError("the vectors artifact is missing for this document")
            chunk_rows = session.scalars(
                select(AnalysisChunk)
                .where(AnalysisChunk.analysis_id == analysis.id)
                .order_by(AnalysisChunk.chunk_index)
            ).all()
            chunks = [
                {
                    "chunk_index": row.chunk_index,
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "text_hash": row.text_hash,
                    "text": row.text,
                }
                for row in chunk_rows
            ]
            collection_key = document.collection_key
            vectors_key = vectors_row.storage_key
            vectors_sha256 = vectors_row.sha256
            vectors_size_bytes = vectors_row.size_bytes
            resolved_analysis_id = analysis.id
            resolved_pipeline_fingerprint = analysis.pipeline_fingerprint
            session.rollback()

        try:
            payload = artifact_store.read_artifact(
                layout,
                vectors_key,
                expected_sha256=vectors_sha256,
                expected_size_bytes=vectors_size_bytes,
            )
        except (OSError, ValueError) as exc:
            raise VectorIndexError(f"vectors artifact failed integrity checks: {exc}") from exc
        dense = payload.get("dense")
        sparse = payload.get("sparse")
        if (
            payload.get("analysis_id") != str(resolved_analysis_id)
            or payload.get("pipeline_fingerprint") != resolved_pipeline_fingerprint
            or payload.get("embedding_model_id") != self.settings.embedding_model_id
            or payload.get("dimension") != self.settings.embedding_dimension
            or not isinstance(dense, list)
            or not isinstance(sparse, list)
            or len(dense) != len(chunks)
            or len(sparse) != len(chunks)
        ):
            raise VectorIndexError("the vectors artifact does not match retained chunks")
        points: list[ChunkPoint] = []
        for chunk, dense_vector, sparse_vector in zip(chunks, dense, sparse, strict=True):
            points.append(
                ChunkPoint(
                    document_id=document_id,
                    analysis_id=resolved_analysis_id,
                    chunk_index=chunk["chunk_index"],
                    collection_key=collection_key,
                    page_start=chunk["page_start"],
                    page_end=chunk["page_end"],
                    text_hash=chunk["text_hash"],
                    text=chunk["text"],
                    dense=tuple(float(component) for component in dense_vector),
                    sparse=SparseVectorData(
                        indices=tuple(int(index) for index in sparse_vector["indices"]),
                        values=tuple(float(value) for value in sparse_vector["values"]),
                    ),
                )
            )
        return points
