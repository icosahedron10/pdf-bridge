"""Staged, immutable target preparation orchestration.

Database transactions are deliberately limited to catalog reads and artifact
writes. PDF parsing, language detection, vLLM calls, MPNet tokenization, and
both embedding calls run between those transactions. Publication reconstructs
``ChunkPoint`` values solely from sealed persisted artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.persistence.models import (
    CandidateEvidence,
    CandidateSource,
    Document,
    DocumentState,
    EvidenceKind,
    FormatterBatch,
    FormatterBatchStatus,
    OperationPhase,
    PreparedCandidate,
    PreparedChunk,
    PreparedChunkVector,
    PreparedPage,
    PreparedRevision,
    RevisionStatus,
    utc_now,
)
from pdf_bridge.persistence.models import (
    ExtractedPage as ExtractedPageRow,
)
from pdf_bridge.services.extraction import (
    EXTRACTION_PROFILE,
    EnglishDetector,
    ExtractedDocument,
    ExtractionInfrastructureError,
    ExtractionLimits,
    ExtractionRejectedError,
    extract_pdf_layout,
    validate_native_english,
)
from pdf_bridge.services.local_embeddings import (
    DENSE_DIMENSION,
    LocalModelError,
    SparseVector,
)
from pdf_bridge.services.markdown_chunking import (
    HARD_MAX_TOKENS,
    MarkdownChunk,
    MarkdownChunkingError,
    MarkdownPage,
    canonical_markdown,
    chunk_markdown,
)
from pdf_bridge.services.markdown_formatter import (
    MARKDOWN_FORMATTER_VERSION,
    FormattedDocument,
    FormatterClient,
    FormatterConfig,
    FormatterProgress,
    LayoutPage,
    MarkdownFormattingError,
    fidelity_projection,
    format_markdown_document,
)
from pdf_bridge.services.profiles import PipelineProfiles
from pdf_bridge.services.vector_index import ChunkPoint

PREPARATION_MANIFEST_VERSION = 1
_PROFILE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ERROR_CHARACTERS = 500

type PreparationProgressCallback = Callable[[OperationPhase], None]


class _ProgressCallbackFailed(Exception):
    """Keep lease/checkpoint failures outside preparation failure handling."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _report_progress(
    callback: PreparationProgressCallback | None,
    phase: OperationPhase,
) -> None:
    if callback is not None:
        callback(phase)


class EmbeddingProvider(Protocol):
    """Exact local model surface required by target preparation."""

    def count_tokens(self, text: str) -> int: ...

    def embed_dense(self, texts: list[str]) -> list[tuple[float, ...]]: ...

    def embed_sparse_documents(self, texts: list[str]) -> list[SparseVector]: ...


@dataclass(frozen=True, slots=True)
class PreparationIdentity:
    """Resolved immutable target and model/profile identities for one revision."""

    active_qdrant_collection: str
    profiles: PipelineProfiles
    formatter_model_id: str
    formatter_tokenizer_class: str
    dense_model_id: str
    sparse_model_id: str


@dataclass(frozen=True, slots=True)
class PreparationHandle:
    """Content-free identity returned after the initial short transaction."""

    revision_id: uuid.UUID
    document_id: uuid.UUID
    revision_number: int
    collection_key: str
    identity: PreparationIdentity


@dataclass(frozen=True, slots=True)
class PreparedVector:
    """One correlated chunk and its validated local document vectors."""

    chunk: MarkdownChunk
    dense: tuple[float, ...]
    sparse: SparseVector
    dense_sha256: str
    sparse_sha256: str


@dataclass(frozen=True, slots=True)
class ChunkVectorBundle:
    """External chunk/tokenizer/embedding result ready for persistence."""

    canonical_markdown_sha256: str
    vectors: tuple[PreparedVector, ...]
    vector_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ContentPreparationResult:
    """Persisted pre-candidate content and reconstructed screening points."""

    handle: PreparationHandle
    extraction_sha256: str
    markdown_sha256: str
    vector_manifest_sha256: str
    points: tuple[ChunkPoint, ...]


@dataclass(frozen=True, slots=True)
class PreflightCompleteness:
    """Candidate/advisory completion supplied by the separate preflight stage."""

    candidate_discovery_complete: bool
    advisory_complete: bool
    clear_for_publication: bool
    incomplete_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    """One public, validated evidence record; raw provider material is excluded."""

    kind: EvidenceKind
    model_id: str | None
    valid: bool
    label: str | None
    summary: str | None
    citations: tuple[Mapping[str, Any], ...] = ()
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """One ordered deterministic candidate ready for a short catalog write."""

    matched_document_id: uuid.UUID
    source: CandidateSource
    reasons: tuple[str, ...]
    max_cosine: float
    bm25_score: float
    fused_score: float
    matched_chunk_pairs: tuple[tuple[int, uuid.UUID], ...]
    evidence: tuple[EvidenceInput, ...]


@dataclass(frozen=True, slots=True)
class SealedPreparation:
    """Content-free proof returned by the atomic sealing transaction."""

    revision_id: uuid.UUID
    manifest_sha256: str
    extraction_sha256: str
    markdown_sha256: str
    vector_manifest_sha256: str
    evidence_manifest_sha256: str
    page_count: int
    chunk_count: int
    clear_for_publication: bool


class PreparationError(RuntimeError):
    """Sanitized failure at one explicit preparation stage."""

    def __init__(self, code: str, message: str) -> None:
        safe_code = re.sub(r"[^a-z0-9_-]", "_", code.casefold())[:100] or "preparation_failed"
        safe_message = _sanitize(message)
        super().__init__(safe_message)
        self.code = safe_code


def _sanitize(message: str) -> str:
    clean = " ".join(str(message).split())
    clean = "".join(character if ord(character) >= 32 else "?" for character in clean)
    return clean[:_MAX_ERROR_CHARACTERS] or "preparation failed"


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PreparationError(
            "invalid_manifest_value",
            "preparation data could not be represented canonically",
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    try:
        return _sha256_bytes(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PreparationError(
            "invalid_unicode",
            "preparation data contained invalid Unicode",
        ) from exc


def _manifest_hash(value: object) -> str:
    return _sha256_text(_canonical_json(value))


def _projection_hash(tokens: tuple[str, ...]) -> str:
    return _sha256_text(json.dumps(tokens, ensure_ascii=False, separators=(",", ":")))


def _require_hash(value: str | None, field: str) -> str:
    if value is None or not _SHA256.fullmatch(value):
        raise PreparationError("correlation_failed", f"{field} was missing or invalid")
    return value


def _finite_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PreparationError("correlation_failed", f"{field} contained a non-number")
    result = float(value)
    if not math.isfinite(result):
        raise PreparationError("correlation_failed", f"{field} contained a non-finite number")
    return 0.0 if result == 0.0 else result


def _validate_profile_identity(profile_id: str, canonical_json: str, kind: str) -> dict[str, Any]:
    if not _PROFILE_ID.fullmatch(profile_id):
        raise PreparationError("profile_mismatch", f"{kind} profile ID was invalid")
    if profile_id != f"sha256:{_sha256_text(canonical_json)}":
        raise PreparationError("profile_mismatch", f"{kind} profile hash did not match")
    try:
        payload = json.loads(canonical_json)
    except ValueError as exc:
        raise PreparationError("profile_mismatch", f"{kind} profile JSON was invalid") from exc
    if not isinstance(payload, dict) or payload.get("profile_kind") != kind:
        raise PreparationError("profile_mismatch", f"{kind} profile kind did not match")
    return payload


def validate_preparation_identity(identity: PreparationIdentity) -> None:
    """Validate all profile chains, resolved target, and persisted model labels."""

    if (
        not identity.active_qdrant_collection
        or len(identity.active_qdrant_collection) > 255
        or any(ord(character) < 33 for character in identity.active_qdrant_collection)
    ):
        raise PreparationError("invalid_target", "active Qdrant collection was invalid")
    for field, model_id in (
        ("formatter", identity.formatter_model_id),
        ("dense", identity.dense_model_id),
        ("sparse", identity.sparse_model_id),
    ):
        if not model_id.strip() or len(model_id) > 255:
            raise PreparationError("invalid_model_identity", f"{field} model identity was invalid")
    if (
        not identity.formatter_tokenizer_class
        or len(identity.formatter_tokenizer_class) > 255
        or any(ord(character) < 33 for character in identity.formatter_tokenizer_class)
    ):
        raise PreparationError(
            "invalid_model_identity", "formatter tokenizer identity was invalid"
        )

    content = _validate_profile_identity(
        identity.profiles.content.profile_id,
        identity.profiles.content.canonical_json,
        "content",
    )
    index = _validate_profile_identity(
        identity.profiles.index.profile_id,
        identity.profiles.index.canonical_json,
        "index",
    )
    policy = _validate_profile_identity(
        identity.profiles.preflight_policy.profile_id,
        identity.profiles.preflight_policy.canonical_json,
        "preflight_policy",
    )
    content_inputs = content.get("inputs")
    if (
        not isinstance(content_inputs, dict)
        or content_inputs.get("formatter_tokenizer_class")
        != identity.formatter_tokenizer_class
    ):
        raise PreparationError(
            "profile_mismatch", "content profile did not bind the formatter tokenizer"
        )
    if content.get("profile_schema_version") != index.get("profile_schema_version"):
        raise PreparationError("profile_mismatch", "content and index profile schemas differed")
    if index.get("content_profile_id") != identity.profiles.content.profile_id:
        raise PreparationError("profile_mismatch", "index profile did not bind the content profile")
    if index.get("active_qdrant_collection") != identity.active_qdrant_collection:
        raise PreparationError("profile_mismatch", "index profile did not bind the active target")
    if policy.get("index_profile_id") != identity.profiles.index.profile_id:
        raise PreparationError("profile_mismatch", "policy profile did not bind the index profile")


def _load_revision(
    session: Session,
    revision_id: uuid.UUID,
    *,
    required_status: RevisionStatus | None = RevisionStatus.PREPARING,
    for_update: bool = False,
) -> PreparedRevision:
    statement = select(PreparedRevision).where(PreparedRevision.id == revision_id)
    if for_update:
        statement = statement.with_for_update()
    revision = session.scalar(statement)
    if revision is None:
        raise PreparationError("revision_not_found", "prepared revision was not found")
    if required_status is not None and revision.status is not required_status:
        raise PreparationError(
            "revision_state_conflict",
            f"prepared revision was not {required_status.value}",
        )
    return revision


def _validate_revision_identity(
    revision: PreparedRevision,
    handle: PreparationHandle,
) -> None:
    identity = handle.identity
    if (
        revision.id != handle.revision_id
        or revision.document_id != handle.document_id
        or revision.revision_number != handle.revision_number
        or revision.active_qdrant_collection != identity.active_qdrant_collection
        or revision.content_profile_id != identity.profiles.content.profile_id
        or revision.index_profile_id != identity.profiles.index.profile_id
        or revision.preflight_policy_id != identity.profiles.preflight_policy.profile_id
        or revision.formatter_model_id != identity.formatter_model_id
        or revision.dense_model_id != identity.dense_model_id
        or revision.dense_dimension != DENSE_DIMENSION
        or revision.sparse_model_id != identity.sparse_model_id
    ):
        raise PreparationError(
            "revision_identity_mismatch",
            "prepared revision identity no longer matched the requested target",
        )


def begin_preparation(
    session_factory: sessionmaker[Session],
    *,
    document_id: uuid.UUID,
    identity: PreparationIdentity,
) -> PreparationHandle:
    """Create the next PREPARING revision in one short transaction."""

    validate_preparation_identity(identity)
    try:
        with session_factory.begin() as session:
            document = session.scalar(
                select(Document).where(Document.id == document_id).with_for_update()
            )
            if document is None:
                raise PreparationError("document_not_found", "document was not found")
            if document.state is not DocumentState.PREFLIGHTING:
                raise PreparationError(
                    "document_state_conflict",
                    "document was not in PREFLIGHTING state",
                )
            latest = session.scalar(
                select(func.max(PreparedRevision.revision_number)).where(
                    PreparedRevision.document_id == document_id
                )
            )
            revision_number = int(latest or 0) + 1
            revision = PreparedRevision(
                document_id=document.id,
                revision_number=revision_number,
                status=RevisionStatus.PREPARING,
                active_qdrant_collection=identity.active_qdrant_collection,
                content_profile_id=identity.profiles.content.profile_id,
                index_profile_id=identity.profiles.index.profile_id,
                preflight_policy_id=identity.profiles.preflight_policy.profile_id,
                formatter_model_id=identity.formatter_model_id,
                dense_model_id=identity.dense_model_id,
                dense_dimension=DENSE_DIMENSION,
                sparse_model_id=identity.sparse_model_id,
            )
            session.add(revision)
            session.flush()
            handle = PreparationHandle(
                revision_id=revision.id,
                document_id=document.id,
                revision_number=revision_number,
                collection_key=document.collection_key,
                identity=identity,
            )
    except PreparationError:
        raise
    except SQLAlchemyError as exc:
        raise PreparationError(
            "begin_preparation_failed",
            "the preparing revision could not be created",
        ) from exc
    return handle


def extraction_manifest_sha256(document: ExtractedDocument) -> str:
    """Hash the exact ordered normalized extraction artifact."""

    return _manifest_hash(
        {
            "extraction_profile": EXTRACTION_PROFILE,
            "page_count": document.page_count,
            "character_count": document.character_count,
            "pages": [
                {
                    "page_number": page.page_number,
                    "layout_text": page.layout_text,
                    "character_count": page.character_count,
                    "text_sha256": page.text_sha256,
                }
                for page in document.pages
            ],
        }
    )


def _validate_extraction(document: ExtractedDocument) -> None:
    if document.page_count <= 0 or document.page_count != len(document.pages):
        raise PreparationError("extraction_correlation", "extraction page coverage was invalid")
    character_count = 0
    for expected, page in enumerate(document.pages, start=1):
        if page.page_number != expected:
            raise PreparationError(
                "extraction_correlation", "extraction pages were missing or reordered"
            )
        if page.character_count != len(page.layout_text):
            raise PreparationError(
                "extraction_correlation", "extraction character count did not match"
            )
        if page.text_sha256 != _sha256_text(page.layout_text):
            raise PreparationError("extraction_correlation", "extraction page hash did not match")
        character_count += page.character_count
    if character_count != document.character_count:
        raise PreparationError(
            "extraction_correlation", "extraction document character count did not match"
        )


def record_extraction(
    session_factory: sessionmaker[Session],
    *,
    handle: PreparationHandle,
    document: ExtractedDocument,
    language_code: str = "en",
) -> str:
    """Persist every exact extracted page after external eligibility checks."""

    _validate_extraction(document)
    digest = extraction_manifest_sha256(document)
    try:
        with session_factory.begin() as session:
            revision = _load_revision(session, handle.revision_id, for_update=True)
            _validate_revision_identity(revision, handle)
            existing = session.scalar(
                select(func.count(ExtractedPageRow.id)).where(
                    ExtractedPageRow.prepared_revision_id == revision.id
                )
            )
            if existing:
                raise PreparationError(
                    "stage_already_recorded", "extraction artifacts were already recorded"
                )
            session.add_all(
                [
                    ExtractedPageRow(
                        prepared_revision_id=revision.id,
                        page_number=page.page_number,
                        layout_text=page.layout_text,
                        character_count=page.character_count,
                        text_sha256=page.text_sha256,
                    )
                    for page in document.pages
                ]
            )
            revision.language_code = language_code
            revision.native_text_eligible = True
            revision.page_count = document.page_count
            revision.extraction_sha256 = digest
            session.flush()
    except PreparationError:
        raise
    except SQLAlchemyError as exc:
        raise PreparationError(
            "record_extraction_failed",
            "extraction artifacts could not be recorded",
        ) from exc
    return digest


def _formatted_slice_records(
    formatted: FormattedDocument,
    extracted_by_page: Mapping[int, ExtractedPageRow],
) -> tuple[list[dict[str, Any]], dict[tuple[int, int], dict[str, Any]]]:
    page_records: list[dict[str, Any]] = []
    slices_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for expected_page, page in enumerate(formatted.pages, start=1):
        extracted = extracted_by_page.get(expected_page)
        if page.page_number != expected_page or extracted is None:
            raise PreparationError(
                "formatter_correlation", "formatted pages were missing or reordered"
            )
        if page.source_text != extracted.layout_text:
            raise PreparationError(
                "formatter_correlation", "formatter source did not match extracted layout text"
            )
        if page.source_text_sha256 != extracted.text_sha256:
            raise PreparationError(
                "formatter_correlation", "formatter page source hash did not match extraction"
            )
        if not page.markdown.strip():
            raise PreparationError("formatter_correlation", "formatted page Markdown was empty")

        source_cursor = 0
        markdown_cursor = 0
        persisted_slices: list[dict[str, Any]] = []
        for expected_slice, source_slice in enumerate(page.slices):
            if (
                source_slice.page_number != expected_page
                or source_slice.slice_index != expected_slice
            ):
                raise PreparationError(
                    "formatter_correlation", "formatted slices were missing or reordered"
                )
            source_start = source_cursor
            source_end = source_start + len(source_slice.source_text)
            if page.source_text[source_start:source_end] != source_slice.source_text:
                raise PreparationError(
                    "formatter_correlation", "formatter slices did not exactly cover source text"
                )
            source_cursor = source_end

            if expected_slice:
                markdown_cursor += 2
            markdown_start = markdown_cursor
            markdown_end = markdown_start + len(source_slice.markdown)
            if page.markdown[markdown_start:markdown_end] != source_slice.markdown:
                raise PreparationError(
                    "formatter_correlation", "formatter slices did not exactly cover page Markdown"
                )
            markdown_cursor = markdown_end

            source_projection = _projection_hash(fidelity_projection(source_slice.source_text))
            markdown_projection = _projection_hash(
                fidelity_projection(source_slice.markdown, markdown=True)
            )
            if (
                source_slice.source_text_sha256 != _sha256_text(source_slice.source_text)
                or source_slice.source_projection_sha256 != source_projection
                or source_slice.markdown_projection_sha256 != markdown_projection
                or source_projection != markdown_projection
            ):
                raise PreparationError(
                    "formatter_correlation", "formatter slice hashes or fidelity did not match"
                )
            record = {
                "slice_index": expected_slice,
                "source_start": source_start,
                "source_end": source_end,
                "markdown_start": markdown_start,
                "markdown_end": markdown_end,
                "source_text_sha256": source_slice.source_text_sha256,
                "markdown_sha256": _sha256_text(source_slice.markdown),
                "source_projection_sha256": source_projection,
                "markdown_projection_sha256": markdown_projection,
            }
            persisted_slices.append(record)
            slices_by_key[(expected_page, expected_slice)] = record
        if source_cursor != len(page.source_text) or markdown_cursor != len(page.markdown):
            raise PreparationError(
                "formatter_correlation", "formatter slices did not provide exact page coverage"
            )

        source_projection = _projection_hash(fidelity_projection(page.source_text))
        markdown_projection = _projection_hash(fidelity_projection(page.markdown, markdown=True))
        if source_projection != markdown_projection:
            raise PreparationError(
                "formatter_correlation", "formatted page fidelity projection did not match"
            )
        page_records.append(
            {
                "page_number": expected_page,
                "slices": persisted_slices,
                "markdown": page.markdown,
                "markdown_sha256": _sha256_text(page.markdown),
                "source_projection_sha256": source_projection,
                "markdown_projection_sha256": markdown_projection,
            }
        )
    if len(page_records) != len(extracted_by_page):
        raise PreparationError(
            "formatter_correlation", "formatter did not cover every extracted page"
        )
    return page_records, slices_by_key


def _formatter_batch_records(
    formatted: FormattedDocument,
    config: FormatterConfig,
    slices_by_key: Mapping[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    keys_by_batch: dict[int, list[tuple[int, int]]] = {}
    for attempt in formatted.attempts:
        if attempt.valid:
            keys = [
                (projection.page_number, projection.slice_index)
                for projection in attempt.projections
            ]
            if not keys or len(keys) != len(set(keys)):
                raise PreparationError(
                    "formatter_correlation", "valid formatter attempt coverage was invalid"
                )
            existing = keys_by_batch.setdefault(attempt.batch_index, keys)
            if existing != keys:
                raise PreparationError(
                    "formatter_correlation", "formatter batch coverage changed across attempts"
                )
    if not keys_by_batch:
        raise PreparationError("formatter_correlation", "formatter returned no valid batches")

    records: list[dict[str, Any]] = []
    seen_attempts: set[tuple[int, int]] = set()
    for attempt in formatted.attempts:
        key = (attempt.batch_index, attempt.attempt_number)
        if key in seen_attempts or attempt.batch_index not in keys_by_batch:
            raise PreparationError(
                "formatter_correlation", "formatter attempts were duplicated or incomplete"
            )
        seen_attempts.add(key)
        attempt_projections = {
            (projection.page_number, projection.slice_index): projection
            for projection in attempt.projections
        }
        if len(attempt_projections) != len(attempt.projections):
            raise PreparationError(
                "formatter_correlation", "formatter attempt projections were duplicated"
            )
        page_slices: list[dict[str, Any]] = []
        source_slices: list[dict[str, Any]] = []
        for page_number, slice_index in keys_by_batch[attempt.batch_index]:
            persisted = slices_by_key.get((page_number, slice_index))
            if persisted is None:
                raise PreparationError(
                    "formatter_correlation", "formatter attempt referenced an unknown slice"
                )
            projection = attempt_projections.get((page_number, slice_index))
            if (
                projection is not None
                and projection.source_projection_sha256 != persisted["source_projection_sha256"]
            ):
                raise PreparationError(
                    "formatter_correlation", "formatter attempt source projection did not match"
                )
            source_item = {
                "page_number": page_number,
                "slice_index": slice_index,
                "source_text_sha256": persisted["source_text_sha256"],
                "source_projection_sha256": persisted["source_projection_sha256"],
            }
            source_slices.append(source_item)
            page_slices.append(
                {
                    **source_item,
                    "markdown_projection_sha256": (
                        projection.markdown_projection_sha256 if projection is not None else None
                    ),
                }
            )
        if attempt.valid and (
            set(attempt_projections) != set(keys_by_batch[attempt.batch_index])
            or any(
                item["markdown_projection_sha256"]
                != slices_by_key[(item["page_number"], item["slice_index"])][
                    "markdown_projection_sha256"
                ]
                for item in page_slices
            )
        ):
            raise PreparationError(
                "formatter_correlation", "valid formatter attempt projections did not match"
            )
        source_manifest = _manifest_hash(source_slices)
        request_manifest = {
            "formatter_version": MARKDOWN_FORMATTER_VERSION,
            "model_id": config.model_id,
            "tokenizer_class": config.expected_tokenizer_class,
            "temperature": 0,
            "n": 1,
            "max_output_tokens": config.max_output_tokens,
            "page_slices": source_slices,
        }
        diagnostic = attempt.diagnostic
        if diagnostic is not None:
            diagnostic = _sanitize(diagnostic)
        records.append(
            {
                "batch_index": attempt.batch_index,
                "attempt": attempt.attempt_number,
                "status": (
                    FormatterBatchStatus.VALID if attempt.valid else FormatterBatchStatus.FAILED
                ),
                "page_slices": page_slices,
                "source_manifest_sha256": source_manifest,
                "request_sha256": _manifest_hash(request_manifest),
                "response_sha256": attempt.response_sha256,
                "finish_reason": "stop" if attempt.valid else None,
                "validation_errors": [] if diagnostic is None else [diagnostic],
            }
        )
    if set(keys_by_batch) != set(range(len(keys_by_batch))):
        raise PreparationError(
            "formatter_correlation", "formatter batch indexes were not consecutive"
        )
    return records


def record_formatting(
    session_factory: sessionmaker[Session],
    *,
    handle: PreparationHandle,
    formatted: FormattedDocument,
    formatter_config: FormatterConfig,
) -> str:
    """Map hash-only attempts and validated page/slice Markdown into rows."""

    if (
        formatter_config.model_id != handle.identity.formatter_model_id
        or formatter_config.expected_tokenizer_class
        != handle.identity.formatter_tokenizer_class
    ):
        raise PreparationError(
            "formatter_identity_mismatch",
            "formatter model or tokenizer did not match the revision",
        )
    if formatted.formatter_version != MARKDOWN_FORMATTER_VERSION:
        raise PreparationError(
            "formatter_identity_mismatch", "formatter version did not match the target profile"
        )
    if formatted.tokenizer_class != handle.identity.formatter_tokenizer_class:
        raise PreparationError(
            "formatter_identity_mismatch",
            "formatter response tokenizer did not match the target profile",
        )
    try:
        with session_factory.begin() as session:
            revision = _load_revision(session, handle.revision_id, for_update=True)
            _validate_revision_identity(revision, handle)
            extracted = session.scalars(
                select(ExtractedPageRow)
                .where(ExtractedPageRow.prepared_revision_id == revision.id)
                .order_by(ExtractedPageRow.page_number)
            ).all()
            if not extracted or len(extracted) != revision.page_count:
                raise PreparationError(
                    "stage_order", "complete extraction must be recorded before formatting"
                )
            existing = session.scalar(
                select(func.count(PreparedPage.id)).where(
                    PreparedPage.prepared_revision_id == revision.id
                )
            )
            if existing:
                raise PreparationError(
                    "stage_already_recorded", "formatter artifacts were already recorded"
                )
            page_records, slices_by_key = _formatted_slice_records(
                formatted,
                {page.page_number: page for page in extracted},
            )
            batch_records = _formatter_batch_records(
                formatted,
                formatter_config,
                slices_by_key,
            )
            markdown_pages = [
                MarkdownPage(
                    page_number=record["page_number"],
                    markdown=record["markdown"],
                )
                for record in page_records
            ]
            markdown = canonical_markdown(markdown_pages)
            markdown_sha256 = _sha256_text(markdown)
            now = utc_now()
            session.add_all(
                [
                    FormatterBatch(
                        prepared_revision_id=revision.id,
                        started_at=now,
                        completed_at=now,
                        **record,
                    )
                    for record in batch_records
                ]
            )
            session.add_all(
                [
                    PreparedPage(
                        prepared_revision_id=revision.id,
                        **record,
                    )
                    for record in page_records
                ]
            )
            revision.formatter_complete = True
            revision.markdown_sha256 = markdown_sha256
            session.flush()
    except PreparationError:
        raise
    except (MarkdownChunkingError, SQLAlchemyError) as exc:
        raise PreparationError(
            "record_formatting_failed",
            "validated formatter artifacts could not be recorded",
        ) from exc
    return markdown_sha256


def load_prepared_markdown_pages(
    session_factory: sessionmaker[Session],
    *,
    handle: PreparationHandle,
) -> tuple[MarkdownPage, ...]:
    """Load a detached page snapshot before external tokenization/embedding."""

    try:
        with session_factory() as session:
            revision = _load_revision(session, handle.revision_id)
            _validate_revision_identity(revision, handle)
            if not revision.formatter_complete:
                raise PreparationError(
                    "stage_order", "complete formatter artifacts must be recorded first"
                )
            rows = session.scalars(
                select(PreparedPage)
                .where(PreparedPage.prepared_revision_id == revision.id)
                .order_by(PreparedPage.page_number)
            ).all()
            pages = tuple(
                MarkdownPage(page_number=row.page_number, markdown=row.markdown) for row in rows
            )
        if len(pages) != revision.page_count:
            raise PreparationError(
                "formatter_correlation", "persisted Markdown page coverage was incomplete"
            )
        return pages
    except PreparationError:
        raise
    except SQLAlchemyError as exc:
        raise PreparationError(
            "load_markdown_failed", "prepared Markdown pages could not be loaded"
        ) from exc


def dense_vector_sha256(vector: Sequence[float]) -> str:
    """Hash one exact finite 768-dimensional dense vector."""

    values = [_finite_float(value, "dense vector") for value in vector]
    if len(values) != DENSE_DIMENSION:
        raise PreparationError("vector_correlation", "dense vector was not 768-dimensional")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
        raise PreparationError("vector_correlation", "dense vector was not normalized")
    return _manifest_hash({"dimension": DENSE_DIMENSION, "values": values})


def sparse_vector_sha256(vector: SparseVector) -> str:
    """Hash one exact BM25 document vector after strict coordinate checks."""

    indices = tuple(vector.indices)
    values = tuple(_finite_float(value, "sparse vector") for value in vector.values)
    if (
        not indices
        or len(indices) != len(values)
        or any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in indices
        )
        or len(set(indices)) != len(indices)
        or any(value < 0 for value in values)
    ):
        raise PreparationError(
            "vector_correlation", "sparse document vector was invalid or uncorrelated"
        )
    return _manifest_hash(
        {
            "encoding": "document",
            "indices": list(indices),
            "values": list(values),
        }
    )


def _vector_manifest(vectors: Sequence[PreparedVector]) -> str:
    return _manifest_hash(
        {
            "dense_dimension": DENSE_DIMENSION,
            "sparse_encoding": "document",
            "chunks": [
                {
                    "chunk_id": str(item.chunk.chunk_id),
                    "chunk_index": item.chunk.chunk_index,
                    "text_sha256": item.chunk.text_sha256,
                    "dense_sha256": item.dense_sha256,
                    "sparse_sha256": item.sparse_sha256,
                }
                for item in vectors
            ],
        }
    )


def build_chunk_vector_bundle(
    *,
    handle: PreparationHandle,
    pages: Sequence[MarkdownPage],
    embedding_models: EmbeddingProvider,
    max_chunks: int = 10_000,
    progress_callback: PreparationProgressCallback | None = None,
) -> ChunkVectorBundle:
    """Chunk and embed detached Markdown with no database transaction open."""

    page_list = list(pages)
    try:
        markdown = canonical_markdown(page_list)
        _report_progress(progress_callback, OperationPhase.CHUNKING_MARKDOWN)
        chunks = chunk_markdown(
            page_list,
            document_id=handle.document_id,
            prepared_revision_id=handle.revision_id,
            tokenizer=embedding_models,
            max_chunks=max_chunks,
        )
        texts = [chunk.markdown for chunk in chunks]
        _report_progress(progress_callback, OperationPhase.EMBEDDING_DENSE)
        dense = embedding_models.embed_dense(texts)
        _report_progress(progress_callback, OperationPhase.EMBEDDING_SPARSE)
        sparse = embedding_models.embed_sparse_documents(texts)
    except (MarkdownChunkingError, LocalModelError, ValueError) as exc:
        raise PreparationError(
            "chunk_vector_failed",
            "canonical Markdown could not be chunked and embedded",
        ) from exc
    if not chunks or len(chunks) != len(dense) or len(chunks) != len(sparse):
        raise PreparationError(
            "vector_correlation", "embedding output did not cover every chunk exactly once"
        )

    vectors: list[PreparedVector] = []
    for expected_index, (chunk, dense_vector, sparse_vector) in enumerate(
        zip(chunks, dense, sparse, strict=True)
    ):
        expected_id = uuid.uuid5(
            handle.document_id,
            f"{handle.revision_id}:{expected_index}:{chunk.text_sha256}",
        )
        if (
            chunk.chunk_index != expected_index
            or chunk.chunk_id != expected_id
            or chunk.text_sha256 != _sha256_text(chunk.markdown)
            or not chunk.markdown.strip()
            or chunk.token_count <= 0
            or chunk.token_count > HARD_MAX_TOKENS
            or embedding_models.count_tokens(chunk.markdown) != chunk.token_count
        ):
            raise PreparationError(
                "chunk_correlation", "chunk identity, hash, order, or token count did not match"
            )
        normalized_dense = tuple(_finite_float(value, "dense vector") for value in dense_vector)
        vector = PreparedVector(
            chunk=chunk,
            dense=normalized_dense,
            sparse=sparse_vector,
            dense_sha256=dense_vector_sha256(normalized_dense),
            sparse_sha256=sparse_vector_sha256(sparse_vector),
        )
        vectors.append(vector)
    return ChunkVectorBundle(
        canonical_markdown_sha256=_sha256_text(markdown),
        vectors=tuple(vectors),
        vector_manifest_sha256=_vector_manifest(vectors),
    )


def record_chunk_vectors(
    session_factory: sessionmaker[Session],
    *,
    handle: PreparationHandle,
    bundle: ChunkVectorBundle,
) -> str:
    """Persist complete correlated chunks and document vectors atomically."""

    if not bundle.vectors or bundle.vector_manifest_sha256 != _vector_manifest(bundle.vectors):
        raise PreparationError("vector_correlation", "vector bundle manifest did not match")
    try:
        with session_factory.begin() as session:
            revision = _load_revision(session, handle.revision_id, for_update=True)
            _validate_revision_identity(revision, handle)
            if not revision.formatter_complete or revision.markdown_sha256 is None:
                raise PreparationError(
                    "stage_order", "complete formatter artifacts must be recorded first"
                )
            if revision.markdown_sha256 != bundle.canonical_markdown_sha256:
                raise PreparationError(
                    "chunk_correlation", "chunk input Markdown did not match the revision"
                )
            existing = session.scalar(
                select(func.count(PreparedChunk.id)).where(
                    PreparedChunk.prepared_revision_id == revision.id
                )
            )
            if existing:
                raise PreparationError(
                    "stage_already_recorded", "chunk/vector artifacts were already recorded"
                )

            for item in bundle.vectors:
                chunk = item.chunk
                chunk_row = PreparedChunk(
                    id=chunk.chunk_id,
                    prepared_revision_id=revision.id,
                    chunk_index=chunk.chunk_index,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    heading_path=list(chunk.heading_path),
                    token_count=chunk.token_count,
                    text_sha256=chunk.text_sha256,
                    markdown=chunk.markdown,
                )
                vector_row = PreparedChunkVector(
                    chunk=chunk_row,
                    prepared_revision_id=revision.id,
                    dense_dimension=DENSE_DIMENSION,
                    dense=list(item.dense),
                    sparse_indices=list(item.sparse.indices),
                    sparse_values=list(item.sparse.values),
                    dense_normalized=True,
                    document_encoded=True,
                    dense_sha256=item.dense_sha256,
                    sparse_sha256=item.sparse_sha256,
                )
                session.add_all([chunk_row, vector_row])
            revision.chunk_count = len(bundle.vectors)
            revision.expected_point_count = len(bundle.vectors)
            revision.vector_manifest_sha256 = bundle.vector_manifest_sha256
            revision.vector_complete = True
            session.flush()
    except PreparationError:
        raise
    except SQLAlchemyError as exc:
        raise PreparationError(
            "record_vectors_failed", "chunk/vector artifacts could not be recorded"
        ) from exc
    return bundle.vector_manifest_sha256


def _validate_chunk_rows(
    revision: PreparedRevision,
    document: Document,
    chunks: Sequence[PreparedChunk],
) -> tuple[tuple[ChunkPoint, ...], str]:
    vectors: list[PreparedVector] = []
    points: list[ChunkPoint] = []
    if not chunks:
        raise PreparationError("chunk_correlation", "prepared revision had no chunks")
    for expected_index, chunk in enumerate(chunks):
        vector = chunk.vector
        expected_id = uuid.uuid5(
            document.id,
            f"{revision.id}:{expected_index}:{chunk.text_sha256}",
        )
        if (
            chunk.prepared_revision_id != revision.id
            or chunk.chunk_index != expected_index
            or chunk.id != expected_id
            or chunk.text_sha256 != _sha256_text(chunk.markdown)
            or not chunk.markdown.strip()
            or chunk.page_start < 1
            or chunk.page_end < chunk.page_start
            or revision.page_count is None
            or chunk.page_end > revision.page_count
            or chunk.token_count <= 0
            or chunk.token_count > HARD_MAX_TOKENS
            or not isinstance(chunk.heading_path, list)
            or any(not isinstance(value, str) for value in chunk.heading_path)
            or vector is None
            or vector.chunk_id != chunk.id
            or vector.prepared_revision_id != revision.id
            or vector.dense_dimension != DENSE_DIMENSION
            or not vector.dense_normalized
            or not vector.document_encoded
        ):
            raise PreparationError(
                "chunk_correlation", "persisted chunk/vector correlation was invalid"
            )
        dense = tuple(_finite_float(value, "dense vector") for value in vector.dense)
        sparse = SparseVector(
            indices=tuple(vector.sparse_indices),
            values=tuple(_finite_float(value, "sparse vector") for value in vector.sparse_values),
        )
        dense_hash = dense_vector_sha256(dense)
        sparse_hash = sparse_vector_sha256(sparse)
        if vector.dense_sha256 != dense_hash or vector.sparse_sha256 != sparse_hash:
            raise PreparationError("vector_correlation", "persisted vector hash did not match")
        item = PreparedVector(
            chunk=MarkdownChunk(
                chunk_id=chunk.id,
                chunk_index=chunk.chunk_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                heading_path=tuple(chunk.heading_path),
                token_count=chunk.token_count,
                text_sha256=chunk.text_sha256,
                markdown=chunk.markdown,
            ),
            dense=dense,
            sparse=sparse,
            dense_sha256=dense_hash,
            sparse_sha256=sparse_hash,
        )
        vectors.append(item)
        points.append(
            ChunkPoint(
                chunk_id=chunk.id,
                document_id=document.id,
                prepared_revision_id=revision.id,
                collection_key=document.collection_key,
                active_qdrant_collection=revision.active_qdrant_collection,
                chunk_index=chunk.chunk_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                heading_path=tuple(chunk.heading_path),
                text_sha256=chunk.text_sha256,
                markdown=chunk.markdown,
                content_profile_id=revision.content_profile_id,
                index_profile_id=revision.index_profile_id,
                dense=dense,
                sparse=sparse,
            )
        )
    if revision.chunk_count != len(chunks) or revision.expected_point_count != len(chunks):
        raise PreparationError("chunk_correlation", "persisted chunk counts did not match")
    return tuple(points), _vector_manifest(vectors)


def reconstruct_chunk_points(
    session_factory: sessionmaker[Session],
    *,
    revision_id: uuid.UUID,
    require_sealed: bool = True,
) -> tuple[ChunkPoint, ...]:
    """Rebuild publication points only from persisted prepared artifacts."""

    try:
        with session_factory() as session:
            revision = _load_revision(session, revision_id, required_status=None)
            if require_sealed and revision.status is not RevisionStatus.SEALED:
                raise PreparationError(
                    "revision_state_conflict", "publication requires a sealed revision"
                )
            document = session.get(Document, revision.document_id)
            if document is None:
                raise PreparationError("correlation_failed", "revision document was missing")
            chunks = session.scalars(
                select(PreparedChunk)
                .where(PreparedChunk.prepared_revision_id == revision.id)
                .order_by(PreparedChunk.chunk_index)
            ).all()
            # Access one-to-one vectors while the short read session is open.
            for chunk in chunks:
                _ = chunk.vector
            points, vector_manifest = _validate_chunk_rows(revision, document, chunks)
            if vector_manifest != revision.vector_manifest_sha256:
                raise PreparationError(
                    "vector_correlation", "revision vector manifest did not match its points"
                )
            return points
    except PreparationError:
        raise
    except SQLAlchemyError as exc:
        raise PreparationError(
            "reconstruct_points_failed", "prepared publication points could not be reconstructed"
        ) from exc


def candidate_evidence_sha256(
    *,
    kind: EvidenceKind,
    model_id: str | None,
    valid: bool,
    label: str | None,
    summary: str | None,
    evidence: Sequence[Mapping[str, Any]],
    failure_code: str | None,
) -> str:
    """Hash the persisted public evidence fields, excluding timestamps and IDs."""

    return _manifest_hash(
        {
            "kind": kind.value,
            "model_id": model_id,
            "valid": valid,
            "label": label,
            "summary": summary,
            "evidence": list(evidence),
            "failure_code": failure_code,
        }
    )


def _normalized_citation(
    session: Session,
    value: Mapping[str, Any],
    *,
    allowed_document_ids: set[uuid.UUID],
) -> dict[str, Any]:
    required = {"document_id", "chunk_id", "page_start", "page_end", "excerpt"}
    if set(value) != required:
        raise PreparationError(
            "evidence_correlation", "evidence citation fields did not match the contract"
        )
    try:
        document_id = uuid.UUID(str(value["document_id"]))
        chunk_id = uuid.UUID(str(value["chunk_id"]))
    except (TypeError, ValueError) as exc:
        raise PreparationError(
            "evidence_correlation", "evidence citation identity was invalid"
        ) from exc
    page_start = value["page_start"]
    page_end = value["page_end"]
    excerpt = value["excerpt"]
    if (
        document_id not in allowed_document_ids
        or type(page_start) is not int
        or type(page_end) is not int
        or page_start < 1
        or page_end < page_start
        or not isinstance(excerpt, str)
        or not excerpt.strip()
        or len(excerpt) > 4_000
    ):
        raise PreparationError(
            "evidence_correlation", "evidence citation content was invalid"
        )
    retained_document_id = session.scalar(
        select(PreparedRevision.document_id)
        .join(PreparedChunk, PreparedChunk.prepared_revision_id == PreparedRevision.id)
        .where(PreparedChunk.id == chunk_id)
    )
    if retained_document_id != document_id:
        raise PreparationError(
            "evidence_correlation", "evidence citation did not match a retained chunk"
        )
    return {
        "document_id": str(document_id),
        "chunk_id": str(chunk_id),
        "page_start": page_start,
        "page_end": page_end,
        "excerpt": excerpt,
    }


def _evidence_row(
    session: Session,
    *,
    revision_id: uuid.UUID,
    candidate_id: uuid.UUID,
    record: EvidenceInput,
    allowed_document_ids: set[uuid.UUID],
) -> CandidateEvidence:
    if (
        record.kind is EvidenceKind.DETERMINISTIC
        and (record.model_id is not None or not record.valid)
    ) or (
        record.kind in {EvidenceKind.CLASSIFIER, EvidenceKind.VERIFIER}
        and not record.model_id
    ) or (record.kind is EvidenceKind.INCOMPLETE and record.valid):
        raise PreparationError(
            "evidence_correlation", "evidence kind, model, and validity were contradictory"
        )
    if (
        record.model_id is not None and len(record.model_id) > 255
    ) or (record.label is not None and len(record.label) > 64) or (
        record.summary is not None
        and (not record.summary.strip() or len(record.summary) > 2_000)
    ):
        raise PreparationError("evidence_correlation", "evidence metadata was invalid")
    if record.failure_code is not None and not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]{0,99}", record.failure_code
    ):
        raise PreparationError("evidence_correlation", "evidence failure code was invalid")
    if record.valid == (record.failure_code is not None):
        raise PreparationError(
            "evidence_correlation", "evidence validity and failure code were contradictory"
        )
    if len(record.citations) > 50:
        raise PreparationError(
            "evidence_correlation", "evidence citation count was invalid"
        )
    citations = [
        _normalized_citation(
            session,
            citation,
            allowed_document_ids=allowed_document_ids,
        )
        for citation in record.citations
    ]
    digest = candidate_evidence_sha256(
        kind=record.kind,
        model_id=record.model_id,
        valid=record.valid,
        label=record.label,
        summary=record.summary,
        evidence=citations,
        failure_code=record.failure_code,
    )
    return CandidateEvidence(
        prepared_revision_id=revision_id,
        candidate_id=candidate_id,
        kind=record.kind,
        model_id=record.model_id,
        valid=record.valid,
        label=record.label,
        summary=record.summary,
        evidence=citations,
        failure_code=record.failure_code,
        evidence_sha256=digest,
    )


def record_preflight_candidates(
    session_factory: sessionmaker[Session],
    *,
    handle: PreparationHandle,
    candidates: Sequence[CandidateInput],
) -> tuple[uuid.UUID, ...]:
    """Persist deterministic candidates before any advisory model call."""

    if len(candidates) > 100:
        raise PreparationError("candidate_limit", "candidate count exceeded the safety limit")
    matched_ids = [candidate.matched_document_id for candidate in candidates]
    if len(matched_ids) != len(set(matched_ids)) or handle.document_id in matched_ids:
        raise PreparationError(
            "candidate_correlation", "candidate documents were duplicated or self-referential"
        )
    try:
        with session_factory.begin() as session:
            revision = _load_revision(session, handle.revision_id, for_update=True)
            _validate_revision_identity(revision, handle)
            if not revision.vector_complete:
                raise PreparationError(
                    "stage_order", "complete chunk vectors must be recorded before candidates"
                )
            existing = session.scalar(
                select(func.count(PreparedCandidate.id)).where(
                    PreparedCandidate.prepared_revision_id == revision.id
                )
            )
            if existing:
                raise PreparationError(
                    "stage_already_recorded", "preflight candidates were already recorded"
                )
            matched_documents = {
                document.id: document
                for document in session.scalars(
                    select(Document).where(Document.id.in_(matched_ids))
                ).all()
            }
            if len(matched_documents) != len(matched_ids):
                raise PreparationError(
                    "candidate_correlation", "a candidate document was not retained"
                )

            candidate_ids: list[uuid.UUID] = []
            for rank, item in enumerate(candidates, start=1):
                matched = matched_documents[item.matched_document_id]
                if matched.collection_key != handle.collection_key:
                    raise PreparationError(
                        "candidate_scope", "candidate document crossed a logical collection"
                    )
                if item.source is CandidateSource.ACTIVE:
                    if matched.state is not DocumentState.READY:
                        raise PreparationError(
                            "candidate_correlation", "active candidate was not READY"
                        )
                elif matched.state in {
                    DocumentState.REJECTED,
                    DocumentState.CANCELLED,
                    DocumentState.DELETED,
                }:
                    raise PreparationError(
                        "candidate_correlation", "screening candidate was content-free"
                    )
                if not item.reasons or len(item.reasons) > 50 or len(set(item.reasons)) != len(
                    item.reasons
                ):
                    raise PreparationError(
                        "candidate_correlation", "candidate reasons were missing or duplicated"
                    )
                if any(
                    not reason
                    or len(reason) > 100
                    or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", reason)
                    for reason in item.reasons
                ):
                    raise PreparationError(
                        "candidate_correlation", "candidate reason was invalid"
                    )
                max_cosine = _finite_float(item.max_cosine, "candidate max cosine")
                bm25_score = _finite_float(item.bm25_score, "candidate BM25 score")
                fused_score = _finite_float(item.fused_score, "candidate fused score")
                if not -1 <= max_cosine <= 1 or bm25_score < 0 or fused_score < 0:
                    raise PreparationError(
                        "candidate_correlation", "candidate score was outside its valid range"
                    )
                pairs: list[list[Any]] = []
                for incoming_index, matched_chunk_id in item.matched_chunk_pairs:
                    if incoming_index < 0:
                        raise PreparationError(
                            "candidate_correlation", "candidate chunk correlation was invalid"
                        )
                    pairs.append([incoming_index, str(matched_chunk_id)])
                if len(item.evidence) != 1 or (
                    item.evidence[0].kind is not EvidenceKind.DETERMINISTIC
                ):
                    raise PreparationError(
                        "candidate_correlation",
                        "candidate must first persist exactly one deterministic evidence record",
                    )
                row = PreparedCandidate(
                    prepared_revision_id=revision.id,
                    matched_document_id=matched.id,
                    source=item.source,
                    rank=rank,
                    reasons=list(item.reasons),
                    max_cosine=max_cosine,
                    bm25_score=bm25_score,
                    fused_score=fused_score,
                    matched_chunk_pairs=pairs,
                    document_snapshot={
                        "id": str(matched.id),
                        "collection_key": matched.collection_key,
                        "original_filename": matched.original_filename,
                        "state": matched.state.value,
                        "sha256": matched.sha256,
                    },
                )
                session.add(row)
                session.flush()
                candidate_ids.append(row.id)
                session.add(
                    _evidence_row(
                        session,
                        revision_id=revision.id,
                        candidate_id=row.id,
                        record=item.evidence[0],
                        allowed_document_ids={handle.document_id, matched.id},
                    )
                )
            session.flush()
    except PreparationError:
        raise
    except SQLAlchemyError as exc:
        raise PreparationError(
            "record_candidates_failed", "preflight evidence could not be recorded"
        ) from exc
    return tuple(candidate_ids)


def append_advisory_evidence(
    session_factory: sessionmaker[Session],
    *,
    handle: PreparationHandle,
    evidence_by_candidate: Mapping[uuid.UUID, Sequence[EvidenceInput]],
) -> None:
    """Append classifier/verifier outcomes after deterministic rows are durable."""

    try:
        with session_factory.begin() as session:
            revision = _load_revision(session, handle.revision_id, for_update=True)
            _validate_revision_identity(revision, handle)
            candidates = session.scalars(
                select(PreparedCandidate)
                .where(PreparedCandidate.prepared_revision_id == revision.id)
                .order_by(PreparedCandidate.rank)
            ).all()
            if set(evidence_by_candidate) != {candidate.id for candidate in candidates}:
                raise PreparationError(
                    "evidence_correlation",
                    "advisory evidence did not cover the exact deterministic candidate set",
                )
            for candidate in candidates:
                existing = session.scalars(
                    select(CandidateEvidence).where(
                        CandidateEvidence.candidate_id == candidate.id
                    )
                ).all()
                if {item.kind for item in existing} != {EvidenceKind.DETERMINISTIC}:
                    raise PreparationError(
                        "stage_already_recorded",
                        "candidate advisory evidence was already recorded or incomplete",
                    )
                records = tuple(evidence_by_candidate[candidate.id])
                kinds = [record.kind for record in records]
                if (
                    len(kinds) != len(set(kinds))
                    or EvidenceKind.DETERMINISTIC in kinds
                    or not {EvidenceKind.CLASSIFIER, EvidenceKind.VERIFIER}.issubset(kinds)
                ):
                    raise PreparationError(
                        "evidence_correlation",
                        "advisory evidence requires unique classifier and verifier outcomes",
                    )
                for record in records:
                    session.add(
                        _evidence_row(
                            session,
                            revision_id=revision.id,
                            candidate_id=candidate.id,
                            record=record,
                            allowed_document_ids={
                                handle.document_id,
                                candidate.matched_document_id,
                            },
                        )
                    )
            session.flush()
    except PreparationError:
        raise
    except SQLAlchemyError as exc:
        raise PreparationError(
            "record_advisory_failed", "advisory evidence could not be recorded"
        ) from exc


def _extraction_rows_hash(rows: Sequence[ExtractedPageRow]) -> tuple[str, int]:
    total = 0
    pages: list[dict[str, Any]] = []
    for expected, page in enumerate(rows, start=1):
        if (
            page.page_number != expected
            or page.character_count != len(page.layout_text)
            or page.text_sha256 != _sha256_text(page.layout_text)
        ):
            raise PreparationError(
                "extraction_correlation", "persisted extraction page correlation was invalid"
            )
        total += page.character_count
        pages.append(
            {
                "page_number": page.page_number,
                "layout_text": page.layout_text,
                "character_count": page.character_count,
                "text_sha256": page.text_sha256,
            }
        )
    if not pages:
        raise PreparationError("extraction_correlation", "persisted extraction was empty")
    return (
        _manifest_hash(
            {
                "extraction_profile": EXTRACTION_PROFILE,
                "page_count": len(pages),
                "character_count": total,
                "pages": pages,
            }
        ),
        len(pages),
    )


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_persisted_pages(
    revision: PreparedRevision,
    extracted: Sequence[ExtractedPageRow],
    prepared: Sequence[PreparedPage],
) -> tuple[str, str]:
    extraction_hash, page_count = _extraction_rows_hash(extracted)
    if page_count != revision.page_count or extraction_hash != revision.extraction_sha256:
        raise PreparationError(
            "extraction_correlation", "revision extraction manifest did not match its pages"
        )
    if len(prepared) != page_count:
        raise PreparationError(
            "formatter_correlation", "persisted Markdown did not cover every extracted page"
        )

    markdown_pages: list[MarkdownPage] = []
    for expected, (source, page) in enumerate(zip(extracted, prepared, strict=True), start=1):
        if (
            page.page_number != expected
            or page.markdown_sha256 != _sha256_text(page.markdown)
            or not page.markdown.strip()
            or not isinstance(page.slices, list)
            or not page.slices
        ):
            raise PreparationError(
                "formatter_correlation", "persisted Markdown page identity was invalid"
            )
        source_cursor = 0
        markdown_cursor = 0
        for expected_slice, item in enumerate(page.slices):
            required = {
                "slice_index",
                "source_start",
                "source_end",
                "markdown_start",
                "markdown_end",
                "source_text_sha256",
                "markdown_sha256",
                "source_projection_sha256",
                "markdown_projection_sha256",
            }
            if not isinstance(item, dict) or set(item) != required:
                raise PreparationError(
                    "formatter_correlation", "persisted formatter slice shape was invalid"
                )
            if not all(
                _strict_int(item[field])
                for field in (
                    "slice_index",
                    "source_start",
                    "source_end",
                    "markdown_start",
                    "markdown_end",
                )
            ):
                raise PreparationError(
                    "formatter_correlation", "persisted formatter slice offsets were invalid"
                )
            if (
                item["slice_index"] != expected_slice
                or item["source_start"] != source_cursor
                or item["source_end"] < item["source_start"]
                or item["markdown_start"] != markdown_cursor + (2 if expected_slice else 0)
                or item["markdown_end"] <= item["markdown_start"]
            ):
                raise PreparationError(
                    "formatter_correlation", "persisted formatter slice coverage was invalid"
                )
            source_text = source.layout_text[item["source_start"] : item["source_end"]]
            markdown_text = page.markdown[item["markdown_start"] : item["markdown_end"]]
            source_projection = _projection_hash(fidelity_projection(source_text))
            markdown_projection = _projection_hash(
                fidelity_projection(markdown_text, markdown=True)
            )
            if (
                item["source_text_sha256"] != _sha256_text(source_text)
                or item["markdown_sha256"] != _sha256_text(markdown_text)
                or item["source_projection_sha256"] != source_projection
                or item["markdown_projection_sha256"] != markdown_projection
                or source_projection != markdown_projection
            ):
                raise PreparationError(
                    "formatter_correlation", "persisted formatter slice hashes did not match"
                )
            source_cursor = item["source_end"]
            markdown_cursor = item["markdown_end"]
        source_projection = _projection_hash(fidelity_projection(source.layout_text))
        markdown_projection = _projection_hash(fidelity_projection(page.markdown, markdown=True))
        if (
            source_cursor != len(source.layout_text)
            or markdown_cursor != len(page.markdown)
            or page.source_projection_sha256 != source_projection
            or page.markdown_projection_sha256 != markdown_projection
            or source_projection != markdown_projection
        ):
            raise PreparationError(
                "formatter_correlation", "persisted formatter page coverage did not match"
            )
        markdown_pages.append(MarkdownPage(page_number=expected, markdown=page.markdown))
    try:
        markdown_hash = _sha256_text(canonical_markdown(markdown_pages))
    except MarkdownChunkingError as exc:
        raise PreparationError(
            "formatter_correlation", "persisted canonical Markdown was invalid"
        ) from exc
    if markdown_hash != revision.markdown_sha256:
        raise PreparationError(
            "formatter_correlation", "revision Markdown manifest did not match its pages"
        )
    return extraction_hash, markdown_hash


def _validate_formatter_batches(
    revision: PreparedRevision,
    batches: Sequence[FormatterBatch],
    pages: Sequence[PreparedPage],
) -> None:
    expected_slices: dict[tuple[int, int], dict[str, Any]] = {}
    expected_order: list[tuple[int, int]] = []
    for page in pages:
        for item in page.slices:
            key = (page.page_number, item["slice_index"])
            expected_slices[key] = item
            expected_order.append(key)
    if not batches or not expected_slices:
        raise PreparationError(
            "formatter_correlation", "formatter attempts or page slices were missing"
        )

    grouped: dict[int, list[FormatterBatch]] = {}
    for batch in batches:
        if batch.prepared_revision_id != revision.id:
            raise PreparationError(
                "formatter_correlation", "formatter attempt belonged to another revision"
            )
        grouped.setdefault(batch.batch_index, []).append(batch)
    if set(grouped) != set(range(len(grouped))):
        raise PreparationError(
            "formatter_correlation", "formatter batch indexes were not consecutive"
        )

    valid_coverage: list[tuple[int, int]] = []
    for batch_index in range(len(grouped)):
        attempts = sorted(grouped[batch_index], key=lambda item: item.attempt)
        if [item.attempt for item in attempts] != list(range(1, len(attempts) + 1)):
            raise PreparationError(
                "formatter_correlation", "formatter attempt numbers were not consecutive"
            )
        valid_attempts = [item for item in attempts if item.status is FormatterBatchStatus.VALID]
        if len(valid_attempts) != 1 or valid_attempts[0] is not attempts[-1]:
            raise PreparationError(
                "formatter_correlation", "formatter batch lacked one final valid attempt"
            )
        reference = valid_attempts[0].page_slices
        if not isinstance(reference, list) or not reference:
            raise PreparationError(
                "formatter_correlation", "formatter attempt slice coverage was empty"
            )
        for attempt in attempts:
            if not isinstance(attempt.page_slices, list):
                raise PreparationError(
                    "formatter_correlation", "formatter attempt slice coverage was invalid"
                )
            source_slices = [
                {
                    "page_number": item.get("page_number"),
                    "slice_index": item.get("slice_index"),
                    "source_text_sha256": item.get("source_text_sha256"),
                    "source_projection_sha256": item.get("source_projection_sha256"),
                }
                for item in attempt.page_slices
                if isinstance(item, dict)
            ]
            if (
                len(source_slices) != len(reference)
                or [(item["page_number"], item["slice_index"]) for item in source_slices]
                != [(item["page_number"], item["slice_index"]) for item in reference]
                or not _SHA256.fullmatch(attempt.source_manifest_sha256)
                or not _SHA256.fullmatch(attempt.request_sha256)
                or attempt.source_manifest_sha256 != _manifest_hash(source_slices)
                or not isinstance(attempt.validation_errors, list)
                or any(
                    not isinstance(error, str) or len(error) > _MAX_ERROR_CHARACTERS
                    for error in attempt.validation_errors
                )
            ):
                raise PreparationError(
                    "formatter_correlation", "formatter attempt hashes or coverage did not match"
                )
            if attempt.status is FormatterBatchStatus.VALID:
                if (
                    attempt.finish_reason != "stop"
                    or attempt.response_sha256 is None
                    or not _SHA256.fullmatch(attempt.response_sha256)
                    or attempt.validation_errors
                ):
                    raise PreparationError(
                        "formatter_correlation", "valid formatter attempt proof was invalid"
                    )
            elif attempt.status is not FormatterBatchStatus.FAILED:
                raise PreparationError(
                    "formatter_correlation", "formatter attempt retained a nonterminal status"
                )
            for item in attempt.page_slices:
                if not isinstance(item, dict) or set(item) != {
                    "page_number",
                    "slice_index",
                    "source_text_sha256",
                    "source_projection_sha256",
                    "markdown_projection_sha256",
                }:
                    raise PreparationError(
                        "formatter_correlation", "formatter batch slice shape was invalid"
                    )
                projection = item["markdown_projection_sha256"]
                if projection is not None and not _SHA256.fullmatch(projection):
                    raise PreparationError(
                        "formatter_correlation", "formatter attempt projection hash was invalid"
                    )
        for item in reference:
            required = {
                "page_number",
                "slice_index",
                "source_text_sha256",
                "source_projection_sha256",
                "markdown_projection_sha256",
            }
            if not isinstance(item, dict) or set(item) != required:
                raise PreparationError(
                    "formatter_correlation", "formatter batch slice shape was invalid"
                )
            key = (item["page_number"], item["slice_index"])
            page_slice = expected_slices.get(key)
            if (
                page_slice is None
                or item["source_text_sha256"] != page_slice["source_text_sha256"]
                or item["source_projection_sha256"] != page_slice["source_projection_sha256"]
                or item["markdown_projection_sha256"] != page_slice["markdown_projection_sha256"]
            ):
                raise PreparationError(
                    "formatter_correlation", "formatter batch did not match prepared pages"
                )
            valid_coverage.append(key)
    if valid_coverage != expected_order:
        raise PreparationError(
            "formatter_correlation", "valid formatter batches did not exactly cover page slices"
        )


def _evidence_manifest(
    revision: PreparedRevision,
    candidates: Sequence[PreparedCandidate],
    evidence_rows: Sequence[CandidateEvidence],
    completeness: PreflightCompleteness,
) -> tuple[str, tuple[str, ...]]:
    reasons = tuple(sorted(set(completeness.incomplete_reasons)))
    if any(not reason.strip() or len(reason) > 200 for reason in reasons):
        raise PreparationError("evidence_correlation", "incomplete reason was invalid")
    expected_clear = (
        completeness.candidate_discovery_complete
        and completeness.advisory_complete
        and not candidates
        and not reasons
    )
    if completeness.clear_for_publication is not expected_clear:
        raise PreparationError(
            "evidence_correlation", "clear-for-publication flags were contradictory"
        )
    if (
        not completeness.candidate_discovery_complete or not completeness.advisory_complete
    ) and not reasons:
        raise PreparationError(
            "evidence_correlation", "incomplete preflight required a bounded reason"
        )
    if completeness.candidate_discovery_complete and completeness.advisory_complete and reasons:
        raise PreparationError(
            "evidence_correlation", "complete preflight could not retain incomplete reasons"
        )

    by_candidate: dict[uuid.UUID, list[CandidateEvidence]] = {}
    for evidence in evidence_rows:
        if evidence.prepared_revision_id != revision.id:
            raise PreparationError(
                "evidence_correlation", "evidence belonged to a different revision"
            )
        by_candidate.setdefault(evidence.candidate_id, []).append(evidence)

    candidate_manifest: list[dict[str, Any]] = []
    for expected_rank, candidate in enumerate(candidates, start=1):
        if (
            candidate.prepared_revision_id != revision.id
            or candidate.rank != expected_rank
            or candidate.matched_document_id == revision.document_id
        ):
            raise PreparationError(
                "evidence_correlation", "candidate rank or revision correlation was invalid"
            )
        scores = {
            "max_cosine": _finite_float(candidate.max_cosine, "candidate score"),
            "bm25_score": _finite_float(candidate.bm25_score, "candidate score"),
            "fused_score": _finite_float(candidate.fused_score, "candidate score"),
        }
        rows = sorted(by_candidate.pop(candidate.id, []), key=lambda item: item.kind.value)
        kinds: set[EvidenceKind] = set()
        evidence_manifest: list[dict[str, Any]] = []
        for evidence in rows:
            if evidence.kind in kinds:
                raise PreparationError(
                    "evidence_correlation", "candidate evidence kind was duplicated"
                )
            kinds.add(evidence.kind)
            calculated = candidate_evidence_sha256(
                kind=evidence.kind,
                model_id=evidence.model_id,
                valid=evidence.valid,
                label=evidence.label,
                summary=evidence.summary,
                evidence=evidence.evidence,
                failure_code=evidence.failure_code,
            )
            if evidence.evidence_sha256 != calculated:
                raise PreparationError(
                    "evidence_correlation", "candidate evidence hash did not match"
                )
            evidence_manifest.append(
                {
                    "kind": evidence.kind.value,
                    "evidence_sha256": calculated,
                    "valid": evidence.valid,
                }
            )
        valid_kinds = {item.kind for item in rows if item.valid}
        if (
            completeness.candidate_discovery_complete
            and EvidenceKind.DETERMINISTIC not in valid_kinds
        ):
            raise PreparationError(
                "evidence_correlation", "complete discovery lacked deterministic evidence"
            )
        if completeness.advisory_complete and not {
            EvidenceKind.CLASSIFIER,
            EvidenceKind.VERIFIER,
        }.issubset(valid_kinds):
            raise PreparationError(
                "evidence_correlation", "complete advisory review lacked classifier or verifier"
            )
        if completeness.advisory_complete and EvidenceKind.INCOMPLETE in kinds:
            raise PreparationError(
                "evidence_correlation", "complete advisory review retained incomplete evidence"
            )
        candidate_manifest.append(
            {
                "candidate_id": str(candidate.id),
                "matched_document_id": str(candidate.matched_document_id),
                "source": candidate.source.value,
                "rank": candidate.rank,
                "reasons": candidate.reasons,
                **scores,
                "matched_chunk_pairs": candidate.matched_chunk_pairs,
                "document_snapshot": candidate.document_snapshot,
                "evidence": evidence_manifest,
            }
        )
    if by_candidate:
        raise PreparationError(
            "evidence_correlation", "evidence referenced an unknown prepared candidate"
        )
    manifest = {
        "candidate_discovery_complete": completeness.candidate_discovery_complete,
        "advisory_complete": completeness.advisory_complete,
        "clear_for_publication": completeness.clear_for_publication,
        "incomplete_reasons": list(reasons),
        "candidates": candidate_manifest,
    }
    return _manifest_hash(manifest), reasons


def _whole_manifest(
    revision: PreparedRevision,
    document: Document,
    *,
    extraction_sha256: str,
    markdown_sha256: str,
    vector_manifest_sha256: str,
    evidence_manifest_sha256: str,
    formatter_tokenizer_class: str,
    completeness: PreflightCompleteness,
    incomplete_reasons: Sequence[str],
) -> str:
    return _manifest_hash(
        {
            "manifest_version": PREPARATION_MANIFEST_VERSION,
            "revision_id": str(revision.id),
            "document_id": str(document.id),
            "revision_number": revision.revision_number,
            "collection_key": document.collection_key,
            "active_qdrant_collection": revision.active_qdrant_collection,
            "content_profile_id": revision.content_profile_id,
            "index_profile_id": revision.index_profile_id,
            "preflight_policy_id": revision.preflight_policy_id,
            "formatter_model_id": revision.formatter_model_id,
            "formatter_tokenizer_class": formatter_tokenizer_class,
            "dense_model_id": revision.dense_model_id,
            "dense_dimension": revision.dense_dimension,
            "sparse_model_id": revision.sparse_model_id,
            "page_count": revision.page_count,
            "chunk_count": revision.chunk_count,
            "expected_point_count": revision.expected_point_count,
            "extraction_sha256": extraction_sha256,
            "markdown_sha256": markdown_sha256,
            "vector_manifest_sha256": vector_manifest_sha256,
            "evidence_manifest_sha256": evidence_manifest_sha256,
            "native_text_eligible": revision.native_text_eligible,
            "formatter_complete": revision.formatter_complete,
            "vector_complete": revision.vector_complete,
            "candidate_discovery_complete": completeness.candidate_discovery_complete,
            "advisory_complete": completeness.advisory_complete,
            "clear_for_publication": completeness.clear_for_publication,
            "incomplete_reasons": list(incomplete_reasons),
        }
    )


def seal_prepared_revision(
    session_factory: sessionmaker[Session],
    *,
    handle: PreparationHandle,
    completeness: PreflightCompleteness,
) -> SealedPreparation:
    """Exhaustively validate and atomically make the revision immutable."""

    validate_preparation_identity(handle.identity)
    try:
        with session_factory.begin() as session:
            revision = _load_revision(session, handle.revision_id, for_update=True)
            _validate_revision_identity(revision, handle)
            document = session.get(Document, revision.document_id)
            if document is None or document.collection_key != handle.collection_key:
                raise PreparationError(
                    "revision_identity_mismatch", "revision document correlation did not match"
                )
            if (
                not revision.native_text_eligible
                or not revision.formatter_complete
                or not revision.vector_complete
            ):
                raise PreparationError(
                    "partial_preparation", "partial content artifacts could not be sealed"
                )
            extracted = session.scalars(
                select(ExtractedPageRow)
                .where(ExtractedPageRow.prepared_revision_id == revision.id)
                .order_by(ExtractedPageRow.page_number)
            ).all()
            pages = session.scalars(
                select(PreparedPage)
                .where(PreparedPage.prepared_revision_id == revision.id)
                .order_by(PreparedPage.page_number)
            ).all()
            extraction_hash, markdown_hash = _validate_persisted_pages(revision, extracted, pages)
            batches = session.scalars(
                select(FormatterBatch)
                .where(FormatterBatch.prepared_revision_id == revision.id)
                .order_by(FormatterBatch.batch_index, FormatterBatch.attempt)
            ).all()
            _validate_formatter_batches(revision, batches, pages)
            chunks = session.scalars(
                select(PreparedChunk)
                .where(PreparedChunk.prepared_revision_id == revision.id)
                .order_by(PreparedChunk.chunk_index)
            ).all()
            for chunk in chunks:
                _ = chunk.vector
            _, vector_hash = _validate_chunk_rows(revision, document, chunks)
            if vector_hash != revision.vector_manifest_sha256:
                raise PreparationError(
                    "vector_correlation", "revision vector manifest did not match"
                )

            candidates = session.scalars(
                select(PreparedCandidate)
                .where(PreparedCandidate.prepared_revision_id == revision.id)
                .order_by(PreparedCandidate.rank, PreparedCandidate.id)
            ).all()
            evidence = session.scalars(
                select(CandidateEvidence).where(
                    CandidateEvidence.prepared_revision_id == revision.id
                )
            ).all()
            evidence_hash, reasons = _evidence_manifest(
                revision,
                candidates,
                evidence,
                completeness,
            )
            manifest_hash = _whole_manifest(
                revision,
                document,
                extraction_sha256=extraction_hash,
                markdown_sha256=markdown_hash,
                vector_manifest_sha256=vector_hash,
                evidence_manifest_sha256=evidence_hash,
                formatter_tokenizer_class=handle.identity.formatter_tokenizer_class,
                completeness=completeness,
                incomplete_reasons=reasons,
            )

            now = utc_now()
            revision.candidate_discovery_complete = completeness.candidate_discovery_complete
            revision.advisory_complete = completeness.advisory_complete
            revision.clear_for_publication = completeness.clear_for_publication
            revision.incomplete_reasons = list(reasons)
            revision.extraction_sha256 = extraction_hash
            revision.markdown_sha256 = markdown_hash
            revision.vector_manifest_sha256 = vector_hash
            revision.evidence_manifest_sha256 = evidence_hash
            revision.manifest_sha256 = manifest_hash
            revision.completed_at = now
            revision.sealed_at = now
            revision.failure_code = None
            revision.failure_message = None
            revision.status = RevisionStatus.SEALED
            session.flush()
            sealed = SealedPreparation(
                revision_id=revision.id,
                manifest_sha256=manifest_hash,
                extraction_sha256=extraction_hash,
                markdown_sha256=markdown_hash,
                vector_manifest_sha256=vector_hash,
                evidence_manifest_sha256=evidence_hash,
                page_count=revision.page_count or 0,
                chunk_count=revision.chunk_count or 0,
                clear_for_publication=revision.clear_for_publication,
            )
    except PreparationError:
        raise
    except SQLAlchemyError as exc:
        raise PreparationError(
            "seal_preparation_failed", "prepared revision could not be sealed"
        ) from exc
    return sealed


def fail_preparation(
    session_factory: sessionmaker[Session],
    *,
    revision_id: uuid.UUID,
    error: PreparationError,
) -> None:
    """Mark a non-sealed revision failed without exposing source/provider data."""

    try:
        with session_factory.begin() as session:
            revision = _load_revision(session, revision_id, required_status=None, for_update=True)
            if revision.status is RevisionStatus.SEALED:
                raise PreparationError(
                    "revision_state_conflict", "a sealed revision cannot be marked failed"
                )
            if revision.status is RevisionStatus.FAILED:
                return
            revision.status = RevisionStatus.FAILED
            revision.failure_code = error.code
            revision.failure_message = _sanitize(str(error))
            revision.completed_at = utc_now()
            session.flush()
    except PreparationError:
        raise
    except SQLAlchemyError as exc:
        raise PreparationError(
            "record_failure_failed", "preparation failure could not be recorded"
        ) from exc


def _external_error(exc: Exception) -> PreparationError:
    if isinstance(exc, PreparationError):
        return exc
    if isinstance(exc, ExtractionRejectedError):
        return PreparationError(
            f"extraction_{exc.reason}",
            "source PDF did not satisfy the target extraction policy",
        )
    if isinstance(exc, ExtractionInfrastructureError):
        return PreparationError("extraction_unavailable", "required PDF extraction was unavailable")
    if isinstance(exc, MarkdownFormattingError):
        return PreparationError("formatter_failed", str(exc))
    if isinstance(exc, (MarkdownChunkingError, LocalModelError)):
        return PreparationError(
            "chunk_vector_failed", "canonical Markdown could not be chunked and embedded"
        )
    return PreparationError("preparation_failed", "preparation could not be completed")


def prepare_revision_content(
    session_factory: sessionmaker[Session],
    *,
    document_id: uuid.UUID,
    identity: PreparationIdentity,
    source_path: Path,
    extraction_limits: ExtractionLimits,
    language_detector: EnglishDetector,
    formatter_config: FormatterConfig,
    formatter_client: FormatterClient,
    embedding_models: EmbeddingProvider,
    max_chunks: int = 10_000,
    progress_callback: PreparationProgressCallback | None = None,
) -> ContentPreparationResult:
    """Run content preparation through persisted vectors, but not candidate work."""

    if (
        formatter_config.model_id != identity.formatter_model_id
        or formatter_config.expected_tokenizer_class != identity.formatter_tokenizer_class
    ):
        raise PreparationError(
            "formatter_identity_mismatch",
            "formatter model or tokenizer did not match the revision identity",
        )
    handle = begin_preparation(
        session_factory,
        document_id=document_id,
        identity=identity,
    )

    def checkpoint(phase: OperationPhase) -> None:
        try:
            _report_progress(progress_callback, phase)
        except Exception as exc:
            raise _ProgressCallbackFailed(exc) from exc

    try:
        extracted = extract_pdf_layout(source_path, extraction_limits)
        checkpoint(OperationPhase.CHECKING_ELIGIBILITY)
        validate_native_english(extracted, language_detector)
        extraction_hash = record_extraction(
            session_factory,
            handle=handle,
            document=extracted,
        )

        def report_formatter_progress(progress: FormatterProgress) -> None:
            checkpoint(OperationPhase(progress.value))

        formatted = format_markdown_document(
            [
                LayoutPage(page_number=page.page_number, text=page.layout_text)
                for page in extracted.pages
            ],
            formatter_config,
            client=formatter_client,
            progress_callback=report_formatter_progress,
        )
        markdown_hash = record_formatting(
            session_factory,
            handle=handle,
            formatted=formatted,
            formatter_config=formatter_config,
        )

        pages = load_prepared_markdown_pages(session_factory, handle=handle)
        bundle = build_chunk_vector_bundle(
            handle=handle,
            pages=pages,
            embedding_models=embedding_models,
            max_chunks=max_chunks,
            progress_callback=checkpoint,
        )
        vector_hash = record_chunk_vectors(
            session_factory,
            handle=handle,
            bundle=bundle,
        )
        points = reconstruct_chunk_points(
            session_factory,
            revision_id=handle.revision_id,
            require_sealed=False,
        )
        return ContentPreparationResult(
            handle=handle,
            extraction_sha256=extraction_hash,
            markdown_sha256=markdown_hash,
            vector_manifest_sha256=vector_hash,
            points=points,
        )
    except _ProgressCallbackFailed as exc:
        raise exc.cause from exc
    except Exception as exc:
        error = _external_error(exc)
        try:
            fail_preparation(
                session_factory,
                revision_id=handle.revision_id,
                error=error,
            )
        except PreparationError as failure_error:
            raise PreparationError(
                "failure_persistence_failed",
                "preparation failed and its durable failure checkpoint could not be recorded",
            ) from failure_error
        if error is exc:
            raise
        raise error from exc
