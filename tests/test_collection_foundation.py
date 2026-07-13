from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import IntegrityError

from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.models import (
    Document,
    DocumentState,
    IndexAction,
    ScanState,
)
from pdf_bridge.services.intake import validate_collection_references


def _settings_values(storage_root: Path) -> dict[str, object]:
    return {
        "app_env": "test",
        "storage_root": storage_root,
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": SecretStr("collection-test-session-secret-32-characters"),
    }


def _alembic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, str]:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'catalog.sqlite3').as_posix()}"
    monkeypatch.setenv("PDF_BRIDGE_DATABASE_URL", database_url)
    return Config(str(Path(__file__).parents[1] / "alembic.ini")), database_url


def test_collections_are_required_before_storage_is_created(
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


def test_only_retained_rows_must_reference_configured_collections(session_factory) -> None:
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
        retained = Document(
            original_filename="active.pdf",
            normalized_filename="active.pdf",
            size_bytes=10,
            sha256="d" * 64,
            idempotency_key="active-reference",
            state=DocumentState.ANALYZING,
            scan_state=ScanState.CLEAN,
            uploader_identity="model-test",
            collection_key="retired",
        )
        session.add_all([tombstone, retained])
        session.flush()

        with pytest.raises(RuntimeError, match="unconfigured collections: retired"):
            validate_collection_references(session, {"customer"})

        retained.collection_key = "customer"
        session.flush()
        validate_collection_references(session, {"customer"})


def test_model_schema_contains_semantic_intake_tables(session_factory) -> None:
    inspector = sa.inspect(session_factory.kw["bind"])
    tables = set(inspector.get_table_names())
    assert {
        "documents",
        "work_operations",
        "document_analyses",
        "analysis_chunks",
        "analysis_candidates",
        "candidate_findings",
        "intake_decisions",
        "replacement_workflows",
        "document_artifacts",
        "index_outbox",
        "collection_epochs",
        "audit_events",
    } <= tables
    assert "job_batches" not in tables
    assert "queue_operations" not in tables
    assert IndexAction.PUBLISH.value == "PUBLISH"
    artifact_columns = {
        column["name"] for column in inspector.get_columns("document_artifacts")
    }
    assert "analysis_id" in artifact_columns
    assert "document_id" not in artifact_columns
    artifact_foreign_keys = inspector.get_foreign_keys("document_artifacts")
    assert any(
        key["constrained_columns"] == ["analysis_id"]
        and key["referred_table"] == "document_analyses"
        and key["options"].get("ondelete") == "CASCADE"
        for key in artifact_foreign_keys
    )
    artifact_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("document_artifacts")
    }
    assert ("analysis_id", "kind") in artifact_uniques


def test_blank_semantic_intake_migration_upgrades_and_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alembic, database_url = _alembic(tmp_path, monkeypatch)
    command.upgrade(alembic, "head")

    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    assert "work_operations" in inspector.get_table_names()
    assert "queue_operations" not in inspector.get_table_names()
    assert {
        column["name"] for column in inspector.get_columns("document_artifacts")
    } >= {"analysis_id", "kind", "storage_key", "sha256", "size_bytes"}
    assert "document_id" not in {
        column["name"] for column in inspector.get_columns("document_artifacts")
    }
    assert inspector.get_foreign_keys("intake_decisions") == [
        key
        for key in inspector.get_foreign_keys("intake_decisions")
        if key["referred_table"] != "document_analyses"
    ]

    command.downgrade(alembic, "base")
    assert "documents" not in sa.inspect(engine).get_table_names()
    engine.dispose()
