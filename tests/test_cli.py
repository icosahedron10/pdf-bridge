from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from pdf_bridge.contracts.schemas import HistoricalImportManifest
from pdf_bridge.controllers.admin_cli import app
from pdf_bridge.persistence.models import Document, DocumentState, OperationType, WorkOperation
from pdf_bridge.services.historical_import import import_historical_manifest

from .conftest import PDF_A, PDF_B, clean_scanner


def _manifest(path: Path, documents: list[dict[str, str]]) -> Path:
    manifest = path / "manifest.json"
    manifest.write_text(json.dumps({"version": 3, "documents": documents}), encoding="utf-8")
    return manifest


def test_cli_exposes_local_import_without_jenkins_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "import-manifest" in result.stdout
    assert "pull" not in result.stdout
    assert "report" not in result.stdout


def test_manifest_requires_version_three_and_rejects_legacy_fields() -> None:
    with pytest.raises(ValidationError):
        HistoricalImportManifest.model_validate(
            {
                "version": 2,
                "documents": [
                    {
                        "path": "one.pdf",
                        "collection_key": "customer",
                        "pipeline_run_id": "legacy",
                    }
                ],
            }
        )


def test_historical_import_dry_run_creates_no_catalog_rows(
    tmp_path: Path, settings, session_factory
) -> None:
    source_root = tmp_path / "historical"
    source_root.mkdir()
    (source_root / "one.pdf").write_bytes(PDF_A)
    manifest = _manifest(
        tmp_path,
        [{"path": "one.pdf", "collection_key": "customer"}],
    )

    with session_factory() as session:
        response = import_historical_manifest(
            session,
            manifest_path=manifest,
            source_root=source_root,
            settings=settings,
            scanner=clean_scanner,
            dry_run=True,
            actor_id="migration-operator",
        )
        assert response.dry_run is True
        assert response.imported == 0
        assert response.items[0].document_id is None
        assert session.query(Document).count() == 0


def test_historical_import_apply_queues_normal_analysis_operations(
    tmp_path: Path, settings, session_factory
) -> None:
    source_root = tmp_path / "historical"
    source_root.mkdir()
    (source_root / "one.pdf").write_bytes(PDF_A)
    (source_root / "two.pdf").write_bytes(PDF_B)
    manifest = _manifest(
        tmp_path,
        [
            {"path": "one.pdf", "collection_key": "customer"},
            {"path": "two.pdf", "collection_key": "internal"},
        ],
    )

    with session_factory() as session:
        response = import_historical_manifest(
            session,
            manifest_path=manifest,
            source_root=source_root,
            settings=settings,
            scanner=clean_scanner,
            dry_run=False,
            actor_id="migration-operator",
        )
        session.commit()

        assert response.imported == 2
        documents = session.query(Document).order_by(Document.collection_key).all()
        assert {document.state for document in documents} == {DocumentState.ANALYZING}
        operations = session.query(WorkOperation).all()
        assert len(operations) == 2
        assert {operation.operation_type for operation in operations} == {
            OperationType.ANALYZE
        }
        assert all(item.document_id for item in response.items)
