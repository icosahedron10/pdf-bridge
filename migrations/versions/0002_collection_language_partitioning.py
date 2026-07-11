"""Add collection partitioning and document language classification.

Revision ID: 0002_collection_language_partitioning
Revises: 0001_initial_catalog
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_collection_language_partitioning"
down_revision: str | None = "0001_initial_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


old_document_state = sa.Enum(
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
    "CLASSIFICATION_REVIEW",
    name="document_state",
    native_enum=False,
    create_constraint=True,
)
old_operation_state = sa.Enum(
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
operation_state = sa.Enum(
    "QUEUED",
    "CLAIMED",
    "STAGED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "REVIEW_REQUIRED",
    name="operation_state",
    native_enum=False,
    create_constraint=True,
)
language_code = sa.Enum(
    "und",
    "en",
    "fr",
    name="language_code",
    native_enum=False,
    create_constraint=False,
)
language_status = sa.Enum(
    "pending",
    "detected",
    "review_required",
    "overridden",
    name="language_status",
    native_enum=False,
    create_constraint=False,
)


def upgrade() -> None:
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.alter_column(
            "state",
            existing_type=old_document_state,
            type_=document_state,
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("collection_key", sa.String(length=63), nullable=True))
        batch_op.add_column(
            sa.Column(
                "language",
                language_code,
                server_default=sa.text("'und'"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "language_status",
                language_status,
                server_default=sa.text("'pending'"),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("language_method", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("language_confidence", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("language_reason", sa.String(length=500), nullable=True))
        batch_op.add_column(
            sa.Column("language_detected_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_documents_language_code"),
            "language IN ('und', 'en', 'fr')",
        )
        batch_op.create_check_constraint(
            op.f("ck_documents_language_status"),
            "language_status IN ('pending', 'detected', 'review_required', 'overridden')",
        )
        batch_op.create_check_constraint(
            op.f("ck_documents_language_confidence_range"),
            "language_confidence IS NULL OR "
            "(language_confidence >= 0 AND language_confidence <= 1)",
        )
        batch_op.create_index(
            "ix_documents_collection_state", ["collection_key", "state"], unique=False
        )
        batch_op.create_index(
            "ix_documents_collection_language_state",
            ["collection_key", "language", "state"],
            unique=False,
        )

    with op.batch_alter_table("queue_operations", schema=None) as batch_op:
        batch_op.alter_column(
            "state",
            existing_type=old_operation_state,
            type_=operation_state,
            existing_nullable=False,
        )

    # Legacy documents cannot safely enter a collection until an operator assigns one.
    # Deleted/cancelled rows are retained as immutable tombstones and need no review work.
    op.execute(
        sa.text(
            """
            UPDATE documents
            SET state = 'CLASSIFICATION_REVIEW',
                collection_key = NULL,
                language = 'und',
                language_status = 'review_required',
                language_method = NULL,
                language_confidence = NULL,
                language_reason = NULL,
                language_detected_at = NULL
            WHERE state NOT IN ('DELETED', 'CANCELLED')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE job_batches
            SET state = 'EXPIRED',
                operation_count = 0,
                pipeline_run_id = NULL
            WHERE id IN (
                SELECT DISTINCT batch_id
                FROM queue_operations
                WHERE batch_id IS NOT NULL
                  AND state IN ('QUEUED', 'CLAIMED', 'STAGED')
                  AND document_id IN (
                      SELECT id FROM documents WHERE state = 'CLASSIFICATION_REVIEW'
                  )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE queue_operations
            SET state = 'REVIEW_REQUIRED',
                batch_id = NULL,
                updated_at = CURRENT_TIMESTAMP,
                claimed_at = NULL,
                lease_expires_at = NULL,
                staged_at = NULL,
                completed_at = CURRENT_TIMESTAMP,
                pipeline_run_id = NULL
            WHERE operation_type = 'INGEST'
              AND state IN ('QUEUED', 'CLAIMED', 'STAGED')
              AND document_id IN (
                  SELECT id FROM documents WHERE state = 'CLASSIFICATION_REVIEW'
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE queue_operations
            SET state = 'CANCELLED',
                batch_id = NULL,
                updated_at = CURRENT_TIMESTAMP,
                claimed_at = NULL,
                lease_expires_at = NULL,
                staged_at = NULL,
                completed_at = CURRENT_TIMESTAMP,
                pipeline_run_id = NULL
            WHERE operation_type = 'DELETE'
              AND state IN ('QUEUED', 'CLAIMED', 'STAGED')
              AND document_id IN (
                  SELECT id FROM documents WHERE state = 'CLASSIFICATION_REVIEW'
              )
            """
        )
    )


def downgrade() -> None:
    # The legacy schema has no review states. Preserve rows as explicit failures rather
    # than silently presenting unclassified documents as successfully ingested.
    op.execute(
        sa.text(
            "UPDATE documents SET state = 'INGEST_FAILED' "
            "WHERE state = 'CLASSIFICATION_REVIEW'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE queue_operations SET state = 'FAILED' "
            "WHERE state = 'REVIEW_REQUIRED'"
        )
    )

    with op.batch_alter_table("queue_operations", schema=None) as batch_op:
        batch_op.alter_column(
            "state",
            existing_type=operation_state,
            type_=old_operation_state,
            existing_nullable=False,
        )

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index("ix_documents_collection_language_state")
        batch_op.drop_index("ix_documents_collection_state")
        batch_op.drop_constraint(op.f("ck_documents_language_confidence_range"), type_="check")
        batch_op.drop_constraint(op.f("ck_documents_language_status"), type_="check")
        batch_op.drop_constraint(op.f("ck_documents_language_code"), type_="check")
        batch_op.drop_column("language_detected_at")
        batch_op.drop_column("language_reason")
        batch_op.drop_column("language_confidence")
        batch_op.drop_column("language_method")
        batch_op.drop_column("language_status")
        batch_op.drop_column("language")
        batch_op.drop_column("collection_key")
        batch_op.alter_column(
            "state",
            existing_type=document_state,
            type_=old_document_state,
            existing_nullable=False,
        )
