"""Target catalog, prepared-revision, operation, and audit persistence models.

The coordinated reset intentionally has no compatibility layer for the v1
analysis/ingestion schema.  Documents retain immutable intake metadata and
content-free history; all generated content hangs from a prepared revision so
terminal cleanup can delete it without deleting the revision manifest,
decision, publication record, or audit ledger.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    select,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy import (
    inspect as sa_inspect,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class DocumentState(str, enum.Enum):
    """Exact public document lifecycle defined by the v2 contract."""

    PREFLIGHTING = "PREFLIGHTING"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    PUBLISHING = "PUBLISHING"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    READY = "READY"
    DELETING = "DELETING"
    DELETE_FAILED = "DELETE_FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    DELETED = "DELETED"


class TerminalDisposition(str, enum.Enum):
    """Content-free state a deletion workflow must commit."""

    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    DELETED = "DELETED"


TERMINAL_DOCUMENT_STATES = (
    DocumentState.REJECTED,
    DocumentState.CANCELLED,
    DocumentState.DELETED,
)

CONTENT_BLOCKED_DOCUMENT_STATES = (
    DocumentState.DELETING,
    DocumentState.DELETE_FAILED,
    *TERMINAL_DOCUMENT_STATES,
)

RETRYABLE_DOCUMENT_STATES = (
    DocumentState.PREFLIGHT_FAILED,
    DocumentState.PUBLISH_FAILED,
    DocumentState.DELETE_FAILED,
)


class ScanState(str, enum.Enum):
    """Malware scan result captured before durable admission."""

    PENDING = "PENDING"
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    ERROR = "ERROR"


class OperationType(str, enum.Enum):
    """Durable work classes owned by the in-process worker."""

    PREFLIGHT = "PREFLIGHT"
    PUBLISH = "PUBLISH"
    DELETE = "DELETE"


class OperationPriority(enum.IntEnum):
    """Stable numeric queue order; lower values are claimed first."""

    HIGH = 0
    REPLACEMENT = 10
    PUBLISH = 20
    NORMAL = 30


def priority_for_operation(
    operation_type: OperationType, *, replacement_delete: bool = False
) -> OperationPriority:
    """Resolve the contract queue class for a new durable operation."""

    if replacement_delete:
        if operation_type is not OperationType.PUBLISH:
            raise ValueError("replacement deletion is part of a publish operation")
        return OperationPriority.REPLACEMENT
    if operation_type is OperationType.DELETE:
        return OperationPriority.HIGH
    if operation_type is OperationType.PUBLISH:
        return OperationPriority.PUBLISH
    return OperationPriority.NORMAL


class OperationState(str, enum.Enum):
    """Durable queue/execution status independent of document state."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OperationPhase(str, enum.Enum):
    """Fine-grained operator-visible progress across every work class."""

    QUEUED = "QUEUED"

    EXTRACTING = "EXTRACTING"
    CHECKING_ELIGIBILITY = "CHECKING_ELIGIBILITY"
    PACKING_FORMATTER_BATCHES = "PACKING_FORMATTER_BATCHES"
    FORMATTING_MARKDOWN = "FORMATTING_MARKDOWN"
    VALIDATING_MARKDOWN = "VALIDATING_MARKDOWN"
    CHUNKING_MARKDOWN = "CHUNKING_MARKDOWN"
    EMBEDDING_DENSE = "EMBEDDING_DENSE"
    EMBEDDING_SPARSE = "EMBEDDING_SPARSE"
    UPSERTING_SCREENING_POINTS = "UPSERTING_SCREENING_POINTS"
    DISCOVERING_CANDIDATES = "DISCOVERING_CANDIDATES"
    CLASSIFYING_CANDIDATES = "CLASSIFYING_CANDIDATES"
    SEALING_REVISION = "SEALING_REVISION"
    AWAITING_DECISION = "AWAITING_DECISION"

    UPSERT_ACTIVE_POINTS = "UPSERT_ACTIVE_POINTS"
    VERIFY_ACTIVE_POINTS = "VERIFY_ACTIVE_POINTS"
    REMOVE_SCREENING_POINTS = "REMOVE_SCREENING_POINTS"
    VERIFY_SCREENING_REMOVAL = "VERIFY_SCREENING_REMOVAL"

    # These names are the exact deletion phases exposed by API v2.
    DELETE_ACTIVE_POINTS = "DELETE_ACTIVE_POINTS"
    VERIFY_ACTIVE_ZERO = "VERIFY_ACTIVE_ZERO"
    DELETE_SCREENING_POINTS = "DELETE_SCREENING_POINTS"
    VERIFY_SCREENING_ZERO = "VERIFY_SCREENING_ZERO"
    PURGE_STORAGE = "PURGE_STORAGE"
    COMMIT_TOMBSTONE = "COMMIT_TOMBSTONE"

    COMPLETE = "COMPLETE"


class RevisionStatus(str, enum.Enum):
    """Preparation status; SEALED revisions cannot be changed or extended."""

    PREPARING = "PREPARING"
    SEALED = "SEALED"
    FAILED = "FAILED"


class FormatterBatchStatus(str, enum.Enum):
    """Outcome of one bounded formatter batch."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    VALID = "VALID"
    FAILED = "FAILED"


class CandidateSource(str, enum.Enum):
    """Physical visibility boundary where a candidate was found."""

    ACTIVE = "ACTIVE"
    SCREENING = "SCREENING"


class EvidenceKind(str, enum.Enum):
    """Kind of retained deterministic or advisory preflight evidence."""

    DETERMINISTIC = "DETERMINISTIC"
    CLASSIFIER = "CLASSIFIER"
    VERIFIER = "VERIFIER"
    INCOMPLETE = "INCOMPLETE"


class DecisionAction(str, enum.Enum):
    """Immutable operator action bound to one prepared revision."""

    KEEP = "KEEP"
    REPLACE = "REPLACE"
    CANCEL = "CANCEL"


class PublicationStatus(str, enum.Enum):
    """Durable publication checkpoint for one prepared revision."""

    PENDING = "PENDING"
    UPSERTED = "UPSERTED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class DeletionPhase(str, enum.Enum):
    """Exact restart-safe deletion phases from the API v2 contract."""

    DELETE_ACTIVE_POINTS = "DELETE_ACTIVE_POINTS"
    VERIFY_ACTIVE_ZERO = "VERIFY_ACTIVE_ZERO"
    DELETE_SCREENING_POINTS = "DELETE_SCREENING_POINTS"
    VERIFY_SCREENING_ZERO = "VERIFY_SCREENING_ZERO"
    PURGE_STORAGE = "PURGE_STORAGE"
    COMMIT_TOMBSTONE = "COMMIT_TOMBSTONE"


class IndexTarget(str, enum.Enum):
    """Configured physical collection role for an outbox mutation."""

    ACTIVE = "ACTIVE"
    SCREENING = "SCREENING"


class IndexAction(str, enum.Enum):
    """Idempotent point-level Qdrant mutation."""

    UPSERT = "UPSERT"
    PUBLISH = "PUBLISH"
    DELETE = "DELETE"


class OutboxState(str, enum.Enum):
    """Durable completion state of a Qdrant outbox entry."""

    PENDING = "PENDING"
    APPLIED = "APPLIED"
    SUPERSEDED = "SUPERSEDED"


def enum_type(enum_class: type[enum.Enum], name: str) -> SAEnum:
    """Store portable constrained strings rather than backend-native enums."""

    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [str(member.value) for member in members],
    )


class Document(Base):
    """Authoritative catalog identity and lifecycle for one admitted PDF."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint(
            "(state IN ('DELETING', 'DELETE_FAILED') AND terminal_disposition IS NOT NULL) "
            "OR (state IN ('REJECTED', 'CANCELLED', 'DELETED') "
            "AND terminal_disposition = state) "
            "OR (state NOT IN ('DELETING', 'DELETE_FAILED', 'REJECTED', 'CANCELLED', "
            "'DELETED') AND terminal_disposition IS NULL)",
            name="terminal_disposition_matches_state",
        ),
        CheckConstraint(
            "state NOT IN ('REJECTED', 'CANCELLED', 'DELETED') OR storage_key IS NULL",
            name="terminal_document_has_no_source",
        ),
        Index("ix_documents_collection_state_created", "collection_key", "state", "created_at"),
        Index("ix_documents_collection_sha_state", "collection_key", "sha256", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    collection_key: Mapped[str] = mapped_column(String(63), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="application/pdf"
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True, unique=True)

    state: Mapped[DocumentState] = mapped_column(
        enum_type(DocumentState, "document_state"), nullable=False, index=True
    )
    terminal_disposition: Mapped[TerminalDisposition | None] = mapped_column(
        enum_type(TerminalDisposition, "terminal_disposition"), nullable=True
    )

    scan_state: Mapped[ScanState] = mapped_column(
        enum_type(ScanState, "scan_state"), nullable=False
    )
    scan_engine: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scan_signature: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    failure_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replaced_by_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )

    prepared_revisions: Mapped[list[PreparedRevision]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="PreparedRevision.revision_number",
        foreign_keys="PreparedRevision.document_id",
    )
    operations: Mapped[list[WorkOperation]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="WorkOperation.created_at",
        foreign_keys="WorkOperation.document_id",
    )
    decisions: Mapped[list[Decision]] = relationship(
        back_populates="document",
        order_by="Decision.created_at",
        foreign_keys="Decision.document_id",
    )
    publications: Mapped[list[PublicationRecord]] = relationship(
        back_populates="document",
        order_by="PublicationRecord.created_at",
        foreign_keys="PublicationRecord.document_id",
    )
    deletion_progress: Mapped[DeletionProgress | None] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys="DeletionProgress.document_id",
    )
    tombstone: Mapped[Tombstone | None] = relationship(
        back_populates="document", uselist=False, foreign_keys="Tombstone.document_id"
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="document", order_by="AuditEvent.occurred_at"
    )


class PreparedRevision(Base):
    """Content-free manifest header for one preparation attempt.

    Content children are retained while the document is accessible and may be
    deleted during terminal purge.  The sealed header remains so immutable
    decisions, publication proof, and tombstone hashes keep their correlation.
    """

    __tablename__ = "prepared_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "revision_number"),
        CheckConstraint("revision_number >= 1", name="revision_number_positive"),
        CheckConstraint("page_count IS NULL OR page_count >= 0", name="page_count_nonnegative"),
        CheckConstraint("chunk_count IS NULL OR chunk_count >= 0", name="chunk_count_nonnegative"),
        CheckConstraint(
            "expected_point_count IS NULL OR expected_point_count >= 0",
            name="expected_point_count_nonnegative",
        ),
        CheckConstraint("dense_dimension = 768", name="dense_dimension_target"),
        CheckConstraint(
            "status != 'SEALED' OR (manifest_sha256 IS NOT NULL AND sealed_at IS NOT NULL "
            "AND page_count IS NOT NULL AND chunk_count IS NOT NULL "
            "AND expected_point_count = chunk_count)",
            name="sealed_manifest_complete",
        ),
        Index("ix_prepared_revisions_document_status", "document_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RevisionStatus] = mapped_column(
        enum_type(RevisionStatus, "revision_status"),
        nullable=False,
        default=RevisionStatus.PREPARING,
    )

    active_qdrant_collection: Mapped[str] = mapped_column(String(255), nullable=False)
    content_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    index_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    preflight_policy_id: Mapped[str] = mapped_column(String(128), nullable=False)

    formatter_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    dense_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    dense_dimension: Mapped[int] = mapped_column(Integer, nullable=False, default=768)
    sparse_model_id: Mapped[str] = mapped_column(String(255), nullable=False)

    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    native_text_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    formatter_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vector_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    candidate_discovery_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    advisory_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    clear_for_publication: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    incomplete_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_point_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extraction_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    markdown_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[Document] = relationship(
        back_populates="prepared_revisions", foreign_keys=[document_id]
    )
    artifacts: Mapped[list[RevisionArtifact]] = relationship(
        back_populates="prepared_revision",
        cascade="all, delete-orphan",
        order_by="RevisionArtifact.kind",
    )
    extracted_pages: Mapped[list[ExtractedPage]] = relationship(
        back_populates="prepared_revision",
        cascade="all, delete-orphan",
        order_by="ExtractedPage.page_number",
    )
    formatter_batches: Mapped[list[FormatterBatch]] = relationship(
        back_populates="prepared_revision",
        cascade="all, delete-orphan",
        order_by="(FormatterBatch.batch_index, FormatterBatch.attempt)",
    )
    prepared_pages: Mapped[list[PreparedPage]] = relationship(
        back_populates="prepared_revision",
        cascade="all, delete-orphan",
        order_by="PreparedPage.page_number",
    )
    chunks: Mapped[list[PreparedChunk]] = relationship(
        back_populates="prepared_revision",
        cascade="all, delete-orphan",
        order_by="PreparedChunk.chunk_index",
    )
    candidates: Mapped[list[PreparedCandidate]] = relationship(
        back_populates="prepared_revision",
        cascade="all, delete-orphan",
        order_by="PreparedCandidate.rank",
    )
    evidence: Mapped[list[CandidateEvidence]] = relationship(
        back_populates="prepared_revision",
        cascade="all, delete-orphan",
        order_by="CandidateEvidence.created_at",
    )
    decision: Mapped[Decision | None] = relationship(
        back_populates="prepared_revision", uselist=False
    )
    publication: Mapped[PublicationRecord | None] = relationship(
        back_populates="prepared_revision", uselist=False
    )


class RevisionArtifact(Base):
    """Opaque UUID-addressed protected artifact belonging to a revision."""

    __tablename__ = "revision_artifacts"
    __table_args__ = (
        UniqueConstraint("prepared_revision_id", "kind", "sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    prepared_revision: Mapped[PreparedRevision] = relationship(back_populates="artifacts")


class ExtractedPage(Base):
    """Normalized pypdf layout text and identity for one source page."""

    __tablename__ = "extracted_pages"
    __table_args__ = (
        UniqueConstraint("prepared_revision_id", "page_number"),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
        CheckConstraint("character_count >= 0", name="character_count_nonnegative"),
        CheckConstraint("length(text_sha256) = 64", name="text_sha256_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    layout_text: Mapped[str] = mapped_column(Text, nullable=False)
    character_count: Mapped[int] = mapped_column(Integer, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    prepared_revision: Mapped[PreparedRevision] = relationship(back_populates="extracted_pages")


class FormatterBatch(Base):
    """One bounded page/slice formatter request and validation result."""

    __tablename__ = "formatter_batches"
    __table_args__ = (
        UniqueConstraint("prepared_revision_id", "batch_index", "attempt"),
        CheckConstraint("batch_index >= 0", name="batch_index_nonnegative"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[FormatterBatchStatus] = mapped_column(
        enum_type(FormatterBatchStatus, "formatter_batch_status"),
        nullable=False,
        default=FormatterBatchStatus.PENDING,
    )
    page_slices: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    validation_errors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    prepared_revision: Mapped[PreparedRevision] = relationship(back_populates="formatter_batches")


class PreparedPage(Base):
    """Validated canonical Markdown and projection hashes for one page."""

    __tablename__ = "prepared_pages"
    __table_args__ = (
        UniqueConstraint("prepared_revision_id", "page_number"),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
        CheckConstraint("length(markdown_sha256) = 64", name="markdown_sha256_length"),
        CheckConstraint(
            "length(source_projection_sha256) = 64", name="source_projection_sha256_length"
        ),
        CheckConstraint(
            "length(markdown_projection_sha256) = 64",
            name="markdown_projection_sha256_length",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    slices: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    markdown_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_projection_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    markdown_projection_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    prepared_revision: Mapped[PreparedRevision] = relationship(back_populates="prepared_pages")


class PreparedChunk(Base):
    """Deterministic heading/page-aware Markdown chunk and future point ID."""

    __tablename__ = "prepared_chunks"
    __table_args__ = (
        UniqueConstraint("prepared_revision_id", "chunk_index"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
        CheckConstraint("page_start >= 1", name="page_start_positive"),
        CheckConstraint("page_end >= page_start", name="page_range_ordered"),
        CheckConstraint("token_count > 0 AND token_count <= 384", name="token_count_target_bound"),
        CheckConstraint("length(text_sha256) = 64", name="text_sha256_length"),
    )

    # The worker supplies the contract UUIDv5; there is deliberately no random default.
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)

    prepared_revision: Mapped[PreparedRevision] = relationship(back_populates="chunks")
    vector: Mapped[PreparedChunkVector | None] = relationship(
        back_populates="chunk", cascade="all, delete-orphan", uselist=False
    )


class PreparedChunkVector(Base):
    """Correlated dense and BM25 document vectors for one prepared chunk."""

    __tablename__ = "prepared_chunk_vectors"
    __table_args__ = (
        CheckConstraint("dense_dimension = 768", name="dense_dimension_target"),
        CheckConstraint("length(dense_sha256) = 64", name="dense_sha256_length"),
        CheckConstraint("length(sparse_sha256) = 64", name="sparse_sha256_length"),
        CheckConstraint("document_encoded = 1", name="sparse_is_document_encoding"),
        CheckConstraint("dense_normalized = 1", name="dense_is_normalized"),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dense_dimension: Mapped[int] = mapped_column(Integer, nullable=False, default=768)
    dense: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    sparse_indices: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    sparse_values: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    dense_normalized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    document_encoded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    dense_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sparse_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    chunk: Mapped[PreparedChunk] = relationship(back_populates="vector")
    prepared_revision: Mapped[PreparedRevision] = relationship()


class PreparedCandidate(Base):
    """One deterministic, collection-scoped candidate discovered in preflight."""

    __tablename__ = "prepared_candidates"
    __table_args__ = (
        UniqueConstraint("prepared_revision_id", "matched_document_id"),
        CheckConstraint("rank >= 1", name="rank_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    matched_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source: Mapped[CandidateSource] = mapped_column(
        enum_type(CandidateSource, "candidate_source"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    max_cosine: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bm25_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fused_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    matched_chunk_pairs: Mapped[list[list[Any]]] = mapped_column(JSON, nullable=False, default=list)
    document_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    prepared_revision: Mapped[PreparedRevision] = relationship(back_populates="candidates")
    evidence: Mapped[list[CandidateEvidence]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        order_by="CandidateEvidence.created_at",
    )


class CandidateEvidence(Base):
    """Validated deterministic/advisory evidence; prompts/raw output stay in artifacts."""

    __tablename__ = "candidate_evidence"
    __table_args__ = (UniqueConstraint("candidate_id", "kind"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[EvidenceKind] = mapped_column(
        enum_type(EvidenceKind, "evidence_kind"), nullable=False
    )
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    prepared_revision: Mapped[PreparedRevision] = relationship(back_populates="evidence")
    candidate: Mapped[PreparedCandidate] = relationship(back_populates="evidence")


class IdempotencyRecord(Base):
    """Request fingerprint and replay material for one asynchronous mutation."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("length(request_sha256) = 64", name="request_sha256_length"),
        Index("ix_idempotency_records_resource", "resource_type", "resource_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Decision(Base):
    """Immutable Keep/Replace/Cancel decision on an exact sealed revision."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("prepared_revision_id"),
        UniqueConstraint("idempotency_record_id"),
        CheckConstraint(
            "(action = 'REPLACE' AND target_document_id IS NOT NULL) OR "
            "(action IN ('KEEP', 'CANCEL') AND target_document_id IS NULL)",
            name="replacement_target_matches_action",
        ),
        CheckConstraint(
            "length(prepared_manifest_sha256) = 64", name="prepared_manifest_sha256_length"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    prepared_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[DecisionAction] = mapped_column(
        enum_type(DecisionAction, "decision_action"), nullable=False
    )
    target_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("idempotency_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    document: Mapped[Document] = relationship(
        back_populates="decisions", foreign_keys=[document_id]
    )
    prepared_revision: Mapped[PreparedRevision] = relationship(back_populates="decision")
    idempotency_record: Mapped[IdempotencyRecord] = relationship()


class PublicationRecord(Base):
    """Durable exact-target verification record for one prepared revision."""

    __tablename__ = "publication_records"
    __table_args__ = (
        UniqueConstraint("prepared_revision_id"),
        CheckConstraint("expected_points >= 0", name="expected_points_nonnegative"),
        CheckConstraint(
            "verified_points IS NULL OR verified_points >= 0",
            name="verified_points_nonnegative",
        ),
        CheckConstraint(
            "status != 'VERIFIED' OR (verified_points = expected_points "
            "AND payload_revision_verified = 1 AND vector_schema_verified = 1 "
            "AND screening_zero_verified = 1 AND verified_at IS NOT NULL)",
            name="verified_publication_complete",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    active_qdrant_collection: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[PublicationStatus] = mapped_column(
        enum_type(PublicationStatus, "publication_status"),
        nullable=False,
        default=PublicationStatus.PENDING,
    )
    expected_points: Mapped[int] = mapped_column(Integer, nullable=False)
    verified_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_revision_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vector_schema_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    screening_zero_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[Document] = relationship(
        back_populates="publications", foreign_keys=[document_id]
    )
    prepared_revision: Mapped[PreparedRevision] = relationship(back_populates="publication")


class DeletionProgress(Base):
    """Restart-safe deletion checkpoint pinned to exact physical collections."""

    __tablename__ = "deletion_progress"
    __table_args__ = (
        UniqueConstraint("document_id"),
        CheckConstraint(
            "active_qdrant_collection != screening_qdrant_collection",
            name="physical_targets_distinct",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    prepared_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    publication_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("publication_records.id", ondelete="RESTRICT"),
        nullable=True,
    )
    terminal_disposition: Mapped[TerminalDisposition] = mapped_column(
        enum_type(TerminalDisposition, "deletion_terminal_disposition"), nullable=False
    )
    active_qdrant_collection: Mapped[str] = mapped_column(String(255), nullable=False)
    screening_qdrant_collection: Mapped[str] = mapped_column(String(255), nullable=False)
    phase: Mapped[DeletionPhase] = mapped_column(
        enum_type(DeletionPhase, "deletion_phase"),
        nullable=False,
        default=DeletionPhase.DELETE_ACTIVE_POINTS,
    )
    active_zero_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    screening_zero_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    storage_purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    document: Mapped[Document] = relationship(
        back_populates="deletion_progress", foreign_keys=[document_id]
    )
    prepared_revision: Mapped[PreparedRevision | None] = relationship()
    publication_record: Mapped[PublicationRecord | None] = relationship()


class WorkOperation(Base):
    """One durable worker attempt with lease, priority, and sanitized failure."""

    __tablename__ = "work_operations"
    __table_args__ = (
        UniqueConstraint("document_id", "operation_type", "attempt"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("priority >= 0", name="priority_nonnegative"),
        CheckConstraint(
            "phase_started_at >= created_at", name="phase_started_not_before_creation"
        ),
        CheckConstraint("operation_type != 'DELETE' OR priority = 0", name="delete_priority_high"),
        Index("ix_work_operations_claim", "state", "priority", "created_at", "id"),
        Index("ix_work_operations_lease", "state", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    prepared_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    replacement_target_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("idempotency_records.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    operation_type: Mapped[OperationType] = mapped_column(
        enum_type(OperationType, "operation_type"), nullable=False, index=True
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=int(OperationPriority.NORMAL)
    )
    state: Mapped[OperationState] = mapped_column(
        enum_type(OperationState, "operation_state"),
        nullable=False,
        default=OperationState.QUEUED,
        index=True,
    )
    phase: Mapped[OperationPhase] = mapped_column(
        enum_type(OperationPhase, "operation_phase"),
        nullable=False,
        default=OperationPhase.QUEUED,
    )
    phase_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[Document] = relationship(
        back_populates="operations", foreign_keys=[document_id]
    )
    prepared_revision: Mapped[PreparedRevision | None] = relationship()
    idempotency_record: Mapped[IdempotencyRecord | None] = relationship()


class IndexOutboxEntry(Base):
    """Ordered exact-revision mutation against one exact physical collection."""

    __tablename__ = "index_outbox"
    __table_args__ = (
        UniqueConstraint("prepared_revision_id", "target", "action", "qdrant_collection"),
        CheckConstraint("expected_points >= 0", name="expected_points_nonnegative"),
        Index("ix_index_outbox_claim", "state", "id"),
        Index("ix_index_outbox_document_target", "document_id", "target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    prepared_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prepared_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    target: Mapped[IndexTarget] = mapped_column(
        enum_type(IndexTarget, "index_target"), nullable=False
    )
    action: Mapped[IndexAction] = mapped_column(
        enum_type(IndexAction, "index_action"), nullable=False
    )
    qdrant_collection: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_points: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[OutboxState] = mapped_column(
        enum_type(OutboxState, "outbox_state"),
        nullable=False,
        default=OutboxState.PENDING,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    prepared_revision: Mapped[PreparedRevision] = relationship()


class Tombstone(Base):
    """Content-free terminal audit metadata retained for a document."""

    __tablename__ = "tombstones"
    __table_args__ = (
        UniqueConstraint("document_id"),
        CheckConstraint("length(source_sha256) = 64", name="source_sha256_length"),
        CheckConstraint(
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
            name="manifest_sha256_length",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    collection_key: Mapped[str] = mapped_column(String(63), nullable=False)
    disposition: Mapped[TerminalDisposition] = mapped_column(
        enum_type(TerminalDisposition, "tombstone_disposition"), nullable=False
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    document: Mapped[Document] = relationship(
        back_populates="tombstone", foreign_keys=[document_id]
    )


class AuditEvent(Base):
    """Append-only security and lifecycle event."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_document_time", "document_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_operations.id", ondelete="RESTRICT"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    document: Mapped[Document | None] = relationship(back_populates="audit_events")


class _RevisionChild(Protocol):
    prepared_revision_id: uuid.UUID


def _column_values_changed(target: Any) -> bool:
    state = sa_inspect(target)
    return any(state.attrs[column.key].history.has_changes() for column in target.__table__.columns)


def _prevent_update(_mapper: object, _connection: Connection, target: object) -> None:
    if _column_values_changed(target):
        raise RuntimeError(f"{type(target).__name__} records are immutable")


def _prevent_delete(_mapper: object, _connection: Connection, target: object) -> None:
    raise RuntimeError(f"{type(target).__name__} records are immutable")


def _stored_revision_status(
    connection: Connection, prepared_revision_id: uuid.UUID
) -> RevisionStatus | None:
    return connection.execute(
        select(PreparedRevision.status).where(PreparedRevision.id == prepared_revision_id)
    ).scalar_one_or_none()


def _prevent_sealed_revision_update(
    _mapper: object, connection: Connection, target: PreparedRevision
) -> None:
    if (
        _column_values_changed(target)
        and _stored_revision_status(connection, target.id) == RevisionStatus.SEALED
    ):
        raise RuntimeError("sealed prepared revisions are immutable")


def _prevent_sealed_child_insert(
    _mapper: object, connection: Connection, target: _RevisionChild
) -> None:
    if _stored_revision_status(connection, target.prepared_revision_id) == RevisionStatus.SEALED:
        raise RuntimeError("content belonging to a sealed prepared revision is immutable")


def _prevent_sealed_child_update(
    _mapper: object, connection: Connection, target: _RevisionChild
) -> None:
    if (
        _column_values_changed(target)
        and _stored_revision_status(connection, target.prepared_revision_id)
        == RevisionStatus.SEALED
    ):
        raise RuntimeError("content belonging to a sealed prepared revision is immutable")


def _prevent_verified_publication_update(
    _mapper: object, connection: Connection, target: PublicationRecord
) -> None:
    status = connection.execute(
        select(PublicationRecord.status).where(PublicationRecord.id == target.id)
    ).scalar_one_or_none()
    if _column_values_changed(target) and status == PublicationStatus.VERIFIED:
        raise RuntimeError("verified publication records are immutable")


# Audit and decisions survive terminal purge and are never changed or removed.
for immutable_model in (AuditEvent, Decision, Tombstone):
    event.listen(immutable_model, "before_update", _prevent_update)
    event.listen(immutable_model, "before_delete", _prevent_delete)

# Sealing is the final permitted revision-header update.  Content rows cannot
# be inserted or updated afterward, but deletes remain available to the
# lifecycle purge that commits a content-free tombstone.
event.listen(PreparedRevision, "before_update", _prevent_sealed_revision_update)
for sealed_child_model in (
    RevisionArtifact,
    ExtractedPage,
    FormatterBatch,
    PreparedPage,
    PreparedChunk,
    PreparedChunkVector,
    PreparedCandidate,
    CandidateEvidence,
):
    event.listen(sealed_child_model, "before_insert", _prevent_sealed_child_insert)
    event.listen(sealed_child_model, "before_update", _prevent_sealed_child_update)

event.listen(PublicationRecord, "before_update", _prevent_verified_publication_update)
