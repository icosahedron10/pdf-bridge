from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import IntegrityError

from pdf_bridge.config import Settings
from pdf_bridge.lifecycle import validate_collection_references
from pdf_bridge.models import Document, DocumentState, LanguageCode, LanguageStatus, ScanState
from pdf_bridge.schemas import (
    BatchResultsRequest,
    LanguageClassificationResult,
    OperationResultInput,
)


def _settings_values(storage_root: Path) -> dict[str, object]:
    return {
        "app_env": "test",
        "storage_root": storage_root,
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": SecretStr("collection-test-session-secret-32-characters"),
        "job_token": SecretStr("collection-test-job-token-32-characters-long"),
    }


def test_collections_are_required_and_validated_before_storage_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PDF_BRIDGE_COLLECTIONS")
    storage_root = tmp_path / "missing-collections"

    with pytest.raises(ValidationError, match="collections"):
        Settings(**_settings_values(storage_root))

    assert not storage_root.exists()


@pytest.mark.parametrize("key", ["HR", "hr/docs", "hr docs", ".", "-internal"])
def test_collection_keys_must_be_lowercase_and_path_safe(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValidationError, match="collection key"):
        Settings(
            **_settings_values(tmp_path / key.replace("/", "-")),
            collections=[
                {
                    "key": key,
                    "display_name": "Internal",
                    "description": "Employee-only material.",
                    "audience": "internal",
                }
            ],
        )


def test_collection_keys_are_unique(tmp_path: Path) -> None:
    definition = {
        "key": "internal",
        "display_name": "Internal",
        "description": "Employee-only material.",
        "audience": "internal",
    }
    with pytest.raises(ValidationError, match="must be unique"):
        Settings(
            **_settings_values(tmp_path / "duplicate-collections"),
            collections=[definition, definition],
        )


def test_collection_registry_is_bounded_by_search_contract(tmp_path: Path) -> None:
    collections = [
        {
            "key": f"collection-{index}",
            "display_name": f"Collection {index}",
            "description": "A configured corpus boundary.",
            "audience": "customer",
        }
        for index in range(51)
    ]
    with pytest.raises(ValidationError, match="at most 50"):
        Settings(
            **_settings_values(tmp_path / "too-many-collections"),
            collections=collections,
        )


def test_active_collection_references_are_validated(session_factory) -> None:
    with session_factory() as session:
        review = Document(
            original_filename="legacy-review.pdf",
            normalized_filename="legacy-review.pdf",
            size_bytes=10,
            sha256="b" * 64,
            idempotency_key="legacy-review-reference",
            state=DocumentState.CLASSIFICATION_REVIEW,
            scan_state=ScanState.CLEAN,
            uploader_identity="model-test",
        )
        active = Document(
            original_filename="active.pdf",
            normalized_filename="active.pdf",
            size_bytes=10,
            sha256="c" * 64,
            idempotency_key="active-reference",
            state=DocumentState.QUEUED,
            scan_state=ScanState.CLEAN,
            uploader_identity="model-test",
        )
        session.add_all([review, active])
        session.flush()

        with pytest.raises(RuntimeError, match="without a collection"):
            validate_collection_references(session, {"customer"})

        active.collection_key = "retired"
        session.flush()
        with pytest.raises(RuntimeError, match="unconfigured collections: retired"):
            validate_collection_references(session, {"customer"})

        active.collection_key = "customer"
        session.flush()
        validate_collection_references(session, {"customer"})


@pytest.mark.parametrize(
    "payload",
    [
        {
            "language": "en",
            "status": "detected",
            "method": "   ",
        },
        {
            "operation_id": "00000000-0000-0000-0000-000000000001",
            "outcome": "failed",
            "components": {
                "pdf_source": "failed",
                "markdown": "not_applicable",
                "bm25": "not_applicable",
                "dense": "not_applicable",
            },
            "error": "   ",
        },
        {
            "operation_id": "00000000-0000-0000-0000-000000000001",
            "outcome": "failed",
            "components": {
                "pdf_source": "succeeded",
                "markdown": "succeeded",
                "bm25": "succeeded",
                "dense": "succeeded",
            },
            "classification": {
                "language": "und",
                "status": "review_required",
                "method": "downstream-parser",
                "reason": "low_confidence",
            },
            "error": "indexing failed after classification",
        },
    ],
    ids=[
        "blank-classification-method",
        "blank-failure-error",
        "review-classification-with-failed-outcome",
    ],
)
def test_pipeline_evidence_must_be_nonblank(payload: dict[str, object]) -> None:
    model = (
        LanguageClassificationResult
        if "language" in payload
        else OperationResultInput
    )
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_pipeline_run_id_must_be_nonblank() -> None:
    with pytest.raises(ValidationError, match="pipeline_run_id"):
        BatchResultsRequest.model_validate(
            {
                "pipeline_run_id": "   ",
                "results": [
                    {
                        "operation_id": "00000000-0000-0000-0000-000000000001",
                        "outcome": "failed",
                        "components": {
                            "pdf_source": "failed",
                            "markdown": "not_applicable",
                            "bm25": "not_applicable",
                            "dense": "not_applicable",
                        },
                        "error": "parser failed",
                    }
                ],
            }
        )


def test_document_language_defaults_indexes_and_confidence_constraint(
    session_factory,
) -> None:
    with session_factory() as session:
        document = Document(
            original_filename="language.pdf",
            normalized_filename="language.pdf",
            size_bytes=10,
            sha256="a" * 64,
            idempotency_key="language-model-test",
            state=DocumentState.QUEUED,
            scan_state=ScanState.CLEAN,
            uploader_identity="model-test",
        )
        session.add(document)
        session.flush()

        assert document.language is LanguageCode.UND
        assert document.language_status is LanguageStatus.PENDING
        assert document.collection_key is None

        document.language_confidence = 1.01
        with pytest.raises(IntegrityError):
            session.flush()

    inspector = sa.inspect(session_factory.kw["bind"])
    indexes = {index["name"] for index in inspector.get_indexes("documents")}
    assert "ix_documents_collection_state" in indexes
    assert "ix_documents_collection_language_state" in indexes


def test_migration_moves_only_active_legacy_documents_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("PDF_BRIDGE_DATABASE_URL", database_url)
    alembic = Config(str(Path(__file__).parents[1] / "alembic.ini"))

    command.upgrade(alembic, "0001_initial_catalog")
    engine = sa.create_engine(database_url)
    now = "2026-07-11 12:00:00+00:00"
    document_ids: dict[str, str] = {}
    with engine.begin() as connection:
        for state in ("INGESTED", "DELETED", "CANCELLED"):
            document_id = uuid.uuid4().hex
            document_ids[state] = document_id
            connection.execute(
                sa.text(
                    """
                    INSERT INTO documents (
                        id, original_filename, normalized_filename, size_bytes, sha256,
                        content_type, idempotency_key, state, scan_state, uploader_identity,
                        uploaded_at, updated_at
                    ) VALUES (
                        :id, :filename, :filename, 10, :sha256,
                        'application/pdf', :idempotency_key, :state, 'CLEAN',
                        'migration-test', :now, :now
                    )
                    """
                ),
                {
                    "id": document_id,
                    "filename": f"{state.lower()}.pdf",
                    "sha256": state[0].lower() * 64,
                    "idempotency_key": f"legacy-{state.lower()}",
                    "state": state,
                    "now": now,
                },
            )
        batch_id = uuid.uuid4().hex
        connection.execute(
            sa.text(
                """
                INSERT INTO job_batches (
                    id, request_id, state, manifest_version, operation_count,
                    created_at, claimed_at, lease_expires_at
                ) VALUES (
                    :id, 'legacy-batch', 'STAGED', 1, 4, :now, :now, :now
                )
                """
            ),
            {"id": batch_id, "now": now},
        )
        for attempt, state in enumerate(("QUEUED", "CLAIMED", "STAGED"), start=1):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO queue_operations (
                        id, document_id, batch_id, operation_type, state, attempt,
                        created_at, updated_at, claimed_at, lease_expires_at, staged_at,
                        pipeline_run_id
                    ) VALUES (
                        :id, :document_id, :batch_id, 'INGEST', :state, :attempt,
                        :now, :now, :now, :now, :now, 'legacy-run'
                    )
                    """
                ),
                {
                    "id": uuid.uuid4().hex,
                    "document_id": document_ids["INGESTED"],
                    "batch_id": batch_id,
                    "state": state,
                    "attempt": attempt,
                    "now": now,
                },
            )
        connection.execute(
            sa.text(
                """
                INSERT INTO queue_operations (
                    id, document_id, batch_id, operation_type, state, attempt,
                    created_at, updated_at, claimed_at, lease_expires_at, staged_at,
                    pipeline_run_id
                ) VALUES (
                    :id, :document_id, :batch_id, 'DELETE', 'STAGED', 4,
                    :now, :now, :now, :now, :now, 'legacy-delete-run'
                )
                """
            ),
            {
                "id": uuid.uuid4().hex,
                "document_id": document_ids["INGESTED"],
                "batch_id": batch_id,
                "now": now,
            },
        )

    command.upgrade(alembic, "head")
    with engine.connect() as connection:
        rows = {
            row.id: row
            for row in connection.execute(
                sa.text(
                    "SELECT id, state, collection_key, language, language_status FROM documents"
                )
            ).mappings()
        }

    active = rows[document_ids["INGESTED"]]
    assert active.state == "CLASSIFICATION_REVIEW"
    assert active.collection_key is None
    assert active.language == "und"
    assert active.language_status == "review_required"
    assert rows[document_ids["DELETED"]].state == "DELETED"
    assert rows[document_ids["DELETED"]].language_status == "pending"
    assert rows[document_ids["CANCELLED"]].state == "CANCELLED"
    assert rows[document_ids["CANCELLED"]].language_status == "pending"
    upgraded_columns = {
        column["name"]: column for column in sa.inspect(engine).get_columns("documents")
    }
    assert upgraded_columns["language_reason"]["type"].length == 500

    with engine.connect() as connection:
        operations = list(
            connection.execute(
                sa.text(
                    """
                    SELECT operation_type, state, batch_id, claimed_at, lease_expires_at, staged_at,
                           completed_at, pipeline_run_id
                    FROM queue_operations
                    ORDER BY attempt
                    """
                )
            ).mappings()
        )
        batch = connection.execute(
            sa.text("SELECT state, operation_count FROM job_batches WHERE id = :id"),
            {"id": batch_id},
        ).mappings().one()
    assert len(operations) == 4
    assert all(
        operation.state == "REVIEW_REQUIRED"
        for operation in operations
        if operation.operation_type == "INGEST"
    )
    assert next(
        operation for operation in operations if operation.operation_type == "DELETE"
    ).state == "CANCELLED"
    assert all(operation.batch_id is None for operation in operations)
    assert all(operation.claimed_at is None for operation in operations)
    assert all(operation.lease_expires_at is None for operation in operations)
    assert all(operation.staged_at is None for operation in operations)
    assert all(operation.completed_at is not None for operation in operations)
    assert all(operation.pipeline_run_id is None for operation in operations)
    assert batch.state == "EXPIRED"
    assert batch.operation_count == 0

    command.downgrade(alembic, "0001_initial_catalog")
    columns = {column["name"] for column in sa.inspect(engine).get_columns("documents")}
    assert "collection_key" not in columns
    assert "language" not in columns
    engine.dispose()
