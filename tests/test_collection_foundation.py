from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import IntegrityError

from pdf_bridge.contracts.schemas import (
    BatchResultsRequest,
    BatchResultsResponse,
    OperationResultInput,
)
from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.models import Document, DocumentState, ScanState
from pdf_bridge.services.lifecycle import validate_collection_references

LANGUAGE_COLUMNS = {
    "language",
    "language_status",
    "language_method",
    "language_confidence",
    "language_reason",
    "language_detected_at",
}


def _settings_values(storage_root: Path) -> dict[str, object]:
    return {
        "app_env": "test",
        "storage_root": storage_root,
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": SecretStr("collection-test-session-secret-32-characters"),
        "job_token": SecretStr("collection-test-job-token-32-characters-long"),
    }


def _alembic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[Config, str]:
    database_path = tmp_path / name
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("PDF_BRIDGE_DATABASE_URL", database_url)
    return Config(str(Path(__file__).parents[1] / "alembic.ini")), database_url


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


def test_every_document_requires_collection_but_only_active_keys_are_configured(
    session_factory,
) -> None:
    with session_factory() as session:
        missing_collection = Document(
            original_filename="tombstone.pdf",
            normalized_filename="tombstone.pdf",
            size_bytes=10,
            sha256="b" * 64,
            idempotency_key="missing-collection-tombstone",
            state=DocumentState.DELETED,
            scan_state=ScanState.CLEAN,
            uploader_identity="model-test",
        )
        session.add(missing_collection)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        tombstone = Document(
            original_filename="retired.pdf",
            normalized_filename="retired.pdf",
            size_bytes=10,
            sha256="c" * 64,
            idempotency_key="retired-tombstone",
            state=DocumentState.DELETED,
            scan_state=ScanState.CLEAN,
            uploader_identity="model-test",
            collection_key="retired",
        )
        active = Document(
            original_filename="active.pdf",
            normalized_filename="active.pdf",
            size_bytes=10,
            sha256="d" * 64,
            idempotency_key="active-reference",
            state=DocumentState.QUEUED,
            scan_state=ScanState.CLEAN,
            uploader_identity="model-test",
            collection_key="retired",
        )
        session.add_all([tombstone, active])
        session.flush()

        with pytest.raises(RuntimeError, match="unconfigured collections: retired"):
            validate_collection_references(session, {"customer"})

        active.collection_key = "customer"
        session.flush()
        validate_collection_references(session, {"customer"})


def _failed_result(**extra: object) -> dict[str, object]:
    return {
        "operation_id": "00000000-0000-0000-0000-000000000001",
        "success": False,
        "components": {
            "pdf_source": "failed",
            "markdown": "not_applicable",
            "bm25": "not_applicable",
            "dense": "not_applicable",
        },
        "error": "parser failed",
        **extra,
    }


def test_pipeline_result_requires_consistent_success_components_and_error() -> None:
    with pytest.raises(ValidationError, match="non-whitespace"):
        OperationResultInput.model_validate(_failed_result(error="   "))
    with pytest.raises(ValidationError, match="every component"):
        OperationResultInput.model_validate(
            _failed_result(success=True, error=None)
        )
    with pytest.raises(ValidationError, match="cannot include an error"):
        OperationResultInput.model_validate(
            _failed_result(
                success=True,
                components={
                    "pdf_source": "succeeded",
                    "markdown": "succeeded",
                    "bm25": "succeeded",
                    "dense": "succeeded",
                },
            )
        )


@pytest.mark.parametrize(
    ("legacy_field", "value"),
    [
        ("language", "en"),
        ("classification", {"status": "detected"}),
        ("outcome", "failed"),
        ("review_required", False),
    ],
)
def test_pipeline_result_rejects_legacy_fields(legacy_field: str, value: object) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OperationResultInput.model_validate(_failed_result(**{legacy_field: value}))


def test_batch_response_rejects_legacy_review_counter() -> None:
    with pytest.raises(ValidationError, match="review_required"):
        BatchResultsResponse.model_validate(
            {
                "batch_id": uuid.uuid4(),
                "state": "COMPLETED",
                "completed_at": "2026-07-12T12:00:00Z",
                "succeeded": 1,
                "failed": 0,
                "review_required": 0,
            }
        )


def test_pipeline_run_id_must_be_nonblank() -> None:
    with pytest.raises(ValidationError, match="pipeline_run_id"):
        BatchResultsRequest.model_validate(
            {"pipeline_run_id": "   ", "results": [_failed_result()]}
        )


def test_model_schema_has_only_collection_partitioning(session_factory) -> None:
    inspector = sa.inspect(session_factory.kw["bind"])
    columns = {column["name"]: column for column in inspector.get_columns("documents")}
    indexes = {index["name"] for index in inspector.get_indexes("documents")}

    assert columns["collection_key"]["nullable"] is False
    assert LANGUAGE_COLUMNS.isdisjoint(columns)
    assert "ix_documents_collection_state" in indexes
    assert "ix_documents_collection_language_state" not in indexes


def test_blank_migration_upgrade_and_downgrade_are_collection_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alembic, database_url = _alembic(tmp_path, monkeypatch, "blank.sqlite3")
    command.upgrade(alembic, "head")

    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("documents")}
    indexes = {index["name"] for index in inspector.get_indexes("documents")}
    document_checks = " ".join(
        constraint["sqltext"] for constraint in inspector.get_check_constraints("documents")
    )
    operation_checks = " ".join(
        constraint["sqltext"]
        for constraint in inspector.get_check_constraints("queue_operations")
    )

    assert columns["collection_key"]["nullable"] is False
    assert LANGUAGE_COLUMNS.isdisjoint(columns)
    assert "ix_documents_collection_state" in indexes
    assert "ix_documents_collection_language_state" not in indexes
    assert "CLASSIFICATION_REVIEW" not in document_checks
    assert "REVIEW_REQUIRED" not in operation_checks

    command.downgrade(alembic, "0001_initial_catalog")
    assert "collection_key" not in {
        column["name"] for column in sa.inspect(engine).get_columns("documents")
    }
    engine.dispose()


def test_nonempty_version_one_catalog_requires_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alembic, database_url = _alembic(tmp_path, monkeypatch, "nonempty.sqlite3")
    command.upgrade(alembic, "0001_initial_catalog")
    engine = sa.create_engine(database_url)
    now = "2026-07-12 12:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO documents (
                    id, original_filename, normalized_filename, size_bytes, sha256,
                    content_type, idempotency_key, state, scan_state, uploader_identity,
                    uploaded_at, updated_at
                ) VALUES (
                    :id, 'legacy.pdf', 'legacy.pdf', 10, :sha256,
                    'application/pdf', 'legacy-row', 'INGESTED', 'CLEAN',
                    'migration-test', :now, :now
                )
                """
            ),
            {"id": uuid.uuid4().hex, "sha256": "a" * 64, "now": now},
        )

    with pytest.raises(RuntimeError, match="reset required.*nonempty version-1 catalog"):
        command.upgrade(alembic, "head")

    columns = {column["name"] for column in sa.inspect(engine).get_columns("documents")}
    assert "collection_key" not in columns
    engine.dispose()
