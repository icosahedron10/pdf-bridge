from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from pdf_bridge import admin_cli
from pdf_bridge.admin_cli import app as admin_app
from pdf_bridge.job_cli import (
    BridgeClientError,
    _local_manifest,
    _stage_new_batch,
)
from pdf_bridge.job_cli import (
    app as job_app,
)
from pdf_bridge.models import BatchState, OperationType
from pdf_bridge.schemas import BatchManifestItem, BatchManifestResponse
from tests.conftest import PDF_A, clean_scanner

runner = CliRunner()


@respx.mock
def test_job_pull_streams_verifies_and_atomically_stages(tmp_path: Path, monkeypatch) -> None:
    batch_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    document_id = uuid.uuid4()
    now = datetime.now(UTC)
    base = "https://bridge.test"
    sha256 = __import__("hashlib").sha256(PDF_A).hexdigest()
    respx.post(f"{base}/api/v1/jobs/batches/claim").mock(
        return_value=httpx.Response(
            200,
            json={
                "batch_id": str(batch_id),
                "request_id": "jenkins-cli-test",
                "state": "CLAIMED",
                "claimed_at": now.isoformat(),
                "lease_expires_at": (now + timedelta(minutes=30)).isoformat(),
                "operation_count": 1,
                "idempotent_replay": False,
            },
        )
    )
    respx.get(f"{base}/api/v1/jobs/batches/{batch_id}/manifest").mock(
        return_value=httpx.Response(
            200,
            json={
                "version": 1,
                "batch_id": str(batch_id),
                "request_id": "jenkins-cli-test",
                "state": "CLAIMED",
                "claimed_at": now.isoformat(),
                "lease_expires_at": (now + timedelta(minutes=30)).isoformat(),
                "operations": [
                    {
                        "operation_id": str(operation_id),
                        "document_id": str(document_id),
                        "operation_type": "INGEST",
                        "filename": "handbook.pdf",
                        "size_bytes": len(PDF_A),
                        "sha256": sha256,
                        "download_url": (
                            f"/api/v1/jobs/batches/{batch_id}/operations/{operation_id}/content"
                        ),
                    }
                ],
            },
        )
    )
    respx.get(f"{base}/api/v1/jobs/batches/{batch_id}/operations/{operation_id}/content").mock(
        return_value=httpx.Response(200, content=PDF_A)
    )
    respx.post(f"{base}/api/v1/jobs/batches/{batch_id}/staged").mock(
        return_value=httpx.Response(
            200,
            json={
                "batch_id": str(batch_id),
                "state": "STAGED",
                "staged_at": now.isoformat(),
                "operation_count": 1,
                "idempotent_replay": False,
            },
        )
    )
    monkeypatch.setenv("PDF_BRIDGE_JOB_TOKEN", "job-cli-secret")
    destination = tmp_path / "handoff"
    result_path = tmp_path / "pull-result.json"
    result = runner.invoke(
        job_app,
        [
            "pull",
            "--destination",
            str(destination),
            "--base-url",
            base,
            "--allowed-host",
            "bridge.test",
            "--request-id",
            "jenkins-cli-test",
            "--result-file",
            str(result_path),
        ],
    )
    assert result.exit_code == 0, result.output
    batch_directory = destination / str(batch_id)
    assert (batch_directory / "manifest.json").is_file()
    assert (batch_directory / "files" / f"{operation_id}.pdf").read_bytes() == PDF_A
    assert json.loads(result_path.read_text(encoding="utf-8"))["operation_count"] == 1


@respx.mock
def test_job_report_validates_and_submits_results(tmp_path: Path, monkeypatch) -> None:
    batch_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    now = datetime.now(UTC)
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "version": 1,
                "batch_id": str(batch_id),
                "pipeline_run_id": "pipeline-cli-test",
                "results": [
                    {
                        "operation_id": str(operation_id),
                        "success": True,
                        "chunk_count": 3,
                        "components": {
                            "pdf_source": "succeeded",
                            "markdown": "succeeded",
                            "bm25": "succeeded",
                            "dense": "succeeded",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pull_result_path = tmp_path / "pull-result.json"
    pull_result_path.write_text(
        json.dumps(
            {
                "version": 1,
                "batch_id": str(batch_id),
                "request_id": "jenkins-cli-test",
                "operation_count": 1,
                "batch_directory": str(tmp_path / str(batch_id)),
                "manifest_sha256": "a" * 64,
                "idempotent_replay": False,
            }
        ),
        encoding="utf-8",
    )
    respx.post(f"https://bridge.test/api/v1/jobs/batches/{batch_id}/results").mock(
        return_value=httpx.Response(
            200,
            json={
                "batch_id": str(batch_id),
                "state": "COMPLETED",
                "completed_at": now.isoformat(),
                "succeeded": 1,
                "failed": 0,
                "idempotent_replay": False,
            },
        )
    )
    monkeypatch.setenv("PDF_BRIDGE_JOB_TOKEN", "job-cli-secret")
    result = runner.invoke(
        job_app,
        [
            "report",
            str(report_path),
            "--pull-result",
            str(pull_result_path),
            "--base-url",
            "https://bridge.test",
            "--allowed-host",
            "bridge.test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"succeeded": 1' in result.output


@respx.mock
def test_job_client_rejects_unpinned_host_before_request(tmp_path: Path, monkeypatch) -> None:
    unexpected = respx.post("https://elsewhere.test/api/v1/jobs/batches/claim").mock(
        return_value=httpx.Response(204)
    )
    monkeypatch.setenv("PDF_BRIDGE_JOB_TOKEN", "must-not-be-sent")
    result = runner.invoke(
        job_app,
        [
            "pull",
            "--destination",
            str(tmp_path / "handoff"),
            "--base-url",
            "https://elsewhere.test",
            "--allowed-host",
            "bridge.test",
            "--request-id",
            "jenkins-host-pin",
        ],
    )
    assert result.exit_code == 1
    assert "refusing to send the job token" in result.output
    assert unexpected.called is False


@respx.mock
def test_job_report_rejects_batch_mismatch_before_request(tmp_path: Path, monkeypatch) -> None:
    report_batch_id = uuid.uuid4()
    pull_batch_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "version": 1,
                "batch_id": str(report_batch_id),
                "pipeline_run_id": "pipeline-mismatch-test",
                "results": [
                    {
                        "operation_id": str(operation_id),
                        "success": True,
                        "chunk_count": 1,
                        "components": {
                            "pdf_source": "succeeded",
                            "markdown": "succeeded",
                            "bm25": "succeeded",
                            "dense": "succeeded",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pull_result_path = tmp_path / "pull-result.json"
    pull_result_path.write_text(
        json.dumps(
            {
                "version": 1,
                "batch_id": str(pull_batch_id),
                "request_id": "jenkins-mismatch-test",
                "operation_count": 1,
                "batch_directory": str(tmp_path / str(pull_batch_id)),
                "manifest_sha256": "b" * 64,
                "idempotent_replay": False,
            }
        ),
        encoding="utf-8",
    )
    unexpected = respx.post(
        f"https://bridge.test/api/v1/jobs/batches/{report_batch_id}/results"
    ).mock(return_value=httpx.Response(500))
    monkeypatch.setenv("PDF_BRIDGE_JOB_TOKEN", "must-not-be-sent")
    result = runner.invoke(
        job_app,
        [
            "report",
            str(report_path),
            "--pull-result",
            str(pull_result_path),
            "--base-url",
            "https://bridge.test",
            "--allowed-host",
            "bridge.test",
        ],
    )
    assert result.exit_code == 1
    assert "does not match the current pull result" in result.output
    assert unexpected.called is False


def test_job_staging_removes_partial_batch_after_checksum_failure(tmp_path: Path) -> None:
    batch_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    now = datetime.now(UTC)
    remote = BatchManifestResponse(
        batch_id=batch_id,
        request_id="jenkins-checksum-test",
        state=BatchState.CLAIMED,
        claimed_at=now,
        lease_expires_at=now + timedelta(minutes=30),
        operations=[
            BatchManifestItem(
                operation_id=operation_id,
                document_id=uuid.uuid4(),
                operation_type=OperationType.INGEST,
                filename="checksum.pdf",
                size_bytes=len(PDF_A),
                sha256=__import__("hashlib").sha256(PDF_A).hexdigest(),
                download_url=f"/operations/{operation_id}/content",
            )
        ],
    )

    class WrongContentClient:
        @contextmanager
        def stream_operation(self, _download_url: str):
            yield httpx.Response(
                200,
                content=b"X" * len(PDF_A),
                request=httpx.Request("GET", "https://bridge.test/content"),
            )

    destination = tmp_path / "handoff"
    destination.mkdir()
    with pytest.raises(BridgeClientError, match="checksum mismatch"):
        _stage_new_batch(
            WrongContentClient(),
            destination,
            remote,
            _local_manifest(remote),
        )
    assert not (destination / str(batch_id)).exists()
    assert list(destination.iterdir()) == []


def test_admin_import_manifest_dry_run(
    tmp_path: Path,
    monkeypatch,
    settings,
    session_factory: sessionmaker[Session],
) -> None:
    source_root = tmp_path / "approved"
    source_root.mkdir()
    (source_root / "existing.pdf").write_bytes(PDF_A)
    manifest = tmp_path / "historical.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "documents": [{"path": "existing.pdf", "filename": "Existing.pdf"}],
            }
        ),
        encoding="utf-8",
    )

    @contextmanager
    def test_scope():
        with session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(admin_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(admin_cli, "scanner_from_settings", lambda _settings: clean_scanner)
    monkeypatch.setattr(admin_cli, "session_scope", test_scope)
    result = runner.invoke(
        admin_app,
        [
            "import-manifest",
            str(manifest),
            "--source-root",
            str(source_root),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["imported"] == 0
    assert payload["items"][0]["filename"] == "Existing.pdf"
