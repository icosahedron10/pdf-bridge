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

from .models import (
    BatchState,
    DocumentState,
    OperationState,
    OperationType,
    ScanState,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


T = TypeVar("T")


class PaginatedResponse(ApiModel, Generic[T]):
    items: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    pages: int = Field(ge=0)

    @classmethod
    def create(
        cls, items: list[T], *, total: int, page: int, page_size: int
    ) -> PaginatedResponse[T]:
        pages = math.ceil(total / page_size) if total else 0
        return cls(items=items, total=total, page=page, page_size=page_size, pages=pages)


class ProblemFieldError(ApiModel):
    location: list[str]
    message: str
    type: str


class DuplicateMatch(ApiModel):
    document_id: uuid.UUID
    filename: str
    size_bytes: int = Field(ge=0)
    state: DocumentState
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
    errors: list[ProblemFieldError] = Field(default_factory=list)
    duplicate: DuplicateMatch | None = None
    possible_duplicates: list[DuplicateMatch] = Field(default_factory=list, max_length=100)


def problem_responses() -> dict[int | str, dict[str, Any]]:
    """Common OpenAPI declarations for the API's problem-details responses."""

    descriptions = {
        400: "Malformed request",
        401: "Authentication failed",
        403: "Request was not authorized",
        404: "Resource was not found",
        409: "State or duplicate conflict",
        413: "Upload is too large",
        422: "Request validation failed",
        500: "Catalog or storage inconsistency",
        502: "Invalid retrieval service response",
        503: "Required dependency is unavailable",
    }
    return {
        status: {
            "model": ProblemDetail,
            "description": description,
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/ProblemDetail"}
                }
            },
        }
        for status, description in descriptions.items()
    }


class DocumentSummary(ApiModel):
    id: uuid.UUID
    original_filename: str
    normalized_filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: DocumentState
    scan_state: ScanState
    uploaded_at: datetime
    ingested_at: datetime | None = None
    deleted_at: datetime | None = None
    detail_url: str | None = None


class OperationSummary(ApiModel):
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
    document: DocumentSummary


class AuditEventPublic(ApiModel):
    id: int
    event_type: str
    actor_type: str
    actor_id: str
    occurred_at: datetime
    details: dict[str, Any]


class DocumentDetail(DocumentSummary):
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
    pass


class QueueListResponse(PaginatedResponse[QueueOperationSummary]):
    pass


class UploadPreflightRequest(ApiModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)


class UploadPreflightResponse(ApiModel):
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
    document: DocumentSummary
    operation_id: uuid.UUID
    idempotent_replay: bool = False


class DocumentMutationResponse(ApiModel):
    document: DocumentSummary
    operation_id: uuid.UUID | None = None


class DeleteDocumentRequest(ApiModel):
    reason: str | None = Field(default=None, max_length=500)


class SearchMode(str, Enum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class SearchRequest(ApiModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: SearchMode = SearchMode.HYBRID
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must contain non-whitespace characters")
        return normalized


class SearchHit(ApiModel):
    document_id: uuid.UUID
    score: float = Field(allow_inf_nan=False)
    snippet: str = Field(max_length=4000)
    match_metadata: dict[str, Any] | None = None


class SearchResponse(ApiModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: SearchMode
    hits: list[SearchHit] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_documents(self) -> SearchResponse:
        ids = [hit.document_id for hit in self.hits]
        if len(set(ids)) != len(ids):
            raise ValueError("hits must contain at most one result per document")
        return self


class BatchClaimRequest(ApiModel):
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    limit: int = Field(default=100, ge=1, le=500)


class BatchClaimResponse(ApiModel):
    batch_id: uuid.UUID
    request_id: str
    state: BatchState
    claimed_at: datetime
    lease_expires_at: datetime
    operation_count: int = Field(ge=0)
    idempotent_replay: bool = False


class BatchManifestItem(ApiModel):
    operation_id: uuid.UUID
    document_id: uuid.UUID
    operation_type: OperationType
    filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_url: str | None = None


class BatchManifestResponse(ApiModel):
    version: int = Field(default=1, ge=1)
    batch_id: uuid.UUID
    request_id: str
    state: BatchState
    claimed_at: datetime
    lease_expires_at: datetime
    operations: list[BatchManifestItem] = Field(max_length=500)


class BatchStageRequest(ApiModel):
    operation_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_operations(self) -> BatchStageRequest:
        if len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("operation_ids must not contain duplicates")
        return self


class BatchStageResponse(ApiModel):
    batch_id: uuid.UUID
    state: BatchState
    staged_at: datetime
    operation_count: int = Field(ge=1)
    idempotent_replay: bool = False


class ComponentState(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class PipelineComponents(ApiModel):
    pdf_source: ComponentState
    markdown: ComponentState
    bm25: ComponentState
    dense: ComponentState


class OperationResultInput(ApiModel):
    operation_id: uuid.UUID
    success: bool
    chunk_count: int | None = Field(default=None, ge=0)
    components: PipelineComponents
    error: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def consistent_result(self) -> OperationResultInput:
        component_states = (
            self.components.pdf_source,
            self.components.markdown,
            self.components.bm25,
            self.components.dense,
        )
        if self.success and any(state != ComponentState.SUCCEEDED for state in component_states):
            raise ValueError("a successful result requires every component to succeed")
        if self.success and self.error:
            raise ValueError("a successful result cannot include an error")
        if not self.success and not self.error:
            raise ValueError("a failed result must include an error")
        return self


class BatchResultsRequest(ApiModel):
    pipeline_run_id: str = Field(min_length=1, max_length=255)
    results: list[OperationResultInput] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_results(self) -> BatchResultsRequest:
        operation_ids = [result.operation_id for result in self.results]
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("results must contain exactly one entry per operation")
        return self


class BatchResultsResponse(ApiModel):
    batch_id: uuid.UUID
    state: BatchState
    completed_at: datetime
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    idempotent_replay: bool = False


class HealthResponse(ApiModel):
    status: str
    checks: dict[str, str] = Field(default_factory=dict)


class HistoricalManifestDocument(ApiModel):
    path: str = Field(min_length=1, max_length=2000)
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    ingested_at: datetime | None = None
    chunk_count: int | None = Field(default=None, ge=0)
    pipeline_run_id: str | None = Field(default=None, max_length=255)


class HistoricalImportManifest(ApiModel):
    version: Literal[1]
    documents: list[HistoricalManifestDocument] = Field(min_length=1, max_length=10_000)


class HistoricalImportItemResult(ApiModel):
    filename: str
    sha256: str
    size_bytes: int = Field(ge=1)
    document_id: uuid.UUID | None = None


class HistoricalImportResponse(ApiModel):
    dry_run: bool
    imported: int = Field(ge=0)
    items: list[HistoricalImportItemResult]
