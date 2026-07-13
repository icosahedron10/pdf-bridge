"""Strict Pydantic contracts for the browser API and retrieval integration."""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from pdf_bridge.persistence.models import (
    AnalysisStatus,
    DecisionAction,
    DocumentState,
    OperationPhase,
    OperationState,
    OperationType,
    ReplacementState,
    ScanState,
)

CollectionKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$",
    ),
]

IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class ApiModel(BaseModel):
    """Strict base model for public API request and response contracts."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


T = TypeVar("T")


class PaginatedResponse(ApiModel, Generic[T]):
    """Generic page of API resources with computed pagination metadata."""

    items: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    pages: int = Field(ge=0)

    @classmethod
    def create(
        cls, items: list[T], *, total: int, page: int, page_size: int
    ) -> PaginatedResponse[T]:
        """Build a page and derive its total page count."""

        pages = math.ceil(total / page_size) if total else 0
        return cls(items=items, total=total, page=page, page_size=page_size, pages=pages)


class DuplicateMatch(ApiModel):
    """Existing document surfaced by a duplicate or filename warning."""

    document_id: uuid.UUID
    filename: str
    size_bytes: int = Field(ge=0)
    state: DocumentState
    collection_key: CollectionKey
    detail_url: str


class ProblemDetail(ApiModel):
    """RFC 9457-style error response with stable machine-readable extensions."""

    type: str = "about:blank"
    title: str
    status: int = Field(ge=400, le=599)
    detail: str
    instance: str | None = None
    code: str
    request_id: str | None = None
    duplicate: DuplicateMatch | None = None


class DocumentSummary(ApiModel):
    """Public catalog summary for a document and its current lifecycle state."""

    id: uuid.UUID
    original_filename: str
    normalized_filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: DocumentState
    scan_state: ScanState
    collection_key: CollectionKey
    uploaded_at: datetime
    ingested_at: datetime | None = None
    rejected_at: datetime | None = None
    deleted_at: datetime | None = None
    detail_url: str | None = None


class OperationSummary(ApiModel):
    """Public summary of one durable worker operation."""

    id: uuid.UUID
    operation_type: OperationType
    state: OperationState
    phase: OperationPhase
    attempt: int = Field(ge=1)
    retryable: bool
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class AuditEventPublic(ApiModel):
    """Public representation of an append-only audit event."""

    id: int
    event_type: str
    actor_type: str
    actor_id: str
    occurred_at: datetime
    details: dict[str, Any]


class FilenameWarningPublic(ApiModel):
    """Advisory collection-scoped filename-family warning."""

    kind: Literal[
        "filename-family",
        "token-set-similarity",
        "jaro-winkler-similarity",
    ]
    similarity: float = Field(ge=0.0, le=1.0)
    shared_tokens: list[str] = Field(default_factory=list, max_length=50)
    matched: DuplicateMatch


class AnalysisSummary(ApiModel):
    """Completeness and result overview of one analysis revision."""

    id: uuid.UUID
    revision: int = Field(ge=1)
    status: AnalysisStatus
    pipeline_fingerprint: str | None = None
    page_count: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=0)
    filename_warnings: list[FilenameWarningPublic] = Field(default_factory=list)
    semantic_complete: bool
    classification_complete: bool
    incomplete_reasons: list[str] = Field(default_factory=list)
    auto_ingest_eligible: bool
    candidate_count: int = Field(ge=0)
    classified_count: int = Field(ge=0)
    overflow_count: int = Field(ge=0)
    created_at: datetime
    completed_at: datetime | None = None


class DecisionSummary(ApiModel):
    """Immutable record of one operator decision."""

    id: uuid.UUID
    action: DecisionAction
    analysis_revision: int = Field(ge=1)
    target_document_id: uuid.UUID | None = None
    advisory_override: bool
    actor_id: str
    created_at: datetime


class ReplacementSummary(ApiModel):
    """Progress of a replacement workflow attached to an upload."""

    id: uuid.UUID
    old_document_id: uuid.UUID
    new_document_id: uuid.UUID
    state: ReplacementState
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class UploadResource(ApiModel):
    """One durable upload workspace row: document, operation, and analysis."""

    upload_id: uuid.UUID
    document: DocumentSummary
    operation: OperationSummary | None = None
    analysis: AnalysisSummary | None = None
    replacement: ReplacementSummary | None = None
    decision: DecisionSummary | None = None
    review_required: bool
    open: bool
    status_url: str
    analysis_url: str | None = None


class UploadAcceptedResponse(ApiModel):
    """202 response for an accepted upload queued for analysis."""

    upload: UploadResource
    idempotent_replay: bool = False


class UploadListResponse(PaginatedResponse[UploadResource]):
    """Paginated upload workspace response."""


class UploadPreflightRequest(ApiModel):
    """Filename, size, and collection submitted before an upload."""

    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    collection_key: CollectionKey


class UploadPreflightResponse(ApiModel):
    """Normalized upload metadata with typed advisory filename warnings."""

    normalized_filename: str
    warnings: list[FilenameWarningPublic] = Field(default_factory=list, max_length=100)


class ChunkExcerptPublic(ApiModel):
    """Page-referenced excerpt from a retained analysis chunk."""

    chunk_reference: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    text: str = Field(max_length=4000)


class FindingPublic(ApiModel):
    """Validated, explanation-only LLM finding for one candidate."""

    role: Literal["classifier", "verifier"]
    model_id: str
    valid: bool
    label: (
        Literal[
            "near_duplicate",
            "likely_revision",
            "potential_contradiction",
            "consistent_overlap",
            "unrelated",
            "uncertain",
        ]
        | None
    ) = None
    summary: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class CandidatePublic(ApiModel):
    """One qualifying candidate with its deterministic and LLM evidence."""

    candidate_id: uuid.UUID
    document: DuplicateMatch
    source: Literal["active", "screening"]
    rank: int = Field(ge=1)
    reasons: list[str]
    max_cosine: float
    strong_cosine_chunks: int = Field(ge=0)
    moderate_cosine_chunks: int = Field(ge=0)
    bm25_strong_placements: int = Field(ge=0)
    fused_score: float
    classified: bool
    overflow: bool
    replacement_eligible: bool
    findings: list[FindingPublic] = Field(default_factory=list)
    incoming_excerpts: list[ChunkExcerptPublic] = Field(default_factory=list)
    candidate_excerpts: list[ChunkExcerptPublic] = Field(default_factory=list)


class AnalysisDetailResponse(ApiModel):
    """Paginated candidate evidence for one upload's current analysis."""

    upload_id: uuid.UUID
    analysis: AnalysisSummary
    candidates: list[CandidatePublic]
    total_candidates: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    pages: int = Field(ge=0)


class DecisionRequest(ApiModel):
    """Operator decision submitted against a specific analysis revision."""

    analysis_revision: int = Field(ge=1)
    action: Literal["keep", "replace", "cancel"]
    target_document_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> DecisionRequest:
        """Replace requires a target; Keep and Cancel forbid one."""

        if self.action == "replace" and self.target_document_id is None:
            raise ValueError("replace decisions require target_document_id")
        if self.action != "replace" and self.target_document_id is not None:
            raise ValueError("only replace decisions accept target_document_id")
        return self


class DocumentMutationResponse(ApiModel):
    """Document state returned after a lifecycle mutation."""

    document: DocumentSummary
    operation_id: uuid.UUID | None = None
    idempotent_replay: bool = False


class DocumentDetail(DocumentSummary):
    """Detailed document response including analysis and audit history."""

    content_type: str
    scan_engine: str | None = None
    scan_signature: str | None = None
    scanned_at: datetime | None = None
    uploader_identity: str
    updated_at: datetime
    cancelled_at: datetime | None = None
    page_count: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=0)
    text_sha256: str | None = None
    analysis_revision: int = Field(ge=0)
    analysis_manifest_hash: str | None = None
    rejection_reason: str | None = None
    replaced_by_document_id: uuid.UUID | None = None
    last_error: str | None = None
    analysis: AnalysisSummary | None = None
    decisions: list[DecisionSummary] = Field(default_factory=list)
    operations: list[OperationSummary] = Field(default_factory=list)
    audit_events: list[AuditEventPublic] = Field(default_factory=list)


class DocumentListResponse(PaginatedResponse[DocumentSummary]):
    """Paginated document catalog response."""


class CollectionSummary(ApiModel):
    """Configured collection metadata with current catalog counts."""

    key: CollectionKey
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2_000)
    audience: Literal["customer", "internal"]
    available_documents: int = Field(ge=0)
    processing_documents: int = Field(ge=0)
    detail_url: str


class CollectionListResponse(ApiModel):
    """Complete list of collections configured for the deployment."""

    items: list[CollectionSummary]
    total: int = Field(ge=0)


class DeleteDocumentRequest(ApiModel):
    """Optional operator reason for requesting document deletion."""

    reason: str | None = Field(default=None, max_length=500)


class SearchMode(str, Enum):
    """Retrieval strategy supported by the external search service."""

    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class SearchRequest(ApiModel):
    """Correlated retrieval request constrained to configured collections."""

    query: str = Field(min_length=1, max_length=1000)
    mode: SearchMode = SearchMode.HYBRID
    collections: list[CollectionKey] = Field(min_length=1, max_length=50)
    include_hits: bool = True
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """Trim the search query and reject whitespace-only values."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("query must contain non-whitespace characters")
        return normalized

    @model_validator(mode="after")
    def validate_scope(self) -> SearchRequest:
        """Enforce unique collections and a valid hit-producing scope."""

        if len(set(self.collections)) != len(self.collections):
            raise ValueError("collections must not contain duplicates")
        if self.include_hits and len(self.collections) != 1:
            raise ValueError("hit-producing searches require exactly one collection")
        return self


class SearchHit(ApiModel):
    """One ranked retrieval hit for a catalog document."""

    document_id: uuid.UUID
    score: float = Field(allow_inf_nan=False)
    snippet: str = Field(max_length=4000)
    match_metadata: dict[str, Any] | None = None


class CollectionSearchGroup(ApiModel):
    """Search totals and optional hits for a single collection."""

    collection_key: CollectionKey
    total: int = Field(ge=0, strict=True)
    hits: list[SearchHit] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_documents(self) -> CollectionSearchGroup:
        """Reject duplicate documents and hit counts above the reported total."""

        ids = [hit.document_id for hit in self.hits]
        if len(set(ids)) != len(ids):
            raise ValueError("hits must contain at most one result per document")
        if len(self.hits) > self.total:
            raise ValueError("hit count cannot exceed total")
        return self


class SearchResponse(ApiModel):
    """Correlated retrieval response grouped by collection."""

    query: str = Field(min_length=1, max_length=1000)
    mode: SearchMode
    groups: list[CollectionSearchGroup] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_groups(self) -> SearchResponse:
        """Require at most one response group per collection."""

        keys = [group.collection_key for group in self.groups]
        if len(set(keys)) != len(keys):
            raise ValueError("groups must contain each collection at most once")
        return self


class HealthResponse(ApiModel):
    """Process or dependency health status returned by readiness endpoints."""

    status: str
    checks: dict[str, str] = Field(default_factory=dict)


class HistoricalManifestDocument(ApiModel):
    """One trusted source document declared by a historical import manifest."""

    path: str = Field(min_length=1, max_length=2000)
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    collection_key: CollectionKey


class HistoricalImportManifest(ApiModel):
    """Version-3 manifest creating normal analysis operations on import."""

    version: Literal[3]
    documents: list[HistoricalManifestDocument] = Field(min_length=1, max_length=10_000)


class HistoricalImportItemResult(ApiModel):
    """Validated or imported historical document result."""

    filename: str
    sha256: str
    size_bytes: int = Field(ge=1)
    collection_key: CollectionKey
    document_id: uuid.UUID | None = None


class HistoricalImportResponse(ApiModel):
    """Aggregate outcome of a historical import or dry run."""

    dry_run: bool
    imported: int = Field(ge=0)
    items: list[HistoricalImportItemResult]
