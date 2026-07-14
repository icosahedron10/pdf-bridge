"""Target-only durable worker for preparation, publication, and verified deletion.

The worker owns two in-process execution slots.  Catalog transactions are
short and contain only durable state transitions; parsing, model calls,
Qdrant calls, and filesystem removal all happen after the transaction closes.
Every mutation is restart-safe through an operation phase, an exact revision
binding, publication proof, deletion progress, or an exact-target outbox row.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
from qdrant_client import QdrantClient
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.core.config import CollectionDefinition, Settings
from pdf_bridge.persistence.models import (
    CandidateEvidence,
    CandidateSource,
    DeletionPhase,
    Document,
    DocumentState,
    EvidenceKind,
    ExtractedPage,
    FormatterBatch,
    IndexAction,
    IndexOutboxEntry,
    IndexTarget,
    OperationPhase,
    OperationState,
    OperationType,
    OutboxState,
    PreparedCandidate,
    PreparedChunk,
    PreparedChunkVector,
    PreparedPage,
    PreparedRevision,
    PublicationRecord,
    PublicationStatus,
    RevisionArtifact,
    RevisionStatus,
    TerminalDisposition,
    WorkOperation,
    utc_now,
)
from pdf_bridge.services import intake, preparation
from pdf_bridge.services.candidates import (
    BM25_STRONG_MIN_CHUNKS,
    BM25_STRONG_PLACEMENT_RANK,
    BM25_TOP_K,
    CANDIDATES_VERSION,
    COSINE_MULTI_MIN_CHUNKS,
    COSINE_MULTI_THRESHOLD,
    COSINE_STRONG_THRESHOLD,
    DENSE_TOP_K,
    MAX_CLASSIFIED_CANDIDATES,
    RRF_K,
    ChunkHit,
    evaluate_candidates,
)
from pdf_bridge.services.candidates import (
    CandidateEvidence as DeterministicCandidate,
)
from pdf_bridge.services.classification import (
    CLASSIFICATION_VERSION,
    ClassificationUnavailableError,
    FindingResult,
    LlmConfig,
    SourceExcerpt,
    classify_candidate,
)
from pdf_bridge.services.extraction import (
    EXTRACTION_PROFILE,
    LANGUAGE_PROFILE,
    EnglishDetector,
    ExtractionLimits,
    LinguaEnglishDetector,
)
from pdf_bridge.services.filenames import (
    FILENAME_ANALYSIS_VERSION,
    JARO_WINKLER_THRESHOLD,
    MIN_FAMILY_SUBSTANTIVE_TOKENS,
    TOKEN_SET_SIMILARITY_THRESHOLD,
    compare_filenames,
    profile_filename,
)
from pdf_bridge.services.local_embeddings import (
    DENSE_DIMENSION,
    LocalEmbeddingModels,
    LocalModelConfig,
    LocalModelError,
    SparseVector,
)
from pdf_bridge.services.markdown_chunking import CHUNKER_PROFILE, HARD_MAX_TOKENS
from pdf_bridge.services.markdown_formatter import (
    MARKDOWN_FORMATTER_VERSION,
    FormatterConfig,
)
from pdf_bridge.services.profiles import build_pipeline_profiles
from pdf_bridge.services.storage import (
    StorageError,
    StorageLayout,
    remove_storage_key,
    resolve_storage_key,
)
from pdf_bridge.services.vector_index import (
    INDEX_SCHEMA_VERSION,
    QdrantPointClient,
    VectorIndexConsistencyError,
    VectorIndexError,
    activate_prepared_points,
    delete_document_points,
    query_candidates,
    stage_active_points,
    upsert_screening_points,
    validate_fixed_collections,
    verify_document_zero,
    verify_prepared_points,
)

logger = logging.getLogger(__name__)

WORKER_SLOTS = 2
_FAILURE_MESSAGE_LIMIT = 500
_MAX_PROTECTED_ARTIFACT_BYTES = 8 * 1024 * 1024
_TERMINAL_OPERATION_PHASES = {
    OperationPhase.COMPLETE,
    OperationPhase.AWAITING_DECISION,
}
_DELETION_OPERATION_PHASES = {OperationPhase(phase.value) for phase in DeletionPhase}


class LocalModelsProvider(Protocol):
    """Local embedding surface needed by preparation, queries, and readiness."""

    def count_tokens(self, text: str) -> int: ...

    def embed_dense(self, texts: list[str]) -> list[tuple[float, ...]]: ...

    def embed_sparse_documents(self, texts: list[str]) -> list[SparseVector]: ...

    def embed_sparse_queries(self, texts: list[str]) -> list[SparseVector]: ...

    def validate_ready(self) -> None: ...


@dataclass(slots=True)
class WorkerProviders:
    """Explicit target provider bundle; missing required providers fail work."""

    qdrant: QdrantPointClient | None = None
    local_models: LocalModelsProvider | None = None
    language_detector: EnglishDetector | None = None
    formatter: FormatterConfig | None = None
    advisory: LlmConfig | None = None
    http_client: httpx.Client | Any | None = None


def _secret(value: object | None) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if callable(getter) else str(value)


def providers_from_settings(
    settings: Settings, *, http_client: httpx.Client | None = None
) -> WorkerProviders:
    """Build target providers from a fully validated Settings object."""

    qdrant: QdrantPointClient | None = None
    if settings.qdrant_url is not None:
        qdrant = QdrantClient(
            url=settings.qdrant_url,
            api_key=_secret(settings.qdrant_api_key),
            timeout=settings.qdrant_timeout_seconds,
        )

    local_models: LocalModelsProvider | None = None
    language_detector: EnglishDetector | None = None
    if (
        settings.dense_model_revision is not None
        and settings.sparse_model_revision is not None
        and settings.model_cache_dir is not None
    ):
        local_models = LocalEmbeddingModels(
            LocalModelConfig(
                dense_model_id=settings.dense_model_id,
                dense_model_revision=settings.dense_model_revision,
                sparse_model_id=settings.sparse_model_id,
                sparse_model_revision=settings.sparse_model_revision,
                cache_dir=settings.model_cache_dir,
                local_files_only=settings.model_local_files_only,
                device=settings.dense_device,
                dense_batch_size=settings.dense_batch_size,
            )
        )
        language_detector = LinguaEnglishDetector()

    formatter: FormatterConfig | None = None
    if settings.formatter_api_url is not None and settings.formatter_model_id is not None:
        formatter = FormatterConfig(
            api_url=settings.formatter_api_url,
            model_id=settings.formatter_model_id,
            expected_tokenizer_class=settings.formatter_tokenizer_class or "",
            prompt_revision=settings.formatter_prompt_revision or "",
            schema_revision=settings.formatter_schema_revision or "",
            api_token=_secret(settings.formatter_api_token),
            timeout_seconds=settings.formatter_timeout_seconds,
            max_input_tokens=settings.formatter_max_input_tokens,
            max_output_tokens=settings.formatter_max_output_tokens,
            token_safety_reserve=settings.formatter_token_safety_reserve,
            max_pages_per_request=settings.formatter_max_pages_per_request,
            max_attempts=settings.formatter_max_attempts,
        )

    advisory: LlmConfig | None = None
    if (
        settings.llm_api_url is not None
        and settings.llm_classifier_model is not None
        and settings.llm_verifier_model is not None
    ):
        advisory = LlmConfig(
            api_url=settings.llm_api_url,
            classifier_model=settings.llm_classifier_model,
            classifier_model_revision=settings.llm_classifier_model_revision or "",
            classifier_prompt_revision=settings.llm_classifier_prompt_revision or "",
            verifier_model=settings.llm_verifier_model,
            verifier_model_revision=settings.llm_verifier_model_revision or "",
            verifier_prompt_revision=settings.llm_verifier_prompt_revision or "",
            max_input_tokens=settings.llm_max_input_tokens,
            max_output_tokens=settings.llm_max_output_tokens,
            max_attempts=settings.llm_max_attempts,
            api_token=_secret(settings.llm_api_token),
            timeout=settings.llm_timeout_seconds,
        )

    return WorkerProviders(
        qdrant=qdrant,
        local_models=local_models,
        language_detector=language_detector,
        formatter=formatter,
        advisory=advisory,
        http_client=http_client,
    )


class WorkerExecutionError(RuntimeError):
    """Content-safe worker failure with explicit retry semantics."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code[:100]
        self.message = " ".join(message.split())[:_FAILURE_MESSAGE_LIMIT]
        self.retryable = retryable


class LeaseLostError(WorkerExecutionError):
    def __init__(self) -> None:
        super().__init__(
            "operation_lease_lost",
            "The operation lease is no longer owned by this worker.",
            retryable=True,
        )


@dataclass(frozen=True, slots=True)
class ClaimedOperation:
    operation_id: uuid.UUID
    document_id: uuid.UUID
    collection_key: str
    operation_type: OperationType
    locked_document_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    operation_id: uuid.UUID
    document_id: uuid.UUID
    prepared_revision_id: uuid.UUID | None
    replacement_target_document_id: uuid.UUID | None
    operation_type: OperationType
    phase: OperationPhase


@dataclass(frozen=True, slots=True)
class DocumentWorkSnapshot:
    document_id: uuid.UUID
    collection_key: str
    storage_key: str
    source_path: Path


@dataclass(frozen=True, slots=True)
class CandidateDiscovery:
    candidate: DeterministicCandidate
    prepared_revision_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class CitationSource:
    document_id: uuid.UUID
    chunk_id: uuid.UUID
    page_start: int
    page_end: int
    text: str

    def public_citation(self, excerpt: str) -> Mapping[str, Any]:
        return {
            "document_id": str(self.document_id),
            "chunk_id": str(self.chunk_id),
            "page_start": self.page_start,
            "page_end": self.page_end,
            "excerpt": excerpt,
        }


@dataclass(frozen=True, slots=True)
class ProtectedExchange:
    """One bounded formatter request/response captured for protected storage."""

    request: Mapping[str, Any]
    response: object | None
    failure: str | None


class _CapturedResponse:
    def __init__(self, response: Any, capture: Any) -> None:
        self._response = response
        self._capture = capture
        self._captured = False

    @property
    def status_code(self) -> int:
        return self._response.status_code

    def json(self) -> object:
        try:
            payload = self._response.json()
        except Exception:
            raw_text = getattr(self._response, "text", None)
            self._record(
                {"raw_text": raw_text} if isinstance(raw_text, str) else None,
                "provider_response_invalid_json",
            )
            raise
        self._record(payload, None)
        return payload

    def _record(self, payload: object | None, failure: str | None) -> None:
        if not self._captured:
            self._capture(payload, failure)
            self._captured = True


class _CapturingFormatterClient:
    """Transparent formatter transport that retains chat exchanges only."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.exchanges: list[ProtectedExchange] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._client.get(url, **kwargs)

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> Any:
        if not url.endswith("/v1/chat/completions"):
            return self._client.post(
                url,
                json=json,
                headers=headers,
                timeout=timeout,
            )
        request = json
        try:
            response = self._client.post(
                url,
                json=json,
                headers=headers,
                timeout=timeout,
            )
        except Exception:
            self.exchanges.append(
                ProtectedExchange(
                    request=request,
                    response=None,
                    failure="provider_request_failed",
                )
            )
            raise
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool) and not 200 <= status < 300:
            raw_text = getattr(response, "text", None)
            self.exchanges.append(
                ProtectedExchange(
                    request=request,
                    response={
                        "status_code": status,
                        "raw_text": raw_text if isinstance(raw_text, str) else None,
                    },
                    failure="provider_failure_status",
                )
            )
            return response

        def capture(payload: object | None, failure: str | None) -> None:
            self.exchanges.append(
                ProtectedExchange(
                    request=request,
                    response=payload,
                    failure=failure,
                )
            )

        return _CapturedResponse(response, capture)


def _protected_artifact_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerExecutionError(
            "artifact_serialization_failed",
            "Protected diagnostic material could not be serialized.",
        ) from exc
    if not raw or len(raw) > _MAX_PROTECTED_ARTIFACT_BYTES:
        raise WorkerExecutionError(
            "artifact_size_exceeded",
            "Protected diagnostic material exceeded its byte limit.",
            retryable=False,
        )
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressor:
        compressor.write(raw)
    compressed = buffer.getvalue()
    if not compressed or len(compressed) > _MAX_PROTECTED_ARTIFACT_BYTES:
        raise WorkerExecutionError(
            "artifact_size_exceeded",
            "Protected diagnostic material exceeded its stored byte limit.",
            retryable=False,
        )
    return compressed


def _write_protected_artifact(
    layout: StorageLayout,
    *,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    artifact_id: uuid.UUID,
    payload: Mapping[str, Any],
) -> tuple[str, str, int]:
    data = _protected_artifact_bytes(payload)
    key = f"artifacts/{document_id}/{revision_id}/{artifact_id}.json.gz"
    path = resolve_storage_key(layout, key)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return key, hashlib.sha256(data).hexdigest(), len(data)


def _safe_failure(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, WorkerExecutionError):
        return exc.code, exc.message, exc.retryable
    if isinstance(exc, preparation.PreparationError):
        return exc.code[:100], " ".join(str(exc).split())[:_FAILURE_MESSAGE_LIMIT], True
    if isinstance(exc, VectorIndexConsistencyError):
        return "qdrant_consistency_failed", "Qdrant verification did not match catalog state.", True
    if isinstance(exc, VectorIndexError):
        return "qdrant_unavailable", "Required Qdrant point work did not complete.", True
    if isinstance(exc, LocalModelError):
        return "local_model_unavailable", "A required local model operation failed.", True
    if isinstance(exc, StorageError):
        return "storage_cleanup_failed", "Canonical storage work did not complete.", True
    return "worker_internal_error", "The worker failed while executing this phase.", True


def _model_identity(model_id: str | None, revision: str | None) -> str:
    if not model_id or not revision:
        raise WorkerExecutionError(
            "provider_not_configured",
            "A required immutable model identity is not configured.",
            retryable=False,
        )
    return f"{model_id}@{revision}"


class AnalysisWorker:
    """Durable two-slot executor for the exact target lifecycle."""

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
        self._busy_documents: set[uuid.UUID] = set()
        self._claim_times: dict[uuid.UUID, datetime] = {}
        self._threads: list[threading.Thread] = []

    # -- process lifecycle -------------------------------------------------

    def start(self) -> None:
        """Recover expired leases and launch exactly two execution slots."""

        if self._threads:
            raise RuntimeError("worker is already started")
        self._stop.clear()
        self.recover_leases()
        slots = int(self.settings.worker_execution_slots)
        if slots != WORKER_SLOTS:
            raise RuntimeError("target worker requires exactly two execution slots")
        for slot in range(slots):
            thread = threading.Thread(
                target=self._slot_loop,
                name=f"{self.worker_id}-slot-{slot}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name=f"{self.worker_id}-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        self._threads.append(heartbeat)
        self.notify()

    def stop(self) -> None:
        """Stop claiming new work and wait for bounded provider calls to finish."""

        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join()
        self._threads.clear()

    def notify(self) -> None:
        self._wake.set()

    def run_available(self, *, max_operations: int | None = None) -> int:
        """Synchronously drain durable work; used by tests and maintenance commands."""

        completed = 0
        while max_operations is None or completed < max_operations:
            claimed = self._claim_next()
            if claimed is None:
                break
            self._execute(claimed)
            completed += 1
        return completed

    # -- readiness ---------------------------------------------------------

    def readiness_checks(self) -> dict[str, tuple[bool, str | None]]:
        """Return content-safe readiness for every worker-owned dependency."""

        checks: dict[str, tuple[bool, str | None]] = {}
        checks["formatter"] = self._http_model_readiness(
            config=self.providers.formatter,
            model_ids=(
                (self.providers.formatter.model_id,) if self.providers.formatter is not None else ()
            ),
            label="formatter",
        )
        checks["advisory"] = self._http_model_readiness(
            config=self.providers.advisory,
            model_ids=(
                (
                    self.providers.advisory.classifier_model,
                    self.providers.advisory.verifier_model,
                )
                if self.providers.advisory is not None
                else ()
            ),
            label="advisory",
        )
        if self.providers.local_models is None:
            checks["local_models"] = (False, "local_models_not_configured")
        else:
            try:
                self.providers.local_models.validate_ready()
            except Exception:
                checks["local_models"] = (False, "local_models_unavailable")
            else:
                checks["local_models"] = (True, None)
        if self.providers.qdrant is None or not self.settings.qdrant_screening_collection_name:
            checks["qdrant"] = (False, "qdrant_not_configured")
        else:
            try:
                validate_fixed_collections(
                    self.providers.qdrant,
                    active_collections=[
                        item.qdrant_collection_name
                        for item in self.settings.collections
                        if item.enabled
                    ],
                    screening_collection=self.settings.qdrant_screening_collection_name,
                )
            except Exception:
                checks["qdrant"] = (False, "qdrant_unavailable")
            else:
                checks["qdrant"] = (True, None)
        return checks

    def _http_model_readiness(
        self,
        *,
        config: FormatterConfig | LlmConfig | None,
        model_ids: tuple[str, ...],
        label: str,
    ) -> tuple[bool, str | None]:
        client = self.providers.http_client
        if config is None or client is None or not model_ids:
            return False, f"{label}_not_configured"
        headers = {"Accept": "application/json"}
        token = getattr(config, "api_token", None)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = float(getattr(config, "timeout_seconds", getattr(config, "timeout", 30.0)))
        models_path = "/v1/models" if isinstance(config, FormatterConfig) else "/models"
        try:
            response = client.get(
                f"{config.api_url.rstrip('/')}{models_path}",
                headers=headers,
                timeout=timeout,
            )
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            elif not 200 <= int(response.status_code) < 300:
                raise RuntimeError("model endpoint returned a failure status")
            payload = response.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            available = (
                {
                    row.get("id")
                    for row in rows
                    if isinstance(row, dict) and isinstance(row.get("id"), str)
                }
                if isinstance(rows, list)
                else set()
            )
            if not set(model_ids).issubset(available):
                raise RuntimeError("configured model was not reported")
            if isinstance(config, FormatterConfig):
                tokenizer_response = client.get(
                    f"{config.api_url.rstrip('/')}/tokenizer_info",
                    headers=headers,
                    timeout=timeout,
                )
                if hasattr(tokenizer_response, "raise_for_status"):
                    tokenizer_response.raise_for_status()
                elif not 200 <= int(tokenizer_response.status_code) < 300:
                    raise RuntimeError("tokenizer endpoint returned a failure status")
                tokenizer_payload = tokenizer_response.json()
                if (
                    not isinstance(tokenizer_payload, dict)
                    or tokenizer_payload.get("tokenizer_class")
                    != config.expected_tokenizer_class
                ):
                    raise RuntimeError("configured tokenizer class was not reported")
                reported_model = tokenizer_payload.get("model") or tokenizer_payload.get(
                    "model_id"
                )
                if reported_model is not None and reported_model != config.model_id:
                    raise RuntimeError("tokenizer endpoint reported a different model")
        except Exception:
            return False, f"{label}_unavailable"
        return True, None

    # -- leases, threads, and claiming ------------------------------------

    def recover_leases(self) -> int:
        """Return every expired RUNNING attempt to QUEUED at its exact phase."""

        with self.session_factory.begin() as session:
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
            return len(stale)

    def _slot_loop(self) -> None:
        while not self._stop.is_set():
            claimed: ClaimedOperation | None = None
            try:
                claimed = self._claim_next()
                if claimed is None:
                    self._wake.wait(timeout=self.settings.worker_poll_seconds)
                    self._wake.clear()
                    continue
                self._execute(claimed)
            except Exception:
                logger.exception("worker slot failed")
                if claimed is not None:
                    self._release_claim(claimed)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(timeout=self.settings.worker_heartbeat_seconds):
            try:
                self.renew_leases()
            except Exception:
                logger.exception("worker heartbeat failed")

    def renew_leases(self) -> int:
        """Extend leases only for claims still inside the operation runtime cap.

        A claim older than ``worker_max_operation_seconds`` stops being renewed so
        its lease lapses, ``recover_leases`` requeues the operation, and the hung
        executing thread fails on its next durable step via ``LeaseLostError``.
        """

        now = utc_now()
        cutoff = now - timedelta(seconds=self.settings.worker_max_operation_seconds)
        with self._busy_lock:
            capped = {
                operation_id
                for operation_id, claimed_at in self._claim_times.items()
                if claimed_at <= cutoff
            }
        renewed = 0
        with self.session_factory.begin() as session:
            running = session.scalars(
                select(WorkOperation).where(
                    WorkOperation.state == OperationState.RUNNING,
                    WorkOperation.worker_id == self.worker_id,
                )
            ).all()
            for operation in running:
                if operation.id in capped:
                    continue
                operation.heartbeat_at = now
                operation.lease_expires_at = now + timedelta(
                    seconds=self.settings.worker_lease_seconds
                )
                renewed += 1
        if capped:
            logger.warning(
                "operation exceeded the runtime cap; lease renewal stopped",
                extra={"operation_ids": sorted(str(item) for item in capped)},
            )
        return renewed

    def _claim_next(self) -> ClaimedOperation | None:
        with self._claim_lock:
            self.recover_leases()
            with self.session_factory.begin() as session:
                with self._busy_lock:
                    busy = set(self._busy_documents)
                statement = (
                    select(WorkOperation)
                    .join(Document, WorkOperation.document_id == Document.id)
                    .where(WorkOperation.state == OperationState.QUEUED)
                    .order_by(
                        WorkOperation.priority.asc(),
                        WorkOperation.created_at.asc(),
                        WorkOperation.id.asc(),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if busy:
                    statement = statement.where(
                        WorkOperation.document_id.not_in(busy),
                        or_(
                            WorkOperation.replacement_target_document_id.is_(None),
                            WorkOperation.replacement_target_document_id.not_in(busy),
                        ),
                    )
                operation = session.scalar(statement)
                if operation is None:
                    return None
                document = session.get(Document, operation.document_id)
                if document is None:
                    raise RuntimeError("queued operation references a missing document")
                now = utc_now()
                operation.state = OperationState.RUNNING
                operation.worker_id = self.worker_id
                operation.started_at = operation.started_at or now
                operation.heartbeat_at = now
                operation.lease_expires_at = now + timedelta(
                    seconds=self.settings.worker_lease_seconds
                )
                claimed = ClaimedOperation(
                    operation_id=operation.id,
                    document_id=operation.document_id,
                    collection_key=document.collection_key,
                    operation_type=operation.operation_type,
                    locked_document_ids=tuple(
                        sorted(
                            {
                                operation.document_id,
                                *(
                                    (operation.replacement_target_document_id,)
                                    if operation.replacement_target_document_id is not None
                                    else ()
                                ),
                            },
                            key=str,
                        )
                    ),
                )
            with self._busy_lock:
                if set(claimed.locked_document_ids) & self._busy_documents:
                    raise RuntimeError("document work exclusion changed while claiming")
                self._busy_documents.update(claimed.locked_document_ids)
                self._claim_times[claimed.operation_id] = utc_now()
            return claimed

    def _release_claim(self, claimed: ClaimedOperation) -> None:
        with self._busy_lock:
            self._busy_documents.difference_update(claimed.locked_document_ids)
            self._claim_times.pop(claimed.operation_id, None)

    # -- operation dispatch and durable helpers ---------------------------

    def _execute(self, claimed: ClaimedOperation) -> None:
        try:
            if claimed.operation_type is OperationType.PREFLIGHT:
                self._run_preflight(claimed.operation_id, claimed.document_id)
            elif claimed.operation_type is OperationType.PUBLISH:
                self._run_publish(claimed.operation_id, claimed.document_id)
            elif claimed.operation_type is OperationType.DELETE:
                self._run_delete(claimed.operation_id, claimed.document_id)
            else:  # Defensive against catalog corruption or a future unhandled enum.
                raise WorkerExecutionError(
                    "operation_type_invalid",
                    "The durable operation type is unsupported.",
                    retryable=False,
                )
        except LeaseLostError:
            logger.warning(
                "operation lease was lost",
                extra={"operation_id": str(claimed.operation_id)},
            )
        except Exception as exc:
            code, _message, _retryable = _safe_failure(exc)
            logger.error(
                "worker operation failed",
                extra={
                    "operation_id": str(claimed.operation_id),
                    "failure_code": code,
                },
            )
            self._fail_operation(claimed.operation_id, exc)
        finally:
            self._release_claim(claimed)

    def _operation_snapshot(self, operation_id: uuid.UUID) -> OperationSnapshot:
        with self.session_factory() as session:
            operation = session.get(WorkOperation, operation_id)
            if (
                operation is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
            ):
                raise LeaseLostError()
            return OperationSnapshot(
                operation_id=operation.id,
                document_id=operation.document_id,
                prepared_revision_id=operation.prepared_revision_id,
                replacement_target_document_id=operation.replacement_target_document_id,
                operation_type=operation.operation_type,
                phase=operation.phase,
            )

    def _set_phase(self, operation_id: uuid.UUID, phase: OperationPhase) -> None:
        if phase in _TERMINAL_OPERATION_PHASES:
            raise ValueError("terminal phases are written only with operation completion")
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            if (
                operation is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
            ):
                raise LeaseLostError()
            intake.set_operation_phase(operation, phase)

    def _bind_revision(self, operation_id: uuid.UUID, revision_id: uuid.UUID) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            revision = session.get(PreparedRevision, revision_id)
            if (
                operation is None
                or revision is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or revision.document_id != operation.document_id
            ):
                raise LeaseLostError()
            operation.prepared_revision_id = revision.id

    def _persist_revision_artifact(
        self,
        *,
        revision_id: uuid.UUID,
        kind: str,
        payload: Mapping[str, Any],
    ) -> None:
        if not kind or len(kind) > 64:
            raise ValueError("revision artifact kind must be a bounded non-blank value")
        with self.session_factory() as session:
            revision = session.get(PreparedRevision, revision_id)
            if revision is None:
                raise WorkerExecutionError(
                    "revision_not_found",
                    "Protected diagnostics lost their prepared revision binding.",
                    retryable=False,
                )
            if revision.status is RevisionStatus.SEALED:
                raise WorkerExecutionError(
                    "revision_state_conflict",
                    "Protected diagnostics cannot be added after sealing.",
                    retryable=False,
                )
            document_id = revision.document_id
            maximum = session.scalar(
                select(func.max(RevisionArtifact.sequence)).where(
                    RevisionArtifact.prepared_revision_id == revision_id,
                    RevisionArtifact.kind == kind,
                )
            )
            sequence = int(maximum) + 1 if maximum is not None else 0

        artifact_id = uuid.uuid4()
        layout = StorageLayout.from_root(self.settings.storage_root)
        key, digest, size = _write_protected_artifact(
            layout,
            document_id=document_id,
            revision_id=revision_id,
            artifact_id=artifact_id,
            payload={
                "artifact_version": 1,
                "document_id": str(document_id),
                "prepared_revision_id": str(revision_id),
                "kind": kind,
                "sequence": sequence,
                "material": payload,
            },
        )
        try:
            with self.session_factory.begin() as session:
                revision = session.get(PreparedRevision, revision_id)
                if (
                    revision is None
                    or revision.document_id != document_id
                    or revision.status is RevisionStatus.SEALED
                ):
                    raise WorkerExecutionError(
                        "revision_state_conflict",
                        "Protected diagnostics lost their mutable revision binding.",
                        retryable=False,
                    )
                session.add(
                    RevisionArtifact(
                        id=artifact_id,
                        prepared_revision_id=revision_id,
                        kind=kind,
                        sequence=sequence,
                        storage_key=key,
                        sha256=digest,
                        size_bytes=size,
                    )
                )
        except Exception:
            remove_storage_key(layout, key, missing_ok=True)
            raise

    def _persist_formatter_exchanges(
        self,
        revision_id: uuid.UUID,
        exchanges: Sequence[ProtectedExchange],
    ) -> None:
        for exchange in exchanges:
            self._persist_revision_artifact(
                revision_id=revision_id,
                kind="formatter_exchange",
                payload={
                    "request": exchange.request,
                    "response": exchange.response,
                    "failure": exchange.failure,
                },
            )

    def _persist_advisory_result(
        self,
        *,
        revision_id: uuid.UUID,
        candidate_id: uuid.UUID,
        result: FindingResult,
    ) -> None:
        self._persist_revision_artifact(
            revision_id=revision_id,
            kind="advisory_exchange",
            payload={
                "candidate_id": str(candidate_id),
                "role": result.role,
                "model_id": result.model_id,
                "model_revision": result.model_revision,
                "prompt_revision": result.prompt_revision,
                "system_prompt": result.system_prompt,
                "prompt": result.prompt,
                "input_tokens": result.input_tokens,
                "attempts": result.attempts,
                "raw_outputs": list(result.raw_outputs),
                "valid": result.valid,
                "validation_error": result.error,
            },
        )

    def _begin_outbox(
        self,
        *,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        target: IndexTarget,
        action: IndexAction,
        collection: str,
        expected_points: int,
    ) -> int:
        if not collection or expected_points < 0:
            raise ValueError("outbox target and expected point count must be valid")
        with self.session_factory.begin() as session:
            revision = session.get(PreparedRevision, revision_id)
            if revision is None or revision.document_id != document_id:
                raise WorkerExecutionError(
                    "outbox_binding_invalid",
                    "Index work lost its exact prepared revision binding.",
                    retryable=False,
                )
            entry = session.scalar(
                select(IndexOutboxEntry).where(
                    IndexOutboxEntry.prepared_revision_id == revision_id,
                    IndexOutboxEntry.target == target,
                    IndexOutboxEntry.action == action,
                    IndexOutboxEntry.qdrant_collection == collection,
                )
            )
            if entry is None:
                entry = IndexOutboxEntry(
                    document_id=document_id,
                    prepared_revision_id=revision_id,
                    target=target,
                    action=action,
                    qdrant_collection=collection,
                    expected_points=expected_points,
                    state=OutboxState.PENDING,
                )
                session.add(entry)
                session.flush()
            elif (
                entry.document_id != document_id
                or entry.expected_points != expected_points
                or entry.state is OutboxState.SUPERSEDED
            ):
                raise WorkerExecutionError(
                    "outbox_binding_invalid",
                    "Existing index work did not match the exact durable target.",
                    retryable=False,
                )
            entry.attempts += 1
            entry.failure_code = None
            entry.failure_message = None
            return entry.id

    def _complete_outbox(self, outbox_id: int) -> None:
        with self.session_factory.begin() as session:
            entry = session.get(IndexOutboxEntry, outbox_id)
            if entry is None or entry.state is OutboxState.SUPERSEDED:
                raise WorkerExecutionError(
                    "outbox_binding_invalid",
                    "Completed index work lost its durable target.",
                    retryable=False,
                )
            entry.state = OutboxState.APPLIED
            entry.failure_code = None
            entry.failure_message = None
            entry.applied_at = utc_now()

    def _succeed_operation(
        self, operation_id: uuid.UUID, *, phase: OperationPhase = OperationPhase.COMPLETE
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            if (
                operation is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
            ):
                raise LeaseLostError()
            now = utc_now()
            operation.state = OperationState.SUCCEEDED
            intake.set_operation_phase(operation, phase, now=now)
            operation.retryable = False
            operation.completed_at = now
            operation.lease_expires_at = None
            operation.heartbeat_at = None
            operation.worker_id = None

    def _fail_operation(self, operation_id: uuid.UUID, exc: Exception) -> None:
        code, message, retryable = _safe_failure(exc)
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            if operation is None or operation.state is not OperationState.RUNNING:
                return
            document = session.get(Document, operation.document_id)
            if document is None:
                raise RuntimeError("operation failure references a missing document")
            affected_document_ids = {document.id}
            operation.state = OperationState.FAILED
            operation.failure_code = code
            operation.failure_message = message
            operation.retryable = retryable
            operation.completed_at = utc_now()
            operation.lease_expires_at = None
            operation.heartbeat_at = None
            operation.worker_id = None

            if operation.operation_type is OperationType.PREFLIGHT:
                if document.state is not DocumentState.DELETING:
                    document.state = DocumentState.PREFLIGHT_FAILED
            elif operation.operation_type is OperationType.PUBLISH:
                document.state = DocumentState.PUBLISH_FAILED
                publication = (
                    session.scalar(
                        select(PublicationRecord).where(
                            PublicationRecord.prepared_revision_id == operation.prepared_revision_id
                        )
                    )
                    if operation.prepared_revision_id is not None
                    else None
                )
                if publication is not None and publication.status is not PublicationStatus.VERIFIED:
                    publication.status = PublicationStatus.FAILED
                    publication.failure_code = code
                if operation.replacement_target_document_id is not None:
                    old = session.get(Document, operation.replacement_target_document_id)
                    affected_document_ids.add(operation.replacement_target_document_id)
                    if old is not None and old.state is DocumentState.DELETING:
                        old.state = DocumentState.DELETE_FAILED
                        old.failure_code = code
                        old.failure_message = message
                        old.failure_retryable = retryable
                        if old.deletion_progress is not None:
                            old.deletion_progress.failure_code = code
            else:
                document.state = DocumentState.DELETE_FAILED
                if document.deletion_progress is not None:
                    document.deletion_progress.failure_code = code

            document.failure_code = code
            document.failure_message = message
            document.failure_retryable = retryable
            pending_outbox = session.scalars(
                select(IndexOutboxEntry).where(
                    IndexOutboxEntry.document_id.in_(affected_document_ids),
                    IndexOutboxEntry.state == OutboxState.PENDING,
                )
            ).all()
            for entry in pending_outbox:
                entry.failure_code = code
                entry.failure_message = message
            intake.audit(
                session,
                event_type=f"{operation.operation_type.value.lower()}_failed",
                actor_type="system",
                actor_id=self.worker_id,
                document_id=document.id,
                operation_id=operation.id,
                details={
                    "code": code,
                    "phase": operation.phase.value,
                    "retryable": retryable,
                },
            )

    def _collection_definition(self, collection_key: str) -> CollectionDefinition:
        definition = next(
            (
                item
                for item in self.settings.collections
                if item.key == collection_key and item.enabled
            ),
            None,
        )
        if definition is None:
            raise WorkerExecutionError(
                "collection_not_configured",
                "The document collection is not enabled in target configuration.",
                retryable=False,
            )
        return definition

    def _required_screening_collection(self) -> str:
        value = self.settings.qdrant_screening_collection_name
        if not value:
            raise WorkerExecutionError(
                "provider_not_configured",
                "The private screening collection is not configured.",
                retryable=False,
            )
        return value

    def _required_qdrant(self) -> QdrantPointClient:
        if self.providers.qdrant is None:
            raise WorkerExecutionError(
                "provider_not_configured", "Qdrant is not configured.", retryable=False
            )
        return self.providers.qdrant

    def _required_preparation_providers(
        self,
    ) -> tuple[LocalModelsProvider, EnglishDetector, FormatterConfig, Any]:
        if (
            self.providers.local_models is None
            or self.providers.language_detector is None
            or self.providers.formatter is None
            or self.providers.http_client is None
        ):
            raise WorkerExecutionError(
                "provider_not_configured",
                "Preparation providers are not fully configured.",
                retryable=False,
            )
        return (
            self.providers.local_models,
            self.providers.language_detector,
            self.providers.formatter,
            self.providers.http_client,
        )

    def _pipeline_identity(
        self, collection: CollectionDefinition
    ) -> preparation.PreparationIdentity:
        formatter_model = self.settings.formatter_model_id
        formatter_tokenizer_class = self.settings.formatter_tokenizer_class
        if formatter_model is None or formatter_tokenizer_class is None:
            raise WorkerExecutionError(
                "provider_not_configured",
                "The formatter model and tokenizer identities are not configured.",
                retryable=False,
            )
        profiles = build_pipeline_profiles(
            content_inputs={
                "extraction_profile": EXTRACTION_PROFILE,
                "language_profile": LANGUAGE_PROFILE,
                "extraction_mode": self.settings.pypdf_extraction_mode,
                "max_pages": self.settings.max_pages,
                "max_characters": self.settings.max_extracted_characters,
                "formatter_version": MARKDOWN_FORMATTER_VERSION,
                "formatter_model": formatter_model,
                "formatter_model_revision": self.settings.formatter_model_revision,
                "formatter_tokenizer_class": formatter_tokenizer_class,
                "formatter_prompt_revision": self.settings.formatter_prompt_revision,
                "formatter_schema_revision": self.settings.formatter_schema_revision,
                "formatter_max_input_tokens": self.settings.formatter_max_input_tokens,
                "formatter_max_output_tokens": self.settings.formatter_max_output_tokens,
                "formatter_token_safety_reserve": self.settings.formatter_token_safety_reserve,
                "formatter_max_pages_per_request": self.settings.formatter_max_pages_per_request,
                "formatter_max_attempts": self.settings.formatter_max_attempts,
                "chunker_profile": CHUNKER_PROFILE,
                "chunk_max_tokens": HARD_MAX_TOKENS,
            },
            index_inputs={
                "schema_version": INDEX_SCHEMA_VERSION,
                "dense_model": self.settings.dense_model_id,
                "dense_revision": self.settings.dense_model_revision,
                "dense_dimension": DENSE_DIMENSION,
                "dense_normalized": True,
                "sparse_model": self.settings.sparse_model_id,
                "sparse_revision": self.settings.sparse_model_revision,
                "sparse_idf": self.settings.sparse_idf,
                "sparse_document_encoding": True,
            },
            preflight_policy_inputs={
                "candidate_version": CANDIDATES_VERSION,
                "filename_version": FILENAME_ANALYSIS_VERSION,
                "dense_top_k": DENSE_TOP_K,
                "bm25_top_k": BM25_TOP_K,
                "cosine_strong_threshold": COSINE_STRONG_THRESHOLD,
                "cosine_multi_threshold": COSINE_MULTI_THRESHOLD,
                "cosine_multi_min_chunks": COSINE_MULTI_MIN_CHUNKS,
                "bm25_strong_placement_rank": BM25_STRONG_PLACEMENT_RANK,
                "bm25_strong_min_chunks": BM25_STRONG_MIN_CHUNKS,
                "rrf_k": RRF_K,
                "max_classified_candidates": MAX_CLASSIFIED_CANDIDATES,
                "classification_version": CLASSIFICATION_VERSION,
                "filename_token_set_similarity_threshold": (TOKEN_SET_SIMILARITY_THRESHOLD),
                "filename_jaro_winkler_threshold": JARO_WINKLER_THRESHOLD,
                "filename_min_family_substantive_tokens": (MIN_FAMILY_SUBSTANTIVE_TOKENS),
                "classifier_model": self.settings.llm_classifier_model,
                "classifier_model_revision": self.settings.llm_classifier_model_revision,
                "classifier_prompt_revision": self.settings.llm_classifier_prompt_revision,
                "verifier_model": self.settings.llm_verifier_model,
                "verifier_model_revision": self.settings.llm_verifier_model_revision,
                "verifier_prompt_revision": self.settings.llm_verifier_prompt_revision,
                "llm_max_input_tokens": self.settings.llm_max_input_tokens,
                "llm_max_output_tokens": self.settings.llm_max_output_tokens,
                "llm_max_attempts": self.settings.llm_max_attempts,
            },
            active_qdrant_collection=collection.qdrant_collection_name,
        )
        return preparation.PreparationIdentity(
            active_qdrant_collection=collection.qdrant_collection_name,
            profiles=profiles,
            formatter_model_id=formatter_model,
            formatter_tokenizer_class=formatter_tokenizer_class,
            dense_model_id=_model_identity(
                self.settings.dense_model_id, self.settings.dense_model_revision
            ),
            sparse_model_id=_model_identity(
                self.settings.sparse_model_id, self.settings.sparse_model_revision
            ),
        )

    # -- preflight ---------------------------------------------------------

    def _preflight_source(
        self, operation_id: uuid.UUID, document_id: uuid.UUID
    ) -> DocumentWorkSnapshot:
        with self.session_factory() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if (
                operation is None
                or document is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or operation.operation_type is not OperationType.PREFLIGHT
            ):
                raise LeaseLostError()
            if document.state is not DocumentState.PREFLIGHTING or not document.storage_key:
                raise WorkerExecutionError(
                    "document_state_conflict",
                    "The preflight document is no longer processable.",
                    retryable=False,
                )
            storage_key = document.storage_key
            collection_key = document.collection_key
        path = resolve_storage_key(StorageLayout.from_root(self.settings.storage_root), storage_key)
        if not path.is_file():
            raise WorkerExecutionError(
                "source_missing",
                "The canonical source object is missing.",
                retryable=False,
            )
        return DocumentWorkSnapshot(
            document_id=document_id,
            collection_key=collection_key,
            storage_key=storage_key,
            source_path=path,
        )

    def _run_preflight(self, operation_id: uuid.UUID, document_id: uuid.UUID) -> None:
        snapshot = self._preflight_source(operation_id, document_id)
        collection = self._collection_definition(snapshot.collection_key)
        identity = self._pipeline_identity(collection)
        models, detector, formatter, formatter_client = self._required_preparation_providers()
        captured_formatter = _CapturingFormatterClient(formatter_client)
        self._set_phase(operation_id, OperationPhase.EXTRACTING)
        try:
            content = preparation.prepare_revision_content(
                self.session_factory,
                document_id=document_id,
                identity=identity,
                source_path=snapshot.source_path,
                extraction_limits=ExtractionLimits(
                    max_pages=self.settings.max_pages,
                    max_characters=self.settings.max_extracted_characters,
                    cpu_seconds=self.settings.parse_cpu_seconds,
                    memory_bytes=self.settings.parse_memory_bytes,
                    wall_clock_seconds=self.settings.parse_wall_clock_seconds,
                ),
                language_detector=detector,
                formatter_config=formatter,
                formatter_client=captured_formatter,
                embedding_models=models,
                max_chunks=self.settings.max_chunks,
                progress_callback=lambda phase: self._set_phase(operation_id, phase),
            )
        except preparation.PreparationError as exc:
            self._persist_failed_formatter_exchanges(document_id, captured_formatter.exchanges)
            if self._is_terminal_rejection(exc.code):
                self._accept_rejection(operation_id, document_id, exc.code, collection)
                return
            raise

        self._bind_revision(operation_id, content.handle.revision_id)
        try:
            self._persist_formatter_exchanges(
                content.handle.revision_id, captured_formatter.exchanges
            )
            self._write_screening_points(operation_id, content)
            self._set_phase(operation_id, OperationPhase.DISCOVERING_CANDIDATES)
            discoveries = self._discover_candidates(content)
            deterministic_inputs = self._deterministic_candidate_inputs(discoveries)
            candidate_ids = preparation.record_preflight_candidates(
                self.session_factory,
                handle=content.handle,
                candidates=deterministic_inputs,
            )

            self._set_phase(operation_id, OperationPhase.CLASSIFYING_CANDIDATES)
            advisory, advisory_complete, incomplete_reasons = self._classify_candidates(
                content.handle,
                discoveries,
                candidate_ids,
            )
            preparation.append_advisory_evidence(
                self.session_factory,
                handle=content.handle,
                evidence_by_candidate=advisory,
            )

            self._set_phase(operation_id, OperationPhase.SEALING_REVISION)
            clear = not discoveries and advisory_complete
            sealed = preparation.seal_prepared_revision(
                self.session_factory,
                handle=content.handle,
                completeness=preparation.PreflightCompleteness(
                    candidate_discovery_complete=True,
                    advisory_complete=advisory_complete,
                    clear_for_publication=clear,
                    incomplete_reasons=tuple(sorted(incomplete_reasons)),
                ),
            )
            self._finish_preflight(operation_id, sealed)
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, preparation.PreparationError)
                else preparation.PreparationError(_safe_failure(exc)[0], _safe_failure(exc)[1])
            )
            try:
                preparation.fail_preparation(
                    self.session_factory,
                    revision_id=content.handle.revision_id,
                    error=error,
                )
            except preparation.PreparationError as checkpoint_error:
                if checkpoint_error.code != "revision_state_conflict":
                    raise
            raise

    def _persist_failed_formatter_exchanges(
        self,
        document_id: uuid.UUID,
        exchanges: Sequence[ProtectedExchange],
    ) -> None:
        if not exchanges:
            return
        with self.session_factory() as session:
            revision = intake.latest_revision(session, document_id)
            if revision is None or revision.status is RevisionStatus.SEALED:
                raise WorkerExecutionError(
                    "artifact_revision_missing",
                    "Formatter diagnostics had no mutable prepared revision.",
                    retryable=False,
                )
            revision_id = revision.id
        self._persist_formatter_exchanges(revision_id, exchanges)

    @staticmethod
    def _is_terminal_rejection(code: str) -> bool:
        return code in {
            "extraction_encrypted",
            "extraction_malformed",
            "extraction_empty",
            "extraction_page-budget",
            "extraction_character-budget",
            "extraction_image-only",
            "extraction_text-insufficient",
            "extraction_non-english",
        }

    def _accept_rejection(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        reason_code: str,
        collection: CollectionDefinition,
    ) -> None:
        screening = self._required_screening_collection()
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if (
                operation is None
                or document is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
            ):
                raise LeaseLostError()
            revision = intake.latest_revision(session, document.id)
            operation.prepared_revision_id = revision.id if revision is not None else None
            intake.queue_rejection_cleanup(
                session,
                document=document,
                reason_code=reason_code,
                active_qdrant_collection=(
                    revision.active_qdrant_collection
                    if revision is not None
                    else collection.qdrant_collection_name
                ),
                screening_qdrant_collection=screening,
                prepared_revision_id=revision.id if revision is not None else None,
            )
            now = utc_now()
            operation.state = OperationState.SUCCEEDED
            intake.set_operation_phase(operation, OperationPhase.COMPLETE, now=now)
            operation.retryable = False
            operation.completed_at = now
            operation.lease_expires_at = None
            operation.heartbeat_at = None
            operation.worker_id = None
            intake.audit(
                session,
                event_type="preflight_rejected_cleanup_queued",
                actor_type="system",
                actor_id=self.worker_id,
                document_id=document.id,
                operation_id=operation.id,
                details={"reason_code": reason_code},
            )
        self.notify()

    def _write_screening_points(
        self,
        operation_id: uuid.UUID,
        content: preparation.ContentPreparationResult,
    ) -> None:
        qdrant = self._required_qdrant()
        screening = self._required_screening_collection()
        self._set_phase(operation_id, OperationPhase.UPSERTING_SCREENING_POINTS)
        # A failed earlier preparation may have left this document's old
        # deterministic IDs in screening. Remove them before writing the new
        # monotonic revision so the document cannot become its own candidate.
        outbox_id = self._begin_outbox(
            document_id=content.handle.document_id,
            revision_id=content.handle.revision_id,
            target=IndexTarget.SCREENING,
            action=IndexAction.UPSERT,
            collection=screening,
            expected_points=len(content.points),
        )
        delete_document_points(qdrant, collection=screening, document_id=content.handle.document_id)
        verify_document_zero(qdrant, collection=screening, document_id=content.handle.document_id)
        upsert_screening_points(
            qdrant,
            screening_collection=screening,
            points=list(content.points),
        )
        verify_prepared_points(
            qdrant,
            collection=screening,
            points=list(content.points),
            published=False,
            visibility="screening",
        )
        self._complete_outbox(outbox_id)

    def _discover_candidates(
        self, content: preparation.ContentPreparationResult
    ) -> tuple[CandidateDiscovery, ...]:
        qdrant = self._required_qdrant()
        models = self.providers.local_models
        if models is None:
            raise WorkerExecutionError(
                "provider_not_configured",
                "Sparse query encoding is not configured.",
                retryable=False,
            )
        screening = self._required_screening_collection()
        sparse_queries = models.embed_sparse_queries([point.markdown for point in content.points])
        dense_results: list[list[ChunkHit]] = []
        sparse_results: list[list[ChunkHit]] = []
        revision_by_document: dict[uuid.UUID, uuid.UUID] = {}
        source_by_document: dict[uuid.UUID, str] = {}

        def convert(hits: Sequence[Any], source: str) -> list[ChunkHit]:
            converted: list[ChunkHit] = []
            for hit in hits:
                if hit.document_id == content.handle.document_id:
                    continue
                previous_revision = revision_by_document.setdefault(
                    hit.document_id, hit.prepared_revision_id
                )
                previous_source = source_by_document.setdefault(hit.document_id, source)
                if previous_revision != hit.prepared_revision_id or previous_source != source:
                    raise VectorIndexConsistencyError(
                        "candidate document appeared with conflicting revision visibility"
                    )
                converted.append(
                    ChunkHit(
                        document_id=hit.document_id,
                        source=source,  # type: ignore[arg-type]
                        chunk_id=str(hit.chunk_id),
                        score=hit.score,
                        rank=hit.rank,
                    )
                )
            return converted

        for point, sparse_query in zip(content.points, sparse_queries, strict=True):
            active_dense = query_candidates(
                qdrant,
                collection=point.active_qdrant_collection,
                query=point.dense,
                using="dense",
                collection_key=content.handle.collection_key,
                published=True,
                visibility="active",
                source="active",
                exclude_revision_id=content.handle.revision_id,
                limit=DENSE_TOP_K,
            )
            screening_dense = query_candidates(
                qdrant,
                collection=screening,
                query=point.dense,
                using="dense",
                collection_key=content.handle.collection_key,
                published=False,
                visibility="screening",
                source="screening",
                exclude_revision_id=content.handle.revision_id,
                limit=DENSE_TOP_K,
            )
            active_sparse = query_candidates(
                qdrant,
                collection=point.active_qdrant_collection,
                query=sparse_query,
                using="bm25",
                collection_key=content.handle.collection_key,
                published=True,
                visibility="active",
                source="active",
                exclude_revision_id=content.handle.revision_id,
                limit=BM25_TOP_K,
            )
            screening_sparse = query_candidates(
                qdrant,
                collection=screening,
                query=sparse_query,
                using="bm25",
                collection_key=content.handle.collection_key,
                published=False,
                visibility="screening",
                source="screening",
                exclude_revision_id=content.handle.revision_id,
                limit=BM25_TOP_K,
            )
            dense_results.append(
                [
                    *convert(active_dense, "active"),
                    *convert(screening_dense, "screening"),
                ]
            )
            sparse_results.append(
                [
                    *convert(active_sparse, "active"),
                    *convert(screening_sparse, "screening"),
                ]
            )

        filename_ids, identical_ids, catalog_sources, catalog_revisions = (
            self._catalog_candidate_context(
                handle=content.handle,
                markdown_sha256=content.markdown_sha256,
                qdrant_sources=source_by_document,
                qdrant_revisions=revision_by_document,
            )
        )
        evaluated = evaluate_candidates(
            dense_results=dense_results,
            bm25_results=sparse_results,
            filename_family_ids=filename_ids,
            identical_text_ids=identical_ids,
            sources=catalog_sources,  # type: ignore[arg-type]
        )
        return tuple(
            CandidateDiscovery(
                candidate=item,
                prepared_revision_id=catalog_revisions.get(item.document_id),
            )
            for item in evaluated
        )

    def _catalog_candidate_context(
        self,
        *,
        handle: preparation.PreparationHandle,
        markdown_sha256: str,
        qdrant_sources: Mapping[uuid.UUID, str],
        qdrant_revisions: Mapping[uuid.UUID, uuid.UUID],
    ) -> tuple[
        set[uuid.UUID],
        set[uuid.UUID],
        dict[uuid.UUID, str],
        dict[uuid.UUID, uuid.UUID],
    ]:
        allowed_screening_states = {
            DocumentState.PREFLIGHTING,
            DocumentState.REVIEW_REQUIRED,
            DocumentState.PUBLISHING,
            DocumentState.PUBLISH_FAILED,
        }
        with self.session_factory() as session:
            incoming = session.get(Document, handle.document_id)
            if incoming is None:
                raise WorkerExecutionError(
                    "document_not_found", "The preflight document was not found.", retryable=False
                )
            catalog_documents = session.scalars(
                select(Document).where(
                    Document.collection_key == handle.collection_key,
                    Document.id != handle.document_id,
                    Document.state.in_({DocumentState.READY, *allowed_screening_states}),
                )
            ).all()
            catalog_by_id = {item.id: item for item in catalog_documents}
            complete_revisions = session.scalars(
                select(PreparedRevision)
                .where(
                    PreparedRevision.document_id.in_(catalog_by_id),
                    PreparedRevision.vector_complete.is_(True),
                )
                .order_by(
                    PreparedRevision.document_id,
                    PreparedRevision.revision_number.desc(),
                )
            ).all()
            latest_complete_by_document: dict[uuid.UUID, PreparedRevision] = {}
            complete_by_id: dict[uuid.UUID, PreparedRevision] = {}
            for revision in complete_revisions:
                latest_complete_by_document.setdefault(revision.document_id, revision)
                complete_by_id[revision.id] = revision

            # A concurrent preflight becomes visible in the catalog before it
            # has any content that can safely support filename or text-match
            # evidence.  Do not admit that document as a candidate until a
            # durable vector-complete revision exists.  Its next preflight (or
            # another document's later discovery pass) will see it once the
            # content checkpoint has committed.
            by_id = {
                document_id: document
                for document_id, document in catalog_by_id.items()
                if document_id in latest_complete_by_document
            }
            if set(qdrant_sources) != set(qdrant_revisions):
                raise VectorIndexConsistencyError(
                    "candidate point source and revision identities did not match"
                )
            for document_id, source in qdrant_sources.items():
                document = by_id.get(document_id)
                retained_revision = complete_by_id.get(qdrant_revisions[document_id])
                if (
                    document is None
                    or retained_revision is None
                    or retained_revision.document_id != document_id
                    or (source == "active" and document.state is not DocumentState.READY)
                    or (source == "screening" and document.state not in allowed_screening_states)
                ):
                    raise VectorIndexConsistencyError(
                        "candidate point did not match catalog visibility"
                    )

            incoming_filename = profile_filename(incoming.original_filename)
            filename_ids = {
                item.id
                for item in by_id.values()
                if compare_filenames(incoming_filename, profile_filename(item.original_filename))
                is not None
            }
            revision_rows = [
                revision
                for revision in complete_revisions
                if revision.markdown_sha256 == markdown_sha256
            ]
            identical_ids = {item.document_id for item in revision_rows}
            revisions = dict(qdrant_revisions)
            for row in revision_rows:
                revisions.setdefault(row.document_id, row.id)
            sources = {
                item.id: ("active" if item.state is DocumentState.READY else "screening")
                for item in by_id.values()
            }
            sources.update(qdrant_sources)
            for document_id, latest in latest_complete_by_document.items():
                revisions.setdefault(document_id, latest.id)
        return filename_ids, identical_ids, sources, revisions

    @staticmethod
    def _deterministic_candidate_inputs(
        discoveries: Sequence[CandidateDiscovery],
    ) -> tuple[preparation.CandidateInput, ...]:
        inputs: list[preparation.CandidateInput] = []
        for item in discoveries:
            candidate = item.candidate
            pairs: list[tuple[int, uuid.UUID]] = []
            for incoming_index, chunk_id in candidate.matched_chunk_pairs:
                try:
                    pairs.append((incoming_index, uuid.UUID(chunk_id)))
                except ValueError as exc:
                    raise VectorIndexConsistencyError(
                        "candidate chunk identity was invalid"
                    ) from exc
            inputs.append(
                preparation.CandidateInput(
                    matched_document_id=candidate.document_id,
                    source=(
                        CandidateSource.ACTIVE
                        if candidate.source == "active"
                        else CandidateSource.SCREENING
                    ),
                    reasons=tuple(candidate.reasons),
                    max_cosine=candidate.max_cosine,
                    bm25_score=float(candidate.bm25_strong_placements),
                    fused_score=candidate.fused_score,
                    matched_chunk_pairs=tuple(pairs),
                    evidence=(
                        preparation.EvidenceInput(
                            kind=EvidenceKind.DETERMINISTIC,
                            model_id=None,
                            valid=True,
                            label="candidate",
                            summary="Deterministic preflight rules selected this candidate.",
                            citations=(),
                            failure_code=None,
                        ),
                    ),
                )
            )
        return tuple(inputs)

    def _classify_candidates(
        self,
        handle: preparation.PreparationHandle,
        discoveries: Sequence[CandidateDiscovery],
        candidate_ids: Sequence[uuid.UUID],
    ) -> tuple[
        dict[uuid.UUID, tuple[preparation.EvidenceInput, ...]],
        bool,
        set[str],
    ]:
        if len(discoveries) != len(candidate_ids):
            raise WorkerExecutionError(
                "candidate_correlation_failed",
                "Persisted candidate identities did not match discovery order.",
                retryable=False,
            )
        if not discoveries:
            return {}, True, set()
        config = self.providers.advisory
        client = self.providers.http_client
        if config is None or client is None:
            raise WorkerExecutionError(
                "provider_not_configured",
                "Advisory classifier providers are not configured.",
                retryable=False,
            )
        evidence_by_candidate: dict[uuid.UUID, tuple[preparation.EvidenceInput, ...]] = {}
        incomplete: set[str] = set()
        all_valid = True
        for index, (discovery, candidate_id) in enumerate(
            zip(discoveries, candidate_ids, strict=True)
        ):
            if index >= MAX_CLASSIFIED_CANDIDATES:
                all_valid = False
                incomplete.add("advisory_candidate_budget")
                evidence_by_candidate[candidate_id] = (
                    self._unavailable_advisory(
                        EvidenceKind.CLASSIFIER,
                        _model_identity(
                            config.classifier_model,
                            config.classifier_model_revision,
                        ),
                    ),
                    self._unavailable_advisory(
                        EvidenceKind.VERIFIER,
                        _model_identity(
                            config.verifier_model,
                            config.verifier_model_revision,
                        ),
                    ),
                    preparation.EvidenceInput(
                        kind=EvidenceKind.INCOMPLETE,
                        model_id=None,
                        valid=False,
                        label=None,
                        summary="Candidate exceeded the bounded advisory review budget.",
                        citations=(),
                        failure_code="advisory_candidate_budget",
                    ),
                )
                continue
            incoming, candidate, citation_sources = self._classification_excerpts(
                handle,
                candidate_document_id=discovery.candidate.document_id,
                candidate_revision_id=discovery.prepared_revision_id,
            )
            records: list[preparation.EvidenceInput] = []
            for role, kind, model_id in (
                (
                    "classifier",
                    EvidenceKind.CLASSIFIER,
                    _model_identity(
                        config.classifier_model,
                        config.classifier_model_revision,
                    ),
                ),
                (
                    "verifier",
                    EvidenceKind.VERIFIER,
                    _model_identity(
                        config.verifier_model,
                        config.verifier_model_revision,
                    ),
                ),
            ):
                try:
                    result = classify_candidate(
                        config,
                        role=role,  # type: ignore[arg-type]
                        incoming_excerpts=incoming,
                        candidate_excerpts=candidate,
                        client=client,
                    )
                    self._persist_advisory_result(
                        revision_id=handle.revision_id,
                        candidate_id=candidate_id,
                        result=result,
                    )
                    record = self._finding_record(kind, result, citation_sources)
                except ClassificationUnavailableError:
                    record = self._unavailable_advisory(kind, model_id)
                if not record.valid:
                    all_valid = False
                    incomplete.add("advisory_incomplete")
                records.append(record)
            if not all(item.valid for item in records):
                records.append(
                    preparation.EvidenceInput(
                        kind=EvidenceKind.INCOMPLETE,
                        model_id=None,
                        valid=False,
                        label=None,
                        summary="One or more independent advisory checks were incomplete.",
                        citations=(),
                        failure_code="advisory_incomplete",
                    )
                )
            evidence_by_candidate[candidate_id] = tuple(records)
        return evidence_by_candidate, all_valid, incomplete

    @staticmethod
    def _unavailable_advisory(kind: EvidenceKind, model_id: str) -> preparation.EvidenceInput:
        return preparation.EvidenceInput(
            kind=kind,
            model_id=model_id,
            valid=False,
            label=None,
            summary="The independent advisory check did not complete.",
            citations=(),
            failure_code="advisory_unavailable",
        )

    @staticmethod
    def _finding_record(
        kind: EvidenceKind,
        result: FindingResult,
        citation_sources: Mapping[str, CitationSource],
    ) -> preparation.EvidenceInput:
        if not result.valid or result.finding is None:
            return preparation.EvidenceInput(
                kind=kind,
                model_id=_model_identity(result.model_id, result.model_revision),
                valid=False,
                label=None,
                summary="Advisory output did not pass strict validation.",
                citations=(),
                failure_code="advisory_invalid",
            )
        citations: list[Mapping[str, Any]] = []
        for item in result.finding.evidence:
            source = citation_sources.get(item.chunk_reference)
            if source is None:
                raise WorkerExecutionError(
                    "advisory_citation_invalid",
                    "Validated advisory evidence lost its chunk correlation.",
                    retryable=False,
                )
            citations.append(source.public_citation(item.quote))
        return preparation.EvidenceInput(
            kind=kind,
            model_id=_model_identity(result.model_id, result.model_revision),
            valid=True,
            label=result.finding.label,
            summary=result.finding.summary,
            citations=tuple(citations),
            failure_code=None,
        )

    def _classification_excerpts(
        self,
        handle: preparation.PreparationHandle,
        *,
        candidate_document_id: uuid.UUID,
        candidate_revision_id: uuid.UUID | None,
    ) -> tuple[list[SourceExcerpt], list[SourceExcerpt], dict[str, CitationSource]]:
        with self.session_factory() as session:
            if candidate_revision_id is None:
                candidate_revision_id = session.scalar(
                    select(PreparedRevision.id)
                    .where(
                        PreparedRevision.document_id == candidate_document_id,
                        PreparedRevision.vector_complete.is_(True),
                    )
                    .order_by(PreparedRevision.revision_number.desc())
                )
            if candidate_revision_id is None:
                raise WorkerExecutionError(
                    "candidate_content_missing",
                    "A deterministic candidate has no retained prepared content.",
                    retryable=False,
                )
            incoming_rows = session.scalars(
                select(PreparedChunk)
                .where(PreparedChunk.prepared_revision_id == handle.revision_id)
                .order_by(PreparedChunk.chunk_index)
            ).all()
            candidate_rows = session.scalars(
                select(PreparedChunk)
                .where(PreparedChunk.prepared_revision_id == candidate_revision_id)
                .order_by(PreparedChunk.chunk_index)
            ).all()
            if not incoming_rows or not candidate_rows:
                raise WorkerExecutionError(
                    "candidate_content_missing",
                    "Advisory review requires retained chunks for both documents.",
                    retryable=False,
                )
            citations: dict[str, CitationSource] = {}

            def convert(
                rows: Sequence[PreparedChunk], document_id: uuid.UUID
            ) -> list[SourceExcerpt]:
                excerpts: list[SourceExcerpt] = []
                for row in rows:
                    reference = str(row.id)
                    citations[reference] = CitationSource(
                        document_id=document_id,
                        chunk_id=row.id,
                        page_start=row.page_start,
                        page_end=row.page_end,
                        text=row.markdown,
                    )
                    excerpts.append(
                        SourceExcerpt(
                            reference=reference,
                            pages=f"{row.page_start}-{row.page_end}",
                            text=row.markdown,
                        )
                    )
                return excerpts

            return (
                convert(incoming_rows, handle.document_id),
                convert(candidate_rows, candidate_document_id),
                citations,
            )

    def _finish_preflight(
        self, operation_id: uuid.UUID, sealed: preparation.SealedPreparation
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            revision = session.get(PreparedRevision, sealed.revision_id)
            if (
                operation is None
                or revision is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or operation.prepared_revision_id != revision.id
                or revision.status is not RevisionStatus.SEALED
            ):
                raise LeaseLostError()
            document = session.get(Document, operation.document_id)
            if document is None or document.state is not DocumentState.PREFLIGHTING:
                raise WorkerExecutionError(
                    "document_state_conflict",
                    "The sealed preflight document changed lifecycle state.",
                    retryable=False,
                )
            if sealed.clear_for_publication:
                queued = intake.queue_clear_publication(
                    session, document=document, revision=revision
                )
                terminal_phase = OperationPhase.COMPLETE
                event_type = "preflight_clear_publication_queued"
                queued_operation_id = str(queued.id)
            else:
                document.state = DocumentState.REVIEW_REQUIRED
                terminal_phase = OperationPhase.AWAITING_DECISION
                event_type = "preflight_review_required"
                queued_operation_id = None
            document.failure_code = None
            document.failure_message = None
            document.failure_retryable = False
            now = utc_now()
            operation.state = OperationState.SUCCEEDED
            intake.set_operation_phase(operation, terminal_phase, now=now)
            operation.retryable = False
            operation.completed_at = now
            operation.lease_expires_at = None
            operation.heartbeat_at = None
            operation.worker_id = None
            intake.audit(
                session,
                event_type=event_type,
                actor_type="system",
                actor_id=self.worker_id,
                document_id=document.id,
                operation_id=operation.id,
                details={
                    "prepared_revision_id": str(revision.id),
                    "manifest_sha256": sealed.manifest_sha256,
                    "queued_publication_operation_id": queued_operation_id,
                },
            )
        self.notify()

    # -- publication ------------------------------------------------------

    def _run_publish(self, operation_id: uuid.UUID, document_id: uuid.UUID) -> None:
        snapshot = self._operation_snapshot(operation_id)
        if (
            snapshot.operation_type is not OperationType.PUBLISH
            or snapshot.document_id != document_id
            or snapshot.prepared_revision_id is None
        ):
            raise WorkerExecutionError(
                "publication_binding_invalid",
                "Publication work lost its exact operation binding.",
                retryable=False,
            )
        if snapshot.replacement_target_document_id is not None:
            self._run_deletion_phases(
                operation_id,
                snapshot.replacement_target_document_id,
            )

        revision_id = snapshot.prepared_revision_id
        screening = self._required_screening_collection()
        with self.session_factory() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            revision = session.get(PreparedRevision, revision_id)
            publication = session.scalar(
                select(PublicationRecord).where(
                    PublicationRecord.prepared_revision_id == revision_id
                )
            )
            if (
                operation is None
                or document is None
                or revision is None
                or publication is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or document.state is not DocumentState.PUBLISHING
                or revision.document_id != document.id
                or revision.status is not RevisionStatus.SEALED
                or publication.document_id != document.id
                or publication.active_qdrant_collection != revision.active_qdrant_collection
                or publication.expected_points != revision.expected_point_count
                or publication.status is PublicationStatus.VERIFIED
            ):
                raise WorkerExecutionError(
                    "publication_binding_invalid",
                    "Publication work did not match its sealed revision checkpoint.",
                    retryable=False,
                )
            active_collection = publication.active_qdrant_collection
            expected_points = publication.expected_points

        points = list(
            preparation.reconstruct_chunk_points(
                self.session_factory,
                revision_id=revision_id,
                require_sealed=True,
            )
        )
        if len(points) != expected_points or any(
            point.document_id != document_id
            or point.prepared_revision_id != revision_id
            or point.active_qdrant_collection != active_collection
            for point in points
        ):
            raise WorkerExecutionError(
                "publication_manifest_invalid",
                "Reconstructed publication points did not match the sealed manifest.",
                retryable=False,
            )

        qdrant = self._required_qdrant()
        self._set_phase(operation_id, OperationPhase.UPSERT_ACTIVE_POINTS)
        active_outbox = self._begin_outbox(
            document_id=document_id,
            revision_id=revision_id,
            target=IndexTarget.ACTIVE,
            action=IndexAction.PUBLISH,
            collection=active_collection,
            expected_points=expected_points,
        )
        stage_active_points(
            qdrant,
            active_collection=active_collection,
            points=points,
        )
        self._set_phase(operation_id, OperationPhase.VERIFY_ACTIVE_POINTS)
        verify_prepared_points(
            qdrant,
            collection=active_collection,
            points=points,
            published=False,
            visibility="publishing",
        )
        activate_prepared_points(
            qdrant,
            collection=active_collection,
            document_id=document_id,
            prepared_revision_id=revision_id,
        )
        verify_prepared_points(
            qdrant,
            collection=active_collection,
            points=points,
            published=True,
            visibility="active",
        )
        self._complete_outbox(active_outbox)
        with self.session_factory.begin() as session:
            publication = session.scalar(
                select(PublicationRecord).where(
                    PublicationRecord.prepared_revision_id == revision_id
                )
            )
            operation = session.get(WorkOperation, operation_id)
            if (
                publication is None
                or operation is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or publication.status is PublicationStatus.VERIFIED
            ):
                raise LeaseLostError()
            publication.status = PublicationStatus.UPSERTED
            publication.failure_code = None

        self._set_phase(operation_id, OperationPhase.REMOVE_SCREENING_POINTS)
        screening_outbox = self._begin_outbox(
            document_id=document_id,
            revision_id=revision_id,
            target=IndexTarget.SCREENING,
            action=IndexAction.DELETE,
            collection=screening,
            expected_points=0,
        )
        delete_document_points(qdrant, collection=screening, document_id=document_id)
        self._set_phase(operation_id, OperationPhase.VERIFY_SCREENING_REMOVAL)
        verify_document_zero(qdrant, collection=screening, document_id=document_id)
        self._complete_outbox(screening_outbox)
        self._finish_publication(
            operation_id=operation_id,
            document_id=document_id,
            revision_id=revision_id,
            expected_points=expected_points,
        )

    def _finish_publication(
        self,
        *,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        expected_points: int,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            revision = session.get(PreparedRevision, revision_id)
            publication = session.scalar(
                select(PublicationRecord).where(
                    PublicationRecord.prepared_revision_id == revision_id
                )
            )
            if (
                operation is None
                or document is None
                or revision is None
                or publication is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or operation.document_id != document.id
                or operation.prepared_revision_id != revision.id
                or document.state is not DocumentState.PUBLISHING
                or revision.status is not RevisionStatus.SEALED
                or publication.status is not PublicationStatus.UPSERTED
                or publication.expected_points != expected_points
            ):
                raise WorkerExecutionError(
                    "publication_binding_invalid",
                    "Verified index work lost its publication checkpoint.",
                    retryable=False,
                )
            if operation.replacement_target_document_id is not None:
                old = session.get(Document, operation.replacement_target_document_id)
                if (
                    old is None
                    or old.state is not DocumentState.DELETED
                    or old.tombstone is None
                    or old.deletion_progress is None
                    or old.deletion_progress.tombstoned_at is None
                ):
                    raise WorkerExecutionError(
                        "replacement_deletion_incomplete",
                        "Replacement publication requires the old tombstone first.",
                        retryable=False,
                    )
            now = utc_now()
            publication.status = PublicationStatus.VERIFIED
            publication.verified_points = expected_points
            publication.payload_revision_verified = True
            publication.vector_schema_verified = True
            publication.screening_zero_verified = True
            publication.failure_code = None
            publication.verified_at = now
            document.state = DocumentState.READY
            document.ready_at = now
            document.failure_code = None
            document.failure_message = None
            document.failure_retryable = False
            operation.state = OperationState.SUCCEEDED
            intake.set_operation_phase(operation, OperationPhase.COMPLETE, now=now)
            operation.retryable = False
            operation.completed_at = now
            operation.lease_expires_at = None
            operation.heartbeat_at = None
            operation.worker_id = None
            intake.audit(
                session,
                event_type="publication_verified",
                actor_type="system",
                actor_id=self.worker_id,
                document_id=document.id,
                operation_id=operation.id,
                details={
                    "prepared_revision_id": str(revision.id),
                    "active_qdrant_collection": publication.active_qdrant_collection,
                    "verified_points": expected_points,
                },
            )

    # -- verified deletion ------------------------------------------------

    def _run_delete(self, operation_id: uuid.UUID, document_id: uuid.UUID) -> None:
        snapshot = self._operation_snapshot(operation_id)
        if (
            snapshot.operation_type is not OperationType.DELETE
            or snapshot.document_id != document_id
            or snapshot.replacement_target_document_id is not None
        ):
            raise WorkerExecutionError(
                "deletion_binding_invalid",
                "Deletion work lost its exact operation binding.",
                retryable=False,
            )
        self._run_deletion_phases(operation_id, document_id)

    def _deletion_checkpoint(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> tuple[uuid.UUID | None, str, str]:
        with self.session_factory() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if (
                operation is None
                or document is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or (
                    operation.document_id != document_id
                    and operation.replacement_target_document_id != document_id
                )
                or document.deletion_progress is None
                or document.terminal_disposition
                is not document.deletion_progress.terminal_disposition
            ):
                raise WorkerExecutionError(
                    "deletion_binding_invalid",
                    "Deletion work did not match its durable checkpoint.",
                    retryable=False,
                )
            progress = document.deletion_progress
            return (
                progress.prepared_revision_id,
                progress.active_qdrant_collection,
                progress.screening_qdrant_collection,
            )

    def _set_deletion_phase(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        phase: DeletionPhase,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if (
                operation is None
                or document is None
                or document.deletion_progress is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or (
                    operation.document_id != document_id
                    and operation.replacement_target_document_id != document_id
                )
            ):
                raise LeaseLostError()
            document.deletion_progress.phase = phase
            intake.set_operation_phase(operation, OperationPhase(phase.value))

    def _run_deletion_phases(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None:
        revision_id, active, screening = self._deletion_checkpoint(operation_id, document_id)
        with self.session_factory.begin() as session:
            document = session.get(Document, document_id)
            if document is None or document.deletion_progress is None:
                raise WorkerExecutionError(
                    "deletion_binding_invalid",
                    "Deletion progress was missing.",
                    retryable=False,
                )
            document.deletion_progress.attempts += 1

        qdrant = self._required_qdrant()
        with self.session_factory() as session:
            progress = session.get(Document, document_id).deletion_progress  # type: ignore[union-attr]
            active_verified = progress.active_zero_verified_at is not None  # type: ignore[union-attr]
            screening_verified = progress.screening_zero_verified_at is not None  # type: ignore[union-attr]
            storage_purged = progress.storage_purged_at is not None  # type: ignore[union-attr]
            tombstoned = progress.tombstoned_at is not None  # type: ignore[union-attr]
            purge_was_checkpointed = (  # type: ignore[union-attr]
                progress.phase is DeletionPhase.PURGE_STORAGE
            )

        if not active_verified:
            self._set_deletion_phase(operation_id, document_id, DeletionPhase.DELETE_ACTIVE_POINTS)
            outbox_id = self._deletion_outbox(
                document_id=document_id,
                revision_id=revision_id,
                target=IndexTarget.ACTIVE,
                collection=active,
            )
            delete_document_points(qdrant, collection=active, document_id=document_id)
            if outbox_id is not None:
                self._complete_outbox(outbox_id)
            self._set_deletion_phase(operation_id, document_id, DeletionPhase.VERIFY_ACTIVE_ZERO)
            verify_document_zero(qdrant, collection=active, document_id=document_id)
            self._record_zero_verification(
                operation_id,
                document_id,
                active=True,
            )

        if not screening_verified:
            self._set_deletion_phase(
                operation_id, document_id, DeletionPhase.DELETE_SCREENING_POINTS
            )
            outbox_id = self._deletion_outbox(
                document_id=document_id,
                revision_id=revision_id,
                target=IndexTarget.SCREENING,
                collection=screening,
            )
            delete_document_points(qdrant, collection=screening, document_id=document_id)
            if outbox_id is not None:
                self._complete_outbox(outbox_id)
            self._set_deletion_phase(operation_id, document_id, DeletionPhase.VERIFY_SCREENING_ZERO)
            verify_document_zero(qdrant, collection=screening, document_id=document_id)
            self._record_zero_verification(
                operation_id,
                document_id,
                active=False,
            )

        if not storage_purged:
            self._set_deletion_phase(operation_id, document_id, DeletionPhase.PURGE_STORAGE)
            self._purge_document_content(
                operation_id,
                document_id,
                allow_missing=purge_was_checkpointed,
            )

        if not tombstoned:
            self._set_deletion_phase(operation_id, document_id, DeletionPhase.COMMIT_TOMBSTONE)
            self._commit_deletion(operation_id, document_id)

    def _deletion_outbox(
        self,
        *,
        document_id: uuid.UUID,
        revision_id: uuid.UUID | None,
        target: IndexTarget,
        collection: str,
    ) -> int | None:
        if revision_id is None:
            return None
        return self._begin_outbox(
            document_id=document_id,
            revision_id=revision_id,
            target=target,
            action=IndexAction.DELETE,
            collection=collection,
            expected_points=0,
        )

    def _record_zero_verification(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        active: bool,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if (
                operation is None
                or document is None
                or document.deletion_progress is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
            ):
                raise LeaseLostError()
            progress = document.deletion_progress
            now = utc_now()
            if active:
                progress.active_zero_verified_at = now
                progress.phase = DeletionPhase.DELETE_SCREENING_POINTS
                intake.set_operation_phase(
                    operation, OperationPhase.DELETE_SCREENING_POINTS, now=now
                )
            else:
                if progress.active_zero_verified_at is None:
                    raise WorkerExecutionError(
                        "deletion_checkpoint_invalid",
                        "Screening zero cannot precede active zero.",
                        retryable=False,
                    )
                progress.screening_zero_verified_at = now
                progress.phase = DeletionPhase.VERIFY_SCREENING_ZERO
                intake.set_operation_phase(
                    operation, OperationPhase.VERIFY_SCREENING_ZERO, now=now
                )

    def _purge_document_content(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        allow_missing: bool,
    ) -> None:
        with self.session_factory() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if (
                operation is None
                or document is None
                or document.deletion_progress is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or document.deletion_progress.active_zero_verified_at is None
                or document.deletion_progress.screening_zero_verified_at is None
            ):
                raise WorkerExecutionError(
                    "deletion_checkpoint_invalid",
                    "Storage purge requires both exact zero proofs.",
                    retryable=False,
                )
            storage_key = document.storage_key
            revision_ids = tuple(
                session.scalars(
                    select(PreparedRevision.id).where(PreparedRevision.document_id == document_id)
                ).all()
            )
            artifact_keys = (
                tuple(
                    session.scalars(
                        select(RevisionArtifact.storage_key).where(
                            RevisionArtifact.prepared_revision_id.in_(revision_ids)
                        )
                    ).all()
                )
                if revision_ids
                else ()
            )

        layout = StorageLayout.from_root(self.settings.storage_root)
        if storage_key is None:
            raise WorkerExecutionError(
                "content_binding_invalid",
                "The deletion checkpoint had no canonical source binding.",
                retryable=False,
            )
        try:
            remove_storage_key(layout, storage_key, missing_ok=allow_missing)
            for artifact_key in artifact_keys:
                remove_storage_key(layout, artifact_key, missing_ok=allow_missing)
        except FileNotFoundError as exc:
            raise WorkerExecutionError(
                "content_missing_before_purge",
                "Expected protected content was absent before the first purge attempt.",
                retryable=False,
            ) from exc
        artifact_root = resolve_storage_key(layout, f"artifacts/{document_id}")
        if artifact_root.exists():
            if not artifact_root.is_dir():
                raise WorkerExecutionError(
                    "artifact_purge_failed",
                    "The protected artifact root was not a directory.",
                    retryable=False,
                )
            shutil.rmtree(artifact_root)
        if artifact_root.exists():
            raise WorkerExecutionError(
                "artifact_purge_failed",
                "Protected artifacts remained after purge.",
            )

        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if (
                operation is None
                or document is None
                or document.deletion_progress is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or document.storage_key != storage_key
                or document.deletion_progress.active_zero_verified_at is None
                or document.deletion_progress.screening_zero_verified_at is None
            ):
                raise LeaseLostError()
            if revision_ids:
                for model in (
                    CandidateEvidence,
                    PreparedCandidate,
                    PreparedChunkVector,
                    PreparedChunk,
                    PreparedPage,
                    FormatterBatch,
                    ExtractedPage,
                    RevisionArtifact,
                ):
                    session.execute(
                        delete(model).where(model.prepared_revision_id.in_(revision_ids))
                    )
            document.storage_key = None
            progress = document.deletion_progress
            now = utc_now()
            progress.storage_purged_at = now
            progress.phase = DeletionPhase.COMMIT_TOMBSTONE
            intake.set_operation_phase(operation, OperationPhase.COMMIT_TOMBSTONE, now=now)

    def _commit_deletion(
        self,
        operation_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None:
        with self.session_factory.begin() as session:
            operation = session.get(WorkOperation, operation_id)
            document = session.get(Document, document_id)
            if (
                operation is None
                or document is None
                or document.deletion_progress is None
                or operation.state is not OperationState.RUNNING
                or operation.worker_id != self.worker_id
                or document.deletion_progress.storage_purged_at is None
            ):
                raise LeaseLostError()
            reason = (
                document.failure_code
                if document.terminal_disposition is TerminalDisposition.REJECTED
                else None
            )
            tombstone = intake.commit_tombstone(
                session,
                document=document,
                reason_code=reason,
                actor_type="system",
                actor_id=self.worker_id,
            )
            intake.audit(
                session,
                event_type="deletion_verified_complete",
                actor_type="system",
                actor_id=self.worker_id,
                document_id=document.id,
                operation_id=operation.id,
                details={
                    "tombstone_id": str(tombstone.id),
                    "disposition": tombstone.disposition.value,
                },
            )
            if (
                operation.operation_type is OperationType.DELETE
                and operation.document_id == document_id
            ):
                now = utc_now()
                operation.state = OperationState.SUCCEEDED
                intake.set_operation_phase(operation, OperationPhase.COMPLETE, now=now)
                operation.retryable = False
                operation.completed_at = now
                operation.lease_expires_at = None
                operation.heartbeat_at = None
                operation.worker_id = None
