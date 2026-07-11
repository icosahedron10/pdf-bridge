"""Strict Pydantic contracts for browser, retrieval, and Jenkins APIs."""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Generic, Literal, TypeVar

from litestar.openapi.datastructures import ResponseSpec
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
    LanguageCode,
    LanguageStatus,
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


class DuplicateMatch(ApiModel):
    document_id: uuid.UUID
    filename: str
    size_bytes: int = Field(ge=0)
    state: DocumentState
    collection_key: str | None = None
    language: LanguageCode = LanguageCode.UND
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


def problem_responses() -> dict[int, ResponseSpec]:
    """Common OpenAPI declarations for the API's problem-details responses."""

    descriptions = {
        401: "Authentication failed",
        403: "Request was not authorized",
        404: "Resource was not found",
        409: "State or duplicate conflict",
        413: "Upload is too large",
        422: "Request was deliberately rejected",
        500: "Catalog or storage inconsistency",
        502: "Invalid retrieval service response",
        503: "Required dependency is unavailable",
    }
    return {
        status: ResponseSpec(
            data_container=ProblemDetail,
            description=description,
            media_type="application/problem+json",
            generate_examples=False,
        )
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
    collection_key: str | None = None
    language: LanguageCode = LanguageCode.UND
    language_status: LanguageStatus = LanguageStatus.PENDING
    language_method: str | None = None
    language_confidence: float | None = Field(default=None, ge=0, le=1)
    language_reason: str | None = None
    language_detected_at: datetime | None = None
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


class CollectionLanguageCounts(ApiModel):
    en: int = Field(ge=0)
    fr: int = Field(ge=0)
    und: int = Field(ge=0)


class CollectionSummary(ApiModel):
    key: CollectionKey
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2_000)
    audience: Literal["customer", "internal"]
    available_documents: int = Field(ge=0)
    processing_documents: int = Field(ge=0)
    review_documents: int = Field(ge=0)
    languages: CollectionLanguageCounts
    detail_url: str


class CollectionListResponse(ApiModel):
    items: list[CollectionSummary]
    total: int = Field(ge=0)


class UploadPreflightRequest(ApiModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    collection_key: CollectionKey


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


class DetectClassificationRequest(ApiModel):
    action: Literal["detect"]
    collection_key: CollectionKey


class OverrideClassificationRequest(ApiModel):
    action: Literal["override"]
    collection_key: CollectionKey | None = None
    language: Literal[LanguageCode.EN, LanguageCode.FR]
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must contain non-whitespace characters")
        return normalized


ClassificationRequest = Annotated[
    DetectClassificationRequest | OverrideClassificationRequest,
    Field(discriminator="action"),
]


class SearchMode(str, Enum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class SearchRequest(ApiModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: SearchMode = SearchMode.HYBRID
    collections: list[CollectionKey] = Field(min_length=1, max_length=50)
    language: LanguageCode | None = None
    include_hits: bool = True
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must contain non-whitespace characters")
        return normalized


    @model_validator(mode="after")
    def validate_scope(self) -> SearchRequest:
        if len(set(self.collections)) != len(self.collections):
            raise ValueError("collections must not contain duplicates")
        if self.include_hits and len(self.collections) != 1:
            raise ValueError("hit-producing searches require exactly one collection")
        if self.language == LanguageCode.UND:
            raise ValueError("search language must be en or fr")
        return self


class SearchHit(ApiModel):
    document_id: uuid.UUID
    score: float = Field(allow_inf_nan=False)
    snippet: str = Field(max_length=4000)
    match_metadata: dict[str, Any] | None = None


class CollectionSearchGroup(ApiModel):
    collection_key: CollectionKey
    total: int = Field(ge=0, strict=True)
    hits: list[SearchHit] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_documents(self) -> CollectionSearchGroup:
        ids = [hit.document_id for hit in self.hits]
        if len(set(ids)) != len(ids):
            raise ValueError("hits must contain at most one result per document")
        if len(self.hits) > self.total:
            raise ValueError("hit count cannot exceed total")
        return self


class SearchResponse(ApiModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: SearchMode
    language: LanguageCode | None = None
    groups: list[CollectionSearchGroup] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_groups(self) -> SearchResponse:
        keys = [group.collection_key for group in self.groups]
        if len(set(keys)) != len(keys):
            raise ValueError("groups must contain each collection at most once")
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
    collection_key: CollectionKey
    language: LanguageCode
    classification_required: bool
    relative_path: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^pdfs/(?:und|en|fr)/[a-z0-9][a-z0-9_-]*/[0-9a-f-]{36}\.pdf$",
    )
    download_url: str | None = None


class BatchManifestResponse(ApiModel):
    version: Literal[2] = 2
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


class PipelineOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REVIEW_REQUIRED = "review_required"


class LanguageResultStatus(str, Enum):
    DETECTED = "detected"
    REVIEW_REQUIRED = "review_required"
    OVERRIDDEN = "overridden"


class LanguageReviewReason(str, Enum):
    NO_TEXT = "no_text"
    OCR_REQUIRED = "ocr_required"
    ENCRYPTED = "encrypted"
    BILINGUAL = "bilingual"
    UNSUPPORTED = "unsupported"
    LOW_CONFIDENCE = "low_confidence"


class LanguageClassificationResult(ApiModel):
    language: LanguageCode
    status: LanguageResultStatus
    method: str = Field(min_length=1, max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)
    reason: LanguageReviewReason | None = None

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("classification method must contain non-whitespace characters")
        return normalized

    @model_validator(mode="after")
    def consistent_classification(self) -> LanguageClassificationResult:
        if self.status == LanguageResultStatus.REVIEW_REQUIRED:
            if self.language != LanguageCode.UND or self.reason is None:
                raise ValueError("review-required classification must be und with a reason")
        elif self.language == LanguageCode.UND or self.reason is not None:
            raise ValueError("detected or overridden classification must be en/fr without reason")
        return self


class OperationResultInput(ApiModel):
    operation_id: uuid.UUID
    outcome: PipelineOutcome
    chunk_count: int | None = Field(default=None, ge=0)
    components: PipelineComponents
    classification: LanguageClassificationResult | None = None
    error: str | None = Field(default=None, max_length=4000)

    @field_validator("error")
    @classmethod
    def normalize_error(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("error must contain non-whitespace characters")
        return normalized

    @model_validator(mode="after")
    def consistent_result(self) -> OperationResultInput:
        component_states = (
            self.components.pdf_source,
            self.components.markdown,
            self.components.bm25,
            self.components.dense,
        )
        if self.outcome == PipelineOutcome.SUCCEEDED and any(
            state != ComponentState.SUCCEEDED for state in component_states
        ):
            raise ValueError("a successful result requires every component to succeed")
        if self.outcome == PipelineOutcome.SUCCEEDED and self.error:
            raise ValueError("a successful result cannot include an error")
        if self.outcome == PipelineOutcome.FAILED and not self.error:
            raise ValueError("a failed result must include an error")
        if (
            self.outcome != PipelineOutcome.REVIEW_REQUIRED
            and self.classification is not None
            and self.classification.status == LanguageResultStatus.REVIEW_REQUIRED
        ):
            raise ValueError(
                "review-required classification may only accompany a review-required outcome"
            )
        if self.outcome == PipelineOutcome.REVIEW_REQUIRED:
            if self.error:
                raise ValueError("a review-required result cannot include an operational error")
            if (
                self.classification is None
                or self.classification.status != LanguageResultStatus.REVIEW_REQUIRED
            ):
                raise ValueError("a review-required result requires undetermined classification")
            if (
                self.components.bm25 != ComponentState.NOT_APPLICABLE
                or self.components.dense != ComponentState.NOT_APPLICABLE
            ):
                raise ValueError("review-required results must not write retrieval indexes")
            if any(
                state == ComponentState.FAILED
                for state in (self.components.pdf_source, self.components.markdown)
            ):
                raise ValueError("operational component failures must use the failed outcome")
        return self


class BatchResultsRequest(ApiModel):
    pipeline_run_id: str = Field(min_length=1, max_length=255)
    results: list[OperationResultInput] = Field(min_length=1, max_length=500)

    @field_validator("pipeline_run_id")
    @classmethod
    def normalize_pipeline_run_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("pipeline_run_id must contain non-whitespace characters")
        return normalized

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
    review_required: int = Field(default=0, ge=0)
    idempotent_replay: bool = False


class HealthResponse(ApiModel):
    status: str
    checks: dict[str, str] = Field(default_factory=dict)


class HistoricalManifestDocument(ApiModel):
    path: str = Field(min_length=1, max_length=2000)
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    collection_key: CollectionKey
    language: Literal[LanguageCode.EN, LanguageCode.FR]
    ingested_at: datetime | None = None
    chunk_count: int | None = Field(default=None, ge=0)
    pipeline_run_id: str | None = Field(default=None, max_length=255)


class HistoricalImportManifest(ApiModel):
    version: Literal[2]
    documents: list[HistoricalManifestDocument] = Field(min_length=1, max_length=10_000)


class HistoricalImportItemResult(ApiModel):
    filename: str
    sha256: str
    size_bytes: int = Field(ge=1)
    collection_key: CollectionKey
    language: Literal[LanguageCode.EN, LanguageCode.FR]
    document_id: uuid.UUID | None = None


class HistoricalImportResponse(ApiModel):
    dry_run: bool
    imported: int = Field(ge=0)
    items: list[HistoricalImportItemResult]
