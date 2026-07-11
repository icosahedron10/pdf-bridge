"""Create the PDF catalog, operation queue, batches, and audit ledger.

Revision ID: 0001_initial_catalog
Revises: None
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_catalog"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


document_state = sa.Enum(
    "QUEUED",
    "CLAIMED",
    "STAGED",
    "INGESTED",
    "INGEST_FAILED",
    "DELETE_QUEUED",
    "DELETE_CLAIMED",
    "DELETE_CLEANUP",
    "DELETED",
    "DELETE_FAILED",
    "CANCEL_CLEANUP",
    "CANCELLED",
    name="document_state",
    native_enum=False,
    create_constraint=True,
)
scan_state = sa.Enum(
    "PENDING",
    "CLEAN",
    "INFECTED",
    "ERROR",
    name="scan_state",
    native_enum=False,
    create_constraint=True,
)
batch_state = sa.Enum(
    "EMPTY",
    "CLAIMED",
    "STAGED",
    "COMPLETED",
    "PARTIAL",
    "FAILED",
    "EXPIRED",
    name="batch_state",
    native_enum=False,
    create_constraint=True,
)
operation_type = sa.Enum(
    "INGEST",
    "DELETE",
    name="operation_type",
    native_enum=False,
    create_constraint=True,
)
operation_state = sa.Enum(
    "QUEUED",
    "CLAIMED",
    "STAGED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    name="operation_state",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("normalized_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("state", document_state, nullable=False),
        sa.Column("scan_state", scan_state, nullable=False),
        sa.Column("scan_engine", sa.String(length=100), nullable=True),
        sa.Column("scan_signature", sa.String(length=255), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploader_identity", sa.String(length=255), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pipeline_run_id", sa.String(length=255), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("pipeline_metadata", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("length(sha256) = 64", name=op.f("ck_documents_sha256_length")),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_documents_size_bytes_nonnegative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_documents_idempotency_key")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_documents_storage_key")),
    )
    op.create_index(
        "ix_documents_normalized_filename_size",
        "documents",
        ["normalized_filename", "size_bytes"],
        unique=False,
    )
    op.create_index("ix_documents_sha256", "documents", ["sha256"], unique=False)
    op.create_index("ix_documents_sha256_state", "documents", ["sha256", "state"], unique=False)
    op.create_index("ix_documents_state", "documents", ["state"], unique=False)

    op.create_table(
        "job_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("state", batch_state, nullable=False),
        sa.Column("manifest_version", sa.Integer(), nullable=False),
        sa.Column("operation_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("staged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pipeline_run_id", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "operation_count >= 0", name=op.f("ck_job_batches_operation_count_nonnegative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_batches")),
    )
    op.create_index("ix_job_batches_request_id", "job_batches", ["request_id"], unique=True)

    op.create_table(
        "queue_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("operation_type", operation_type, nullable=False),
        sa.Column("state", operation_state, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("staged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pipeline_run_id", sa.String(length=255), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("component_results", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_queue_operations_attempt_positive")),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["job_batches.id"],
            name=op.f("fk_queue_operations_batch_id_job_batches"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_queue_operations_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_operations")),
        sa.UniqueConstraint(
            "document_id",
            "operation_type",
            "attempt",
            name=op.f("uq_queue_operations_document_id"),
        ),
    )
    op.create_index(
        "ix_queue_operations_document_id", "queue_operations", ["document_id"], unique=False
    )
    op.create_index(
        "ix_queue_operations_operation_type", "queue_operations", ["operation_type"], unique=False
    )
    op.create_index("ix_queue_operations_state", "queue_operations", ["state"], unique=False)
    op.create_index(
        "ix_queue_operations_state_created",
        "queue_operations",
        ["state", "created_at"],
        unique=False,
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("actor_type", sa.String(length=50), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["job_batches.id"],
            name=op.f("fk_audit_events_batch_id_job_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_audit_events_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["queue_operations.id"],
            name=op.f("fk_audit_events_operation_id_queue_operations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_document_time",
        "audit_events",
        ["document_id", "occurred_at"],
        unique=False,
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_document_time", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_queue_operations_state_created", table_name="queue_operations")
    op.drop_index("ix_queue_operations_state", table_name="queue_operations")
    op.drop_index("ix_queue_operations_operation_type", table_name="queue_operations")
    op.drop_index("ix_queue_operations_document_id", table_name="queue_operations")
    op.drop_table("queue_operations")
    op.drop_index("ix_job_batches_request_id", table_name="job_batches")
    op.drop_table("job_batches")
    op.drop_index("ix_documents_state", table_name="documents")
    op.drop_index("ix_documents_sha256_state", table_name="documents")
    op.drop_index("ix_documents_sha256", table_name="documents")
    op.drop_index("ix_documents_normalized_filename_size", table_name="documents")
    op.drop_table("documents")
