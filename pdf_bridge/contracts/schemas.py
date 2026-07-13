"""Strict Pydantic contracts for browser, retrieval, and Jenkins APIs."""

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
    BatchState,
    DocumentState,
    OperationState,
    OperationType,
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
    """Existing document surfaced as an exact or possible duplicate."""

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
    possible_duplicates: list[DuplicateMatch] = Field(default_factory=list, max_length=100)


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
    deleted_at: datetime | None = None
    detail_url: str | None = None


class OperationSummary(ApiModel):
    """Public summary of one queued pipeline operation."""

    id: uuid.UUID
    operation_type: OperationType
    state: OperationState
    attempt: int = Field(ge=1)
    created_at: datetime
    claimed_at: datetime | None = None
    staged_at: datetime | None = None
    completed_at: datetime | None = None
    pipeline_run_id: str | None = None
    chunk_count: int | None = Field(default=None, ge=0)
    component_results: list[dict[str, Any]] | None = None
    error: str | None = None


class QueueOperationSummary(OperationSummary):
    """Queued operation summary paired with its document."""

    document: DocumentSummary


class AuditEventPublic(ApiModel):
    """Public representation of an append-only audit event."""

    id: int
    event_type: str
    actor_type: str
    actor_id: str
    occurred_at: datetime
    details: dict[str, Any]


class DocumentDetail(DocumentSummary):
    """Detailed document response including pipeline and audit history."""

    content_type: str
    scan_engine: str | None = None
    scan_signature: str | None = None
    scanned_at: datetime | None = None
    uploader_identity: str
    updated_at: datetime
    cancelled_at: datetime | None = None
    pipeline_run_id: str | None = None
    chunk_count: int | None = Field(default=None, ge=0)
    pipeline_metadata: dict[str, Any] | None = None
    last_error: str | None = None
    operations: list[OperationSummary] = Field(default_factory=list)
    audit_events: list[AuditEventPublic] = Field(default_factory=list)


class DocumentListResponse(PaginatedResponse[DocumentSummary]):
    """Paginated document catalog response."""


class QueueListResponse(PaginatedResponse[QueueOperationSummary]):
    """Paginated queue operation response."""


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


class UploadPreflightRequest(ApiModel):
    """Filename, size, and collection submitted before an upload."""

    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    collection_key: CollectionKey


class UploadPreflightResponse(ApiModel):
    """Normalized upload metadata and any possible duplicate warnings."""

    normalized_filename: str
    requires_confirmation: bool
    possible_duplicates: list[DuplicateMatch] = Field(default_factory=list, max_length=100)


IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class UploadResponse(ApiModel):
    """Registered upload and its newly queued ingest operation."""

    document: DocumentSummary
    operation_id: uuid.UUID
    idempotent_replay: bool = False


class DocumentMutationResponse(ApiModel):
    """Document state returned after a lifecycle mutation."""

    document: DocumentSummary
    operation_id: uuid.UUID | None = None


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


class BatchClaimRequest(ApiModel):
    """Idempotent Jenkins request to lease queued operations."""

    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    limit: int = Field(default=100, ge=1, le=500)


class BatchClaimResponse(ApiModel):
    """Lease metadata for a claimed Jenkins batch."""

    batch_id: uuid.UUID
    request_id: str
    state: BatchState
    claimed_at: datetime
    lease_expires_at: datetime
    operation_count: int = Field(ge=0)
    idempotent_replay: bool = False


class BatchManifestItem(ApiModel):
    """One operation and canonical staging path in a batch manifest."""

    operation_id: uuid.UUID
    document_id: uuid.UUID
    operation_type: OperationType
    filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_key: CollectionKey
    relative_path: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^pdfs/[a-z0-9][a-z0-9_-]*/[0-9a-f-]{36}\.pdf$",
    )
    download_url: str | None = None


class BatchManifestResponse(ApiModel):
    """Versioned manifest of operations leased to a Jenkins batch."""

    version: Literal[2] = 2
    batch_id: uuid.UUID
    request_id: str
    state: BatchState
    claimed_at: datetime
    lease_expires_at: datetime
    operations: list[BatchManifestItem] = Field(max_length=500)


class BatchStageRequest(ApiModel):
    """Operation identifiers acknowledged as durably staged by Jenkins."""

    operation_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_operations(self) -> BatchStageRequest:
        """Reject duplicate operation identifiers in a staging acknowledgement."""

        if len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("operation_ids must not contain duplicates")
        return self


class BatchStageResponse(ApiModel):
    """Batch state after its staged operations are acknowledged."""

    batch_id: uuid.UUID
    state: BatchState
    staged_at: datetime
    operation_count: int = Field(ge=1)
    idempotent_replay: bool = False


class ComponentState(str, Enum):
    """Outcome reported for an individual pipeline component."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class PipelineComponents(ApiModel):
    """Component-level results required for a pipeline operation."""

    pdf_source: ComponentState
    markdown: ComponentState
    bm25: ComponentState
    dense: ComponentState


class OperationResultInput(ApiModel):
    """Validated pipeline result for one queued operation."""

    operation_id: uuid.UUID
    success: bool
    chunk_count: int | None = Field(default=None, ge=0)
    components: PipelineComponents
    error: str | None = Field(default=None, max_length=4000)

    @field_validator("error")
    @classmethod
    def normalize_error(cls, value: str | None) -> str | None:
        """Trim a reported error while preserving an absent value."""

        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("error must contain non-whitespace characters")
        return normalized

    @model_validator(mode="after")
    def consistent_result(self) -> OperationResultInput:
        """Enforce agreement between success, components, and errors."""

        component_states = (
            self.components.pdf_source,
            self.components.markdown,
            self.components.bm25,
            self.components.dense,
        )
        if self.success and any(
            state != ComponentState.SUCCEEDED for state in component_states
        ):
            raise ValueError("a successful result requires every component to succeed")
        if self.success and self.error:
            raise ValueError("a successful result cannot include an error")
        if not self.success and not self.error:
            raise ValueError("a failed result must include an error")
        return self


class BatchResultsRequest(ApiModel):
    """Versioned pipeline results submitted for a staged batch."""

    pipeline_run_id: str = Field(min_length=1, max_length=255)
    results: list[OperationResultInput] = Field(min_length=1, max_length=500)

    @field_validator("pipeline_run_id")
    @classmethod
    def normalize_pipeline_run_id(cls, value: str) -> str:
        """Trim and validate the reporting pipeline run identifier."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("pipeline_run_id must contain non-whitespace characters")
        return normalized

    @model_validator(mode="after")
    def unique_results(self) -> BatchResultsRequest:
        """Require exactly one result for every reported operation."""

        operation_ids = [result.operation_id for result in self.results]
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("results must contain exactly one entry per operation")
        return self


class BatchResultsResponse(ApiModel):
    """Final batch state and aggregate operation outcome counts."""

    batch_id: uuid.UUID
    state: BatchState
    completed_at: datetime
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    idempotent_replay: bool = False


class HealthResponse(ApiModel):
    """Process or dependency health status returned by readiness endpoints."""

    status: str
    checks: dict[str, str] = Field(default_factory=dict)


class HistoricalManifestDocument(ApiModel):
    """One trusted source document declared by a historical import manifest."""

    path: str = Field(min_length=1, max_length=2000)
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    collection_key: CollectionKey
    ingested_at: datetime | None = None
    chunk_count: int | None = Field(default=None, ge=0)
    pipeline_run_id: str | None = Field(default=None, max_length=255)


class HistoricalImportManifest(ApiModel):
    """Versioned collection of documents for controlled historical import."""

    version: Literal[2]
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
