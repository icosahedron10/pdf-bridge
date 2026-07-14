"""Strict, content-safe API v2 contracts.

These models describe only the canonical operator/service surface. Protected
storage paths, prompts, provider output, and numeric vectors are intentionally
not representable by any public response model.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from pdf_bridge.persistence.models import (
    CandidateSource,
    DecisionAction,
    DeletionPhase,
    DocumentState,
    EvidenceKind,
    OperationPhase,
    OperationState,
    OperationType,
    PublicationStatus,
    RevisionStatus,
    ScanState,
    TerminalDisposition,
)


def _canonical_uuid(value: object) -> object:
    if isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("UUID values must use lowercase canonical form") from exc
        if value != str(parsed):
            raise ValueError("UUID values must use lowercase canonical form")
    return value


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware UTC values")
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be UTC values")
    return value


CanonicalUuid = Annotated[uuid.UUID, BeforeValidator(_canonical_uuid)]
UtcTimestamp = Annotated[datetime, AfterValidator(_utc_timestamp)]
CollectionKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProfileId = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
OpaqueCursor = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=2_048,
        pattern=r"^[A-Za-z0-9._~-]+$",
    ),
]
FailureCode = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$",
    ),
]
BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
ModelIdentity = Annotated[str, StringConstraints(min_length=1, max_length=255)]


class ApiModel(BaseModel):
    """Forbid accidental public fields and permit ORM-backed serialization."""

    model_config = ConfigDict(extra="forbid", from_attributes=True, frozen=True)


class ErrorDetail(ApiModel):
    """One sanitized error with no provider, content, or path detail."""

    code: FailureCode
    message: BoundedText
    request_id: CanonicalUuid
    retryable: bool
    existing_document_id: CanonicalUuid | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ErrorResponse(ApiModel):
    """Canonical nested error envelope."""

    error: ErrorDetail


T = TypeVar("T")


class CursorPage(ApiModel, Generic[T]):
    """Opaque-cursor response shared by every list resource."""

    items: list[T] = Field(max_length=100)
    limit: int = Field(ge=1, le=100)
    next_cursor: OpaqueCursor | None = None
    has_more: bool = False

    @model_validator(mode="after")
    def cursor_matches_page_state(self) -> Self:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("has_more must exactly match the presence of next_cursor")
        if len(self.items) > self.limit:
            raise ValueError("items cannot exceed the requested cursor limit")
        return self


class CursorQuery(ApiModel):
    """Bounded query parameters for an opaque-cursor list."""

    cursor: OpaqueCursor | None = None
    limit: int = Field(default=50, ge=1, le=100)


class SanitizedFailure(ApiModel):
    """Operator-visible failure facts safe for responses and support bundles."""

    code: FailureCode
    message: BoundedText
    retryable: bool
    phase: OperationPhase | None = None


class AllowedAction(str, Enum):
    """Lifecycle mutations currently valid for a document."""

    KEEP = "KEEP"
    REPLACE = "REPLACE"
    CANCEL = "CANCEL"
    RETRY = "RETRY"
    DELETE = "DELETE"


class OperationPriorityName(str, Enum):
    """Stable public names for the integer persistence queue priorities."""

    HIGH = "HIGH"
    REPLACEMENT = "REPLACEMENT"
    PUBLISH = "PUBLISH"
    NORMAL = "NORMAL"


class OperationSummary(ApiModel):
    """Current durable operation shown with document mutations."""

    id: CanonicalUuid
    operation_type: OperationType
    state: OperationState
    phase: OperationPhase
    priority: OperationPriorityName
    attempt: int = Field(ge=1)
    retryable: bool
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    completed_at: UtcTimestamp | None = None


class OperationDetail(OperationSummary):
    """Queue, phase, timing, and sanitized failure details for polling."""

    document_id: CanonicalUuid
    prepared_revision_id: CanonicalUuid | None = None
    replacement_target_document_id: CanonicalUuid | None = None
    queue_position: int | None = Field(default=None, ge=1)
    queue_age_seconds: float = Field(ge=0, allow_inf_nan=False)
    phase_age_seconds: float = Field(ge=0, allow_inf_nan=False)
    started_at: UtcTimestamp | None = None
    failure: SanitizedFailure | None = None

    @model_validator(mode="after")
    def failed_operation_has_failure(self) -> Self:
        if self.state is OperationState.FAILED and self.failure is None:
            raise ValueError("failed operations require a sanitized failure")
        return self


class OperationMetricBucket(ApiModel):
    """Content-free aggregate for one durable operation state and phase."""

    operation_type: OperationType
    state: OperationState
    phase: OperationPhase
    count: int = Field(ge=1)
    oldest_operation_age_seconds: float = Field(ge=0, allow_inf_nan=False)
    oldest_phase_age_seconds: float = Field(ge=0, allow_inf_nan=False)


class OperationMetricsResponse(ApiModel):
    """Bounded queue and phase aggregates for operations and alerting."""

    generated_at: UtcTimestamp
    total: int = Field(ge=0)
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    failed: int = Field(ge=0)
    oldest_queued_age_seconds: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    buckets: list[OperationMetricBucket] = Field(max_length=100)

    @model_validator(mode="after")
    def totals_match_buckets(self) -> Self:
        states = {
            OperationState.QUEUED: self.queued,
            OperationState.RUNNING: self.running,
            OperationState.FAILED: self.failed,
        }
        if self.total != sum(bucket.count for bucket in self.buckets):
            raise ValueError("operation metric total must equal the bucket counts")
        for state, expected in states.items():
            if expected != sum(
                bucket.count for bucket in self.buckets if bucket.state is state
            ):
                raise ValueError("operation metric state totals must equal the buckets")
        if (self.queued == 0) != (self.oldest_queued_age_seconds is None):
            raise ValueError("oldest queued age must exist exactly when queued work exists")
        return self


class CollectionStateCounts(ApiModel):
    """Exact per-state document counts for one logical collection."""

    total: int = Field(ge=0)
    by_state: dict[DocumentState, int] = Field(max_length=len(DocumentState))

    @field_validator("by_state")
    @classmethod
    def state_counts_are_complete_and_nonnegative(
        cls, value: dict[DocumentState, int]
    ) -> dict[DocumentState, int]:
        if set(value) != set(DocumentState):
            raise ValueError("by_state must contain every target document state exactly once")
        if any(count < 0 for count in value.values()):
            raise ValueError("document state counts cannot be negative")
        return value

    @model_validator(mode="after")
    def total_matches_states(self) -> Self:
        if sum(self.by_state.values()) != self.total:
            raise ValueError("total must equal the sum of all document state counts")
        return self


class CollectionSummary(ApiModel):
    """Configured logical collection metadata and lifecycle counts."""

    key: CollectionKey
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2_000)
    audience: str = Field(min_length=1, max_length=63)
    enabled: bool
    counts: CollectionStateCounts


class CollectionPhysicalTarget(ApiModel):
    """Read-only status for the platform-owned fixed active collection."""

    qdrant_collection_name: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$",
    )
    schema_version: Literal[2]
    schema_compatible: bool
    failure: SanitizedFailure | None = None


class CollectionDetail(CollectionSummary):
    """One logical collection and its immutable physical target status."""

    target: CollectionPhysicalTarget


class CollectionListResponse(CursorPage[CollectionSummary]):
    """Cursor page of configured logical collections."""


class DocumentListQuery(CursorQuery):
    """Collection document filters."""

    state: DocumentState | None = None


class NameCheckRequest(ApiModel):
    """Filename-only advisory submitted before upload."""

    filename: str = Field(min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def filename_is_safe_pdf_metadata(cls, value: str) -> str:
        filename = value.strip()
        if (
            not filename
            or not filename.casefold().endswith(".pdf")
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 for character in filename)
        ):
            raise ValueError("filename must be a path-free PDF filename")
        return filename


class NameCheckMatch(ApiModel):
    """One exact-name or filename-family advisory match."""

    kind: Literal["EXACT_NAME", "FILENAME_FAMILY"]
    document_id: CanonicalUuid
    original_filename: str = Field(min_length=1, max_length=255)
    state: DocumentState
    similarity: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def similarity_matches_kind(self) -> Self:
        if self.kind == "EXACT_NAME" and self.similarity not in {None, 1.0}:
            raise ValueError("exact-name similarity, when present, must equal 1")
        if self.kind == "FILENAME_FAMILY" and self.similarity is None:
            raise ValueError("filename-family matches require similarity")
        return self


class NameCheckResponse(ApiModel):
    """Bounded collection-scoped filename advisory."""

    collection_key: CollectionKey
    normalized_filename: str = Field(min_length=1, max_length=255)
    matches: list[NameCheckMatch] = Field(default_factory=list, max_length=100)


class SourceMetadata(ApiModel):
    """Immutable source facts; no object-store location is public."""

    original_filename: str = Field(min_length=1, max_length=255)
    content_type: Literal["application/pdf"]
    size_bytes: int = Field(ge=1)
    sha256: Sha256
    created_by: str = Field(min_length=1, max_length=255)
    created_at: UtcTimestamp
    scan_state: ScanState
    scan_engine: str | None = Field(default=None, max_length=100)
    scanned_at: UtcTimestamp | None = None
    available: bool


class PreflightCompleteness(ApiModel):
    """Preparation and advisory gates required before publication."""

    native_text_eligible: bool
    formatter_complete: bool
    vector_complete: bool
    candidate_discovery_complete: bool
    advisory_complete: bool
    clear_for_publication: bool
    incomplete_reasons: list[FailureCode] = Field(default_factory=list, max_length=50)


class PreparedRevisionSummary(ApiModel):
    """Immutable prepared-revision identity and correlation facts."""

    id: CanonicalUuid
    revision_number: int = Field(ge=1)
    status: RevisionStatus
    active_qdrant_collection: str = Field(min_length=1, max_length=255)
    content_profile_id: ProfileId
    index_profile_id: ProfileId
    preflight_policy_id: ProfileId
    formatter_model_id: ModelIdentity
    dense_model_id: ModelIdentity
    dense_dimension: Literal[768]
    sparse_model_id: ModelIdentity
    language_code: str | None = Field(default=None, min_length=2, max_length=16)
    completeness: PreflightCompleteness
    page_count: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=0)
    expected_point_count: int | None = Field(default=None, ge=0)
    markdown_sha256: Sha256 | None = None
    manifest_sha256: Sha256 | None = None
    failure: SanitizedFailure | None = None
    created_at: UtcTimestamp
    sealed_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def sealed_revision_has_complete_manifest(self) -> Self:
        if self.status is RevisionStatus.SEALED:
            if (
                self.manifest_sha256 is None
                or self.sealed_at is None
                or self.page_count is None
                or self.chunk_count is None
                or self.expected_point_count != self.chunk_count
            ):
                raise ValueError("sealed revisions require complete correlated counts and manifest")
        return self


class PublicationSummary(ApiModel):
    """Exact active-point verification result for an immutable revision."""

    id: CanonicalUuid
    prepared_revision_id: CanonicalUuid
    active_qdrant_collection: str = Field(min_length=1, max_length=255)
    status: PublicationStatus
    expected_points: int = Field(ge=0)
    verified_points: int | None = Field(default=None, ge=0)
    payload_revision_verified: bool
    vector_schema_verified: bool
    screening_zero_verified: bool
    failure: SanitizedFailure | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    verified_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def verified_publication_is_complete(self) -> Self:
        if self.status is PublicationStatus.VERIFIED:
            if (
                self.verified_points != self.expected_points
                or not self.payload_revision_verified
                or not self.vector_schema_verified
                or not self.screening_zero_verified
                or self.verified_at is None
            ):
                raise ValueError("verified publication requires every exact verification proof")
        return self


class DeletionSummary(ApiModel):
    """Restart-safe point and storage cleanup progress."""

    terminal_disposition: TerminalDisposition
    phase: DeletionPhase
    active_qdrant_collection: str = Field(min_length=1, max_length=255)
    screening_qdrant_collection: str = Field(min_length=1, max_length=255)
    attempts: int = Field(ge=0)
    active_zero_verified_at: UtcTimestamp | None = None
    screening_zero_verified_at: UtcTimestamp | None = None
    storage_purged_at: UtcTimestamp | None = None
    tombstoned_at: UtcTimestamp | None = None
    failure: SanitizedFailure | None = None
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def physical_targets_are_distinct(self) -> Self:
        if self.active_qdrant_collection == self.screening_qdrant_collection:
            raise ValueError("active and screening deletion targets must be distinct")
        return self


class ReplacementSummary(ApiModel):
    """Old/new linkage and current durable replacement progress."""

    decision_id: CanonicalUuid
    old_document_id: CanonicalUuid
    new_document_id: CanonicalUuid
    old_document_state: DocumentState
    new_document_state: DocumentState
    operation_id: CanonicalUuid | None = None
    phase: OperationPhase | None = None
    completed_at: UtcTimestamp | None = None


class DecisionSummary(ApiModel):
    """Immutable decision bound to an exact prepared manifest."""

    id: CanonicalUuid
    prepared_revision_id: CanonicalUuid
    prepared_manifest_sha256: Sha256
    action: DecisionAction
    target_document_id: CanonicalUuid | None = None
    actor_type: str = Field(min_length=1, max_length=50)
    actor_id: str = Field(min_length=1, max_length=255)
    created_at: UtcTimestamp


class DocumentSummary(ApiModel):
    """Immutable source identity plus current lifecycle state for lists."""

    id: CanonicalUuid
    collection_key: CollectionKey
    original_filename: str = Field(min_length=1, max_length=255)
    content_type: Literal["application/pdf"]
    size_bytes: int = Field(ge=1)
    sha256: Sha256
    created_by: str = Field(min_length=1, max_length=255)
    state: DocumentState
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    ready_at: UtcTimestamp | None = None
    failure: SanitizedFailure | None = None
    allowed_actions: list[AllowedAction] = Field(default_factory=list, max_length=5)

    @field_validator("allowed_actions")
    @classmethod
    def allowed_actions_are_unique(cls, value: list[AllowedAction]) -> list[AllowedAction]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_actions cannot contain duplicates")
        return value


class DocumentDetail(DocumentSummary):
    """Complete operator inspection view without protected artifacts."""

    source: SourceMetadata
    terminal_disposition: TerminalDisposition | None = None
    current_operation: OperationSummary | None = None
    prepared_revision: PreparedRevisionSummary | None = None
    publication: PublicationSummary | None = None
    deletion: DeletionSummary | None = None
    replacement: ReplacementSummary | None = None
    decision: DecisionSummary | None = None


class DocumentListResponse(CursorPage[DocumentSummary]):
    """Cursor page of documents."""


class UploadAcceptedResponse(ApiModel):
    """Durable 202 result after bounded admission and enqueue."""

    document: DocumentSummary
    operation: OperationSummary
    idempotent_replay: bool = False


class MarkdownPage(ApiModel):
    """Validated page-scoped Markdown and provenance."""

    page_number: int = Field(ge=1)
    markdown: str = Field(min_length=1, max_length=5_000_000)
    markdown_sha256: Sha256
    source_projection_sha256: Sha256
    markdown_projection_sha256: Sha256
    slice_count: int = Field(ge=1)


class MarkdownDocument(ApiModel):
    """Canonical document Markdown with an exact page map."""

    document_id: CanonicalUuid
    prepared_revision_id: CanonicalUuid
    markdown_sha256: Sha256
    markdown: str = Field(min_length=1, max_length=5_000_000)
    pages: list[MarkdownPage] = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def pages_are_complete_and_ordered(self) -> Self:
        if [page.page_number for page in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError("Markdown pages must be complete and in one-based order")
        return self


class Chunk(ApiModel):
    """One public Markdown chunk; numeric vector values are absent by design."""

    id: CanonicalUuid
    prepared_revision_id: CanonicalUuid
    chunk_index: int = Field(ge=0)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    heading_path: list[str] = Field(default_factory=list, max_length=32)
    token_count: int = Field(ge=1, le=384)
    text_sha256: Sha256
    markdown: str = Field(min_length=1, max_length=100_000)

    @field_validator("heading_path")
    @classmethod
    def headings_are_bounded(cls, value: list[str]) -> list[str]:
        if any(not heading.strip() or len(heading) > 500 for heading in value):
            raise ValueError("heading path entries must be non-blank and at most 500 characters")
        return value

    @model_validator(mode="after")
    def page_range_is_ordered(self) -> Self:
        if self.page_end < self.page_start:
            raise ValueError("page_end cannot precede page_start")
        return self


class ChunkListResponse(CursorPage[Chunk]):
    """Paged public chunks for one immutable revision."""

    document_id: CanonicalUuid
    prepared_revision_id: CanonicalUuid


class EvidenceCitation(ApiModel):
    """Bounded source-backed citation from validated advisory evidence."""

    document_id: CanonicalUuid
    chunk_id: CanonicalUuid
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    excerpt: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def page_range_is_ordered(self) -> Self:
        if self.page_end < self.page_start:
            raise ValueError("page_end cannot precede page_start")
        return self


class PreflightEvidence(ApiModel):
    """Validated deterministic/advisory finding without raw provider output."""

    id: CanonicalUuid
    kind: EvidenceKind
    model_id: ModelIdentity | None = None
    valid: bool
    label: str | None = Field(default=None, min_length=1, max_length=64)
    summary: str | None = Field(default=None, min_length=1, max_length=2_000)
    citations: list[EvidenceCitation] = Field(default_factory=list, max_length=50)
    failure_code: FailureCode | None = None
    evidence_sha256: Sha256
    created_at: UtcTimestamp


class CandidateDocumentReference(ApiModel):
    """Bounded immutable candidate snapshot."""

    id: CanonicalUuid
    collection_key: CollectionKey
    original_filename: str = Field(min_length=1, max_length=255)
    state: DocumentState
    sha256: Sha256


class PreflightCandidate(ApiModel):
    """One collection-scoped deterministic candidate and retained evidence."""

    id: CanonicalUuid
    document: CandidateDocumentReference
    source: CandidateSource
    rank: int = Field(ge=1)
    reasons: list[FailureCode] = Field(min_length=1, max_length=50)
    max_cosine: float = Field(ge=-1, le=1, allow_inf_nan=False)
    bm25_score: float = Field(ge=0, allow_inf_nan=False)
    fused_score: float = Field(ge=0, allow_inf_nan=False)
    matched_chunk_pair_count: int = Field(ge=0)
    replacement_eligible: bool
    evidence: list[PreflightEvidence] = Field(default_factory=list, max_length=10)


class PreflightCandidatePage(CursorPage[PreflightCandidate]):
    """Cursor page nested in a preflight inspection response."""


class PreflightResponse(ApiModel):
    """Current immutable prepared revision, completeness, and candidates."""

    document_id: CanonicalUuid
    prepared_revision: PreparedRevisionSummary
    completeness: PreflightCompleteness
    candidate_count: int = Field(ge=0)
    candidates: PreflightCandidatePage

    @model_validator(mode="after")
    def candidate_total_covers_page(self) -> Self:
        if self.candidate_count < len(self.candidates.items):
            raise ValueError("candidate_count cannot be smaller than the returned candidate page")
        return self


class DecisionRequest(ApiModel):
    """Strict operator decision bound to exactly one prepared revision."""

    prepared_revision_id: CanonicalUuid
    action: DecisionAction
    target_document_id: CanonicalUuid | None = None

    @model_validator(mode="after")
    def replacement_target_matches_action(self) -> Self:
        if self.action is DecisionAction.REPLACE and self.target_document_id is None:
            raise ValueError("REPLACE requires target_document_id")
        if self.action is not DecisionAction.REPLACE and self.target_document_id is not None:
            raise ValueError("only REPLACE accepts target_document_id")
        return self


class RetryRequest(ApiModel):
    """Retry has no mutable body fields; idempotency is carried by the header."""


class MutationResponse(ApiModel):
    """Durable 202 response shared by decisions, retries, and deletes."""

    document: DocumentSummary
    operation: OperationSummary
    idempotent_replay: bool = False


AuditText = Annotated[str, StringConstraints(max_length=500)]
AuditScalar = AuditText | int | bool | None


class AuditEventResponse(ApiModel):
    """Content-free append-only lifecycle event."""

    id: int = Field(ge=1)
    document_id: CanonicalUuid | None = None
    operation_id: CanonicalUuid | None = None
    event_type: str = Field(min_length=1, max_length=100)
    actor_type: str = Field(min_length=1, max_length=50)
    actor_id: str = Field(min_length=1, max_length=255)
    occurred_at: UtcTimestamp
    attributes: dict[str, AuditScalar] = Field(default_factory=dict, max_length=50)


class EventListResponse(CursorPage[AuditEventResponse]):
    """Cursor page of content-free audit events."""

    document_id: CanonicalUuid


class TombstoneSummary(ApiModel):
    """Content-free terminal history retained after purge."""

    id: CanonicalUuid
    document_id: CanonicalUuid
    collection_key: CollectionKey
    disposition: TerminalDisposition
    source_sha256: Sha256
    manifest_sha256: Sha256 | None = None
    reason_code: FailureCode | None = None
    actor_type: str = Field(min_length=1, max_length=50)
    actor_id: str = Field(min_length=1, max_length=255)
    occurred_at: UtcTimestamp


class HistoryQuery(CursorQuery):
    """Terminal-history filters."""

    collection_key: CollectionKey | None = None
    disposition: TerminalDisposition | None = None


class HistoryResponse(CursorPage[TombstoneSummary]):
    """Cursor page of content-free tombstones."""


class HealthCheck(ApiModel):
    """One content-free liveness/readiness component result."""

    component: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9._-]+$")
    status: Literal["READY", "NOT_READY", "DISABLED"]
    failure_code: FailureCode | None = None
    message: str | None = Field(default=None, min_length=1, max_length=500)


class HealthResponse(ApiModel):
    """Process or dependency readiness without secret-bearing diagnostics."""

    status: Literal["OK", "NOT_READY"]
    checks: list[HealthCheck] = Field(default_factory=list, max_length=100)


class SearchMode(str, Enum):
    """Modes accepted by the separately owned retrieval service."""

    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class OperatorSearchRequest(ApiModel):
    """One bounded, single-collection operator diagnostic query."""

    collection_key: CollectionKey
    query: str = Field(min_length=1, max_length=1_000)
    mode: SearchMode = SearchMode.HYBRID
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def query_is_bounded_text(cls, value: str) -> str:
        query = value.strip()
        if not query or any(ord(character) < 32 for character in query):
            raise ValueError("query must be non-blank text without control characters")
        return query


class OperatorSearchHit(ApiModel):
    """Catalog-correlated READY result returned by the operator proxy."""

    rank: int = Field(ge=1, le=100)
    document_id: CanonicalUuid
    prepared_revision_id: CanonicalUuid
    collection_key: CollectionKey
    original_filename: str = Field(min_length=1, max_length=255)
    chunk_id: CanonicalUuid | None = None
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list, max_length=32)
    score: float = Field(allow_inf_nan=False)
    excerpt: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def page_range_is_complete_and_ordered(self) -> Self:
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("search hit page_start and page_end must be supplied together")
        if self.page_start is not None and self.page_end is not None:
            if self.page_end < self.page_start:
                raise ValueError("page_end cannot precede page_start")
        return self


class OperatorSearchResponse(ApiModel):
    """Strict, bounded result correlated to one logical collection."""

    collection_key: CollectionKey
    query: str = Field(min_length=1, max_length=1_000)
    mode: SearchMode
    results: list[OperatorSearchHit] = Field(max_length=100)

    @field_validator("query")
    @classmethod
    def query_is_bounded_text(cls, value: str) -> str:
        query = value.strip()
        if not query or any(ord(character) < 32 for character in query):
            raise ValueError("query must be non-blank text without control characters")
        return query

    @model_validator(mode="after")
    def results_match_scope_and_rank(self) -> Self:
        if any(result.collection_key != self.collection_key for result in self.results):
            raise ValueError("operator search results must match the requested collection")
        ranks = [result.rank for result in self.results]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("operator search result ranks must be complete and ordered")
        document_chunk_pairs = [(result.document_id, result.chunk_id) for result in self.results]
        if len(document_chunk_pairs) != len(set(document_chunk_pairs)):
            raise ValueError("operator search results cannot contain duplicate document chunks")
        if any(not math.isfinite(result.score) for result in self.results):
            raise ValueError("operator search scores must be finite")
        return self
