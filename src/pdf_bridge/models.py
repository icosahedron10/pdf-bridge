"""Persistent catalog and queue models."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
    return datetime.now(UTC)


class DocumentState(str, enum.Enum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    STAGED = "STAGED"
    INGESTED = "INGESTED"
    INGEST_FAILED = "INGEST_FAILED"
    DELETE_QUEUED = "DELETE_QUEUED"
    DELETE_CLAIMED = "DELETE_CLAIMED"
    DELETE_CLEANUP = "DELETE_CLEANUP"
    DELETED = "DELETED"
    DELETE_FAILED = "DELETE_FAILED"
    CANCEL_CLEANUP = "CANCEL_CLEANUP"
    CANCELLED = "CANCELLED"
    CLASSIFICATION_REVIEW = "CLASSIFICATION_REVIEW"


class ScanState(str, enum.Enum):
    PENDING = "PENDING"
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    ERROR = "ERROR"


class OperationType(str, enum.Enum):
    INGEST = "INGEST"
    DELETE = "DELETE"


class OperationState(str, enum.Enum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    STAGED = "STAGED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class LanguageCode(str, enum.Enum):
    UND = "und"
    EN = "en"
    FR = "fr"


class LanguageStatus(str, enum.Enum):
    PENDING = "pending"
    DETECTED = "detected"
    REVIEW_REQUIRED = "review_required"
    OVERRIDDEN = "overridden"


class BatchState(str, enum.Enum):
    EMPTY = "EMPTY"
    CLAIMED = "CLAIMED"
    STAGED = "STAGED"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


def enum_type(enum_class: type[enum.Enum], name: str) -> SAEnum:
    """Store portable, constrained strings instead of PostgreSQL-only enums."""

    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_bytes_nonnegative"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint(
            "language_confidence IS NULL OR "
            "(language_confidence >= 0 AND language_confidence <= 1)",
            name="language_confidence_range",
        ),
        Index("ix_documents_sha256_state", "sha256", "state"),
        Index("ix_documents_collection_state", "collection_key", "state"),
        Index(
            "ix_documents_collection_language_state",
            "collection_key",
            "language",
            "state",
        ),
        Index(
            "ix_documents_normalized_filename_size",
            "normalized_filename",
            "size_bytes",
        ),
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
    collection_key: Mapped[str | None] = mapped_column(String(63), nullable=True)
    language: Mapped[LanguageCode] = mapped_column(
        enum_type(LanguageCode, "language_code"),
        nullable=False,
        default=LanguageCode.UND,
        server_default=LanguageCode.UND.value,
    )
    language_status: Mapped[LanguageStatus] = mapped_column(
        enum_type(LanguageStatus, "language_status"),
        nullable=False,
        default=LanguageStatus.PENDING,
        server_default=LanguageStatus.PENDING.value,
    )
    language_method: Mapped[str | None] = mapped_column(String(100), nullable=True)
    language_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    language_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    language_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pipeline_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pipeline_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    operations: Mapped[list[QueueOperation]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="QueueOperation.created_at",
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="document", order_by="AuditEvent.occurred_at"
    )


class JobBatch(Base):
    __tablename__ = "job_batches"
    __table_args__ = (CheckConstraint("operation_count >= 0", name="operation_count_nonnegative"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    state: Mapped[BatchState] = mapped_column(
        enum_type(BatchState, "batch_state"), nullable=False, default=BatchState.CLAIMED
    )
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    operation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pipeline_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    operations: Mapped[list[QueueOperation]] = relationship(
        back_populates="batch", order_by="QueueOperation.created_at"
    )


class QueueOperation(Base):
    __tablename__ = "queue_operations"
    __table_args__ = (
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        UniqueConstraint("document_id", "operation_type", "attempt"),
        Index("ix_queue_operations_state_created", "state", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_batches.id", ondelete="SET NULL"), nullable=True
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
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pipeline_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    component_results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    document: Mapped[Document] = relationship(back_populates="operations")
    batch: Mapped[JobBatch | None] = relationship(back_populates="operations")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_document_time", "document_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("queue_operations.id", ondelete="RESTRICT"), nullable=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_batches.id", ondelete="RESTRICT"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    document: Mapped[Document | None] = relationship(back_populates="audit_events")


@event.listens_for(AuditEvent, "before_update")
def prevent_audit_update(_mapper: object, _connection: object, _target: AuditEvent) -> None:
    raise RuntimeError("audit events are append-only")


@event.listens_for(AuditEvent, "before_delete")
def prevent_audit_delete(_mapper: object, _connection: object, _target: AuditEvent) -> None:
    raise RuntimeError("audit events are append-only")
