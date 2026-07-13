"""Add required collection partitioning to an empty disposable catalog.

Revision ID: 0002_collection_partitioning
Revises: 0001_initial_catalog
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_collection_partitioning"
down_revision: str | None = "0001_initial_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Require collection keys, but only after the disposable catalog is reset."""

    document_count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM documents")
    ).scalar_one()
    if document_count:
        raise RuntimeError(
            "reset required: 0002_collection_partitioning cannot upgrade a nonempty "
            "version-1 catalog; stop bridge and job consumers, clear the disposable catalog "
            "and downstream corpus, then rerun the migration"
        )

    with op.batch_alter_table("documents", schema=None, recreate="always") as batch_op:
        batch_op.add_column(sa.Column("collection_key", sa.String(length=63), nullable=False))
        batch_op.create_index(
            "ix_documents_collection_state", ["collection_key", "state"], unique=False
        )


def downgrade() -> None:
    """Remove collection partitioning from the empty version-1 catalog shape."""

    with op.batch_alter_table("documents", schema=None, recreate="always") as batch_op:
        batch_op.drop_index("ix_documents_collection_state")
        batch_op.drop_column("collection_key")
