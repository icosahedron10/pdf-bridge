"""Persistent catalog, analysis, decision, and index-outbox models."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

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
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class DocumentState(str, enum.Enum):
    """Lifecycle state of a catalog document.

    ``REJECTED``, ``CANCELLED``, and ``DELETED`` are terminal tombstones: the
    canonical bytes, analysis artifacts, and index points are gone and only
    audit hashes and metadata remain.
    """

    ANALYZING = "ANALYZING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    INGESTING = "INGESTING"
    INGEST_FAILED = "INGEST_FAILED"
    INGESTED = "INGESTED"
    REPLACING = "REPLACING"
    REPLACE_FAILED = "REPLACE_FAILED"
    DELETING = "DELETING"
    DELETE_FAILED = "DELETE_FAILED"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    DELETED = "DELETED"


TERMINAL_DOCUMENT_STATES = (
    DocumentState.REJECTED,
    DocumentState.CANCELLED,
    DocumentState.DELETED,
)

# States whose canonical content is retained; exact same-collection duplicate
# blocking applies against these.
RETAINED_DOCUMENT_STATES = tuple(
    state for state in DocumentState if state not in TERMINAL_DOCUMENT_STATES
)

# States representing open intake work shown on the upload workspace.
OPEN_UPLOAD_STATES = (
    DocumentState.ANALYZING,
    DocumentState.REVIEW_REQUIRED,
    DocumentState.INGESTING,
    DocumentState.INGEST_FAILED,
    DocumentState.REPLACING,
    DocumentState.REPLACE_FAILED,
    DocumentState.CLEANUP_PENDING,
    DocumentState.CLEANUP_FAILED,
)


class ScanState(str, enum.Enum):
    """Malware scan outcome recorded for an uploaded document."""

    PENDING = "PENDING"
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    ERROR = "ERROR"


class OperationType(str, enum.Enum):
    """Durable work type executed by the internal worker."""

    ANALYZE = "ANALYZE"
    INGEST = "INGEST"
    DELETE = "DELETE"
    CLEANUP = "CLEANUP"


class OperationState(str, enum.Enum):
    """Lifecycle state of a durable worker operation."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OperationPhase(str, enum.Enum):
    """Operator-visible progress phase of one operation."""

    QUEUED = "QUEUED"
    EXTRACTING = "EXTRACTING"
    COMPARING = "COMPARING"
    AWAITING_DECISION = "AWAITING_DECISION"
    DELETING_EXISTING = "DELETING_EXISTING"
    INGESTING = "INGESTING"
    CLEANING_UP = "CLEANING_UP"
    COMPLETE = "COMPLETE"


class AnalysisStatus(str, enum.Enum):
    """Outcome of one analysis revision."""

    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class DecisionAction(str, enum.Enum):
    """Operator decision on a reviewed upload."""

    KEEP = "KEEP"
    REPLACE = "REPLACE"
    CANCEL = "CANCEL"


class ReplacementState(str, enum.Enum):
    """Progress of a safe document replacement workflow."""

    PREPARING = "PREPARING"
    DELETING_OLD = "DELETING_OLD"
    INGESTING_NEW = "INGESTING_NEW"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class IndexTarget(str, enum.Enum):
    """Which Qdrant collection family an outbox entry addresses."""

    ACTIVE = "ACTIVE"
    SCREENING = "SCREENING"


class IndexAction(str, enum.Enum):
    """Idempotent Qdrant mutation recorded in the outbox."""

    UPSERT = "UPSERT"
    PUBLISH = "PUBLISH"
    DELETE = "DELETE"


class OutboxState(str, enum.Enum):
    """Completion state of one index outbox entry."""

    PENDING = "PENDING"
    DONE = "DONE"
    SUPERSEDED = "SUPERSEDED"


def enum_type(enum_class: type[enum.Enum], name: str) -> SAEnum:
    """Store portable, constrained strings instead of PostgreSQL-only enums."""

    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class Document(Base):
    """Canonical catalog record for an uploaded PDF and its lifecycle metadata."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_bytes_nonnegative"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        Index("ix_documents_sha256_collection_state", "sha256", "collection_key", "state"),
        Index("ix_documents_collection_state", "collection_key", "state"),
        Index("ix_documents_text_sha256", "text_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True, unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="application/pdf"
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    state: Mapped[DocumentState] = mapped_column(
        enum_type(DocumentState, "document_state"), nullable=False, index=True
    )
    collection_key: Mapped[str] = mapped_column(String(63), nullable=False)

    scan_state: Mapped[ScanState] = mapped_column(
        enum_type(ScanState, "scan_state"), nullable=False, default=ScanState.PENDING
    )
    scan_engine: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scan_signature: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    uploader_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analysis_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collection_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # When a document enters CLEANUP_PENDING the tombstone it resolves to is
    # already decided; the cleanup operation applies it after purging content.
    cleanup_target: Mapped[DocumentState | None] = mapped_column(
        enum_type(DocumentState, "cleanup_target_state"), nullable=True
    )
    replaced_by_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    analysis_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    operations: Mapped[list[WorkOperation]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="WorkOperation.created_at",
        foreign_keys="WorkOperation.document_id",
    )
    analyses: Mapped[list[DocumentAnalysis]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentAnalysis.revision",
        foreign_keys="DocumentAnalysis.document_id",
    )
    decisions: Mapped[list[IntakeDecision]] = relationship(
        back_populates="document",
        order_by="IntakeDecision.created_at",
        foreign_keys="IntakeDecision.document_id",
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="document", order_by="AuditEvent.occurred_at"
    )


class WorkOperation(Base):
    """One durable attempt at analyzing, ingesting, deleting, or cleaning up."""

    __tablename__ = "work_operations"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        UniqueConstraint("document_id", "operation_type", "attempt"),
        Index("ix_work_operations_state_created", "state", "created_at"),
        Index("ix_work_operations_lease", "state", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    operation_type: Mapped[OperationType] = mapped_column(
        enum_type(OperationType, "operation_type"), nullable=False, index=True
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
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    replacement_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("replacement_workflows.id", ondelete="RESTRICT"),
        nullable=True,
    )

    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    document: Mapped[Document] = relationship(
        back_populates="operations", foreign_keys=[document_id]
    )
    replacement: Mapped[ReplacementWorkflow | None] = relationship(
        foreign_keys=[replacement_id]
    )


class DocumentAnalysis(Base):
    """One complete analysis revision of an uploaded document."""

    __tablename__ = "document_analyses"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        UniqueConstraint("document_id", "revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AnalysisStatus] = mapped_column(
        enum_type(AnalysisStatus, "analysis_status"),
        nullable=False,
        default=AnalysisStatus.RUNNING,
    )
    pipeline_fingerprint: Mapped[str | None] = mapped_column(String(100), nullable=True)
    collection_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    filename_warnings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    semantic_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    classification_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    incomplete_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    screening_indexed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_ingest_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    classified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overflow_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[Document] = relationship(
        back_populates="analyses", foreign_keys=[document_id]
    )
    chunks: Mapped[list[AnalysisChunk]] = relationship(
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="AnalysisChunk.chunk_index",
    )
    candidates: Mapped[list[AnalysisCandidate]] = relationship(
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="AnalysisCandidate.rank",
    )
    artifacts: Mapped[list[DocumentArtifact]] = relationship(
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="DocumentArtifact.kind",
    )


class AnalysisChunk(Base):
    """One deterministic, page-mapped chunk retained for evidence and audit."""

    __tablename__ = "analysis_chunks"
    __table_args__ = (
        UniqueConstraint("analysis_id", "chunk_index"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_analyses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    analysis: Mapped[DocumentAnalysis] = relationship(back_populates="chunks")


class AnalysisCandidate(Base):
    """One qualifying candidate document discovered by an analysis."""

    __tablename__ = "analysis_candidates"
    __table_args__ = (
        UniqueConstraint("analysis_id", "matched_document_id"),
        CheckConstraint("rank >= 1", name="rank_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_analyses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    matched_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    max_cosine: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    strong_cosine_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    moderate_cosine_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bm25_strong_placements: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fused_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    classified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    overflow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched_chunk_pairs: Mapped[list[list[Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Snapshot of the matched document at analysis time so evidence stays
    # renderable even after the candidate's state changes.
    document_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    analysis: Mapped[DocumentAnalysis] = relationship(back_populates="candidates")
    matched_document: Mapped[Document] = relationship(foreign_keys=[matched_document_id])
    findings: Mapped[list[CandidateFindingRecord]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        order_by="CandidateFindingRecord.created_at",
    )


class CandidateFindingRecord(Base):
    """One validated, explanation-only LLM finding for a candidate pair."""

    __tablename__ = "candidate_findings"
    __table_args__ = (UniqueConstraint("candidate_id", "role"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analysis_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    candidate: Mapped[AnalysisCandidate] = relationship(back_populates="findings")


class IntakeDecision(Base):
    """Immutable operator decision on a reviewed upload."""

    __tablename__ = "intake_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Content-free snapshot of the analysis the operator reviewed. Analyses
    # are deliberately purged with document content, while immutable decision
    # metadata must survive cancellation, deletion, and replacement.
    analysis_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    analysis_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[DecisionAction] = mapped_column(
        enum_type(DecisionAction, "decision_action"), nullable=False
    )
    target_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    advisory_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    document: Mapped[Document] = relationship(
        back_populates="decisions", foreign_keys=[document_id]
    )


class ReplacementWorkflow(Base):
    """Durable state of one safe replacement of an active document."""

    __tablename__ = "replacement_workflows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    new_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    old_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("intake_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[ReplacementState] = mapped_column(
        enum_type(ReplacementState, "replacement_state"),
        nullable=False,
        default=ReplacementState.PREPARING,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    new_document: Mapped[Document] = relationship(foreign_keys=[new_document_id])
    old_document: Mapped[Document] = relationship(foreign_keys=[old_document_id])


class DocumentArtifact(Base):
    """Pointer to one compressed private analysis artifact on disk."""

    __tablename__ = "document_artifacts"
    __table_args__ = (
        UniqueConstraint("analysis_id", "kind"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_analyses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    analysis: Mapped[DocumentAnalysis] = relationship(back_populates="artifacts")


class IndexOutboxEntry(Base):
    """Ordered, durable record of one pending Qdrant mutation."""

    __tablename__ = "index_outbox"
    __table_args__ = (
        CheckConstraint("collection_epoch >= 1", name="collection_epoch_positive"),
        Index("ix_index_outbox_state_id", "state", "id"),
        Index(
            "ix_index_outbox_document_target_epoch",
            "document_id",
            "target",
            "collection_epoch",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Content-free correlation snapshot. Completed outbox history survives
    # analysis purging and therefore must not retain a live-analysis FK.
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    collection_key: Mapped[str] = mapped_column(String(63), nullable=False)
    collection_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    target: Mapped[IndexTarget] = mapped_column(
        enum_type(IndexTarget, "index_target"), nullable=False
    )
    action: Mapped[IndexAction] = mapped_column(
        enum_type(IndexAction, "index_action"), nullable=False
    )
    expected_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[OutboxState] = mapped_column(
        enum_type(OutboxState, "outbox_state"),
        nullable=False,
        default=OutboxState.PENDING,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CollectionEpoch(Base):
    """Current epoch of one logical collection's physical Qdrant collection."""

    __tablename__ = "collection_epochs"

    collection_key: Mapped[str] = mapped_column(String(63), primary_key=True)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AuditEvent(Base):
    """Append-only record of a security- or lifecycle-relevant action."""

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


@event.listens_for(AuditEvent, "before_update")
def prevent_audit_update(_mapper: object, _connection: object, _target: AuditEvent) -> None:
    """Reject updates so the audit ledger remains append-only."""

    raise RuntimeError("audit events are append-only")


@event.listens_for(AuditEvent, "before_delete")
def prevent_audit_delete(_mapper: object, _connection: object, _target: AuditEvent) -> None:
    """Reject deletes so the audit ledger remains append-only."""

    raise RuntimeError("audit events are append-only")


@event.listens_for(IntakeDecision, "before_update")
def prevent_decision_update(
    _mapper: object, _connection: object, _target: IntakeDecision
) -> None:
    """Reject updates so recorded decisions remain immutable."""

    raise RuntimeError("intake decisions are immutable")


@event.listens_for(IntakeDecision, "before_delete")
def prevent_decision_delete(
    _mapper: object, _connection: object, _target: IntakeDecision
) -> None:
    """Reject deletes so recorded decisions remain immutable."""

    raise RuntimeError("intake decisions are immutable")
