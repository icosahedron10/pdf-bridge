from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from pdf_bridge.contracts.job_contracts import BridgeClientError
from pdf_bridge.contracts.schemas import BatchManifestItem, BatchManifestResponse
from pdf_bridge.controllers import admin_cli
from pdf_bridge.controllers.admin_cli import app as admin_app
from pdf_bridge.controllers.job_cli import (
    app as job_app,
)
from pdf_bridge.managers import importing
from pdf_bridge.managers.importing import HistoricalImportCommitError, run_manifest_import
from pdf_bridge.persistence.models import AuditEvent, BatchState, Document, OperationType
from pdf_bridge.services.job_http import _problem_message
from pdf_bridge.services.job_staging import _local_manifest, _stage_new_batch
from pdf_bridge.services.storage import StorageLayout
from tests.conftest import PDF_A, PDF_B, clean_scanner

runner = CliRunner()


def test_job_client_reads_native_litestar_error_detail() -> None:
    response = httpx.Response(
        400,
        headers={
            "content-type": "application/json",
            "x-request-id": "request-native-error",
        },
        json={"status_code": 400, "detail": "The request body was invalid."},
    )

    assert _problem_message(response) == (
        "400 Bad Request: The request body was invalid. (request request-native-error)"
    )


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
                "version": 2,
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
                        "collection_key": "customer",
                        "relative_path": f"pdfs/customer/{document_id}.pdf",
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
    staged_manifest = json.loads((batch_directory / "manifest.json").read_text(encoding="utf-8"))
    assert staged_manifest["version"] == 2
    assert staged_manifest["operations"][0]["relative_path"] == (
        f"pdfs/customer/{document_id}.pdf"
    )
    assert (
        batch_directory / "pdfs" / "customer" / f"{document_id}.pdf"
    ).read_bytes() == PDF_A
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
                "version": 2,
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
                "version": 2,
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
    document_id = uuid.uuid4()
    now = datetime.now(UTC)
    remote = BatchManifestResponse(
        version=2,
        batch_id=batch_id,
        request_id="jenkins-checksum-test",
        state=BatchState.CLAIMED,
        claimed_at=now,
        lease_expires_at=now + timedelta(minutes=30),
        operations=[
            BatchManifestItem(
                operation_id=operation_id,
                document_id=document_id,
                operation_type=OperationType.INGEST,
                filename="checksum.pdf",
                size_bytes=len(PDF_A),
                sha256=__import__("hashlib").sha256(PDF_A).hexdigest(),
                collection_key="customer",
                relative_path=f"pdfs/customer/{document_id}.pdf",
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


@pytest.mark.parametrize(
    "unsafe_path_template",
    [
        "/pdfs/customer/{document_id}.pdf",
        "../pdfs/customer/{document_id}.pdf",
        "pdfs/customer/../customer/{document_id}.pdf",
        r"pdfs\customer\{document_id}.pdf",
        "pdfs/internal/{document_id}.pdf",
        "C:/pdfs/customer/{document_id}.pdf",
    ],
)
def test_job_client_rejects_server_supplied_unsafe_relative_path(
    unsafe_path_template: str,
) -> None:
    document_id = uuid.uuid4()
    unsafe_path = unsafe_path_template.format(document_id=document_id)
    operation = BatchManifestItem(
        operation_id=uuid.uuid4(),
        document_id=document_id,
        operation_type=OperationType.INGEST,
        filename="path-test.pdf",
        size_bytes=len(PDF_A),
        sha256=__import__("hashlib").sha256(PDF_A).hexdigest(),
        collection_key="customer",
        relative_path=f"pdfs/customer/{document_id}.pdf",
        download_url="/content",
    )
    object.__setattr__(operation, "relative_path", unsafe_path)
    remote = BatchManifestResponse.model_construct(
        version=2,
        batch_id=uuid.uuid4(),
        request_id="jenkins-unsafe-path",
        state=BatchState.CLAIMED,
        claimed_at=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=30),
        operations=[operation],
    )

    with pytest.raises(BridgeClientError, match="unsafe or inconsistent relative_path"):
        _local_manifest(remote)


def test_job_client_requires_manifest_version_two() -> None:
    remote = BatchManifestResponse.model_construct(version=1)

    with pytest.raises(BridgeClientError, match="unsupported server manifest version: 1"):
        _local_manifest(remote)


def test_job_stages_delete_metadata_without_downloading(tmp_path: Path) -> None:
    batch_id = uuid.uuid4()
    document_id = uuid.uuid4()
    relative_path = f"pdfs/internal/{document_id}.pdf"
    now = datetime.now(UTC)
    remote = BatchManifestResponse(
        version=2,
        batch_id=batch_id,
        request_id="jenkins-delete-stage",
        state=BatchState.CLAIMED,
        claimed_at=now,
        lease_expires_at=now + timedelta(minutes=30),
        operations=[
            BatchManifestItem(
                operation_id=uuid.uuid4(),
                document_id=document_id,
                operation_type=OperationType.DELETE,
                filename="retired.pdf",
                size_bytes=len(PDF_A),
                sha256=__import__("hashlib").sha256(PDF_A).hexdigest(),
                collection_key="internal",
                relative_path=relative_path,
                download_url=None,
            )
        ],
    )

    class NoDownloadClient:
        def stream_operation(self, _download_url: str):
            raise AssertionError("DELETE operations must not be downloaded")

    destination = tmp_path / "handoff"
    destination.mkdir()
    final_directory, _checksum = _stage_new_batch(
        NoDownloadClient(), destination, remote, _local_manifest(remote)
    )

    staged = json.loads((final_directory / "manifest.json").read_text(encoding="utf-8"))
    assert staged["operations"][0]["relative_path"] == relative_path
    assert not (final_directory / Path(relative_path)).exists()


def _two_document_manifest(tmp_path: Path) -> tuple[Path, Path]:
    """Write two distinct source PDFs plus a two-entry apply-mode manifest."""

    source_root = tmp_path / "approved-sources"
    source_root.mkdir()
    (source_root / "first.pdf").write_bytes(PDF_A)
    (source_root / "second.pdf").write_bytes(PDF_B)
    manifest = tmp_path / "two-documents.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "documents": [
                    {
                        "path": "first.pdf",
                        "filename": "First.pdf",
                        "collection_key": "internal",
                    },
                    {
                        "path": "second.pdf",
                        "filename": "Second.pdf",
                        "collection_key": "customer",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest, source_root


@contextmanager
def _commit_failing_scope(session_factory: sessionmaker[Session]):
    """Session scope whose commit step fails after the import body succeeds."""

    with session_factory() as session:
        yield session
        raise RuntimeError("simulated commit failure")


def test_import_commit_failure_removes_every_promoted_object(
    tmp_path: Path,
    settings,
    session_factory: sessionmaker[Session],
) -> None:
    manifest, source_root = _two_document_manifest(tmp_path)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        run_manifest_import(
            manifest_path=manifest,
            source_root=source_root,
            dry_run=False,
            actor_id="import-commit-test",
            settings_provider=lambda: settings,
            scanner_factory=lambda _settings: clean_scanner,
            session_scope_factory=lambda: _commit_failing_scope(session_factory),
        )

    layout = StorageLayout.from_root(settings.storage_root)
    assert list(layout.objects.rglob("*.pdf")) == []
    assert list(layout.temporary.iterdir()) == []
    with session_factory() as session:
        assert session.scalars(select(Document)).all() == []
        assert session.scalars(select(AuditEvent)).all() == []


def test_import_cleanup_failure_is_reported_alongside_commit_failure(
    tmp_path: Path,
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    manifest, source_root = _two_document_manifest(tmp_path)

    attempted_keys: list[str] = []

    def refusing_remove(_layout, storage_key: str, *, missing_ok: bool = False) -> None:
        attempted_keys.append(storage_key)
        raise OSError("canonical storage unavailable")

    monkeypatch.setattr(importing, "remove_storage_key", refusing_remove)
    with pytest.raises(HistoricalImportCommitError, match="could not be removed") as failure:
        run_manifest_import(
            manifest_path=manifest,
            source_root=source_root,
            dry_run=False,
            actor_id="import-cleanup-test",
            settings_provider=lambda: settings,
            scanner_factory=lambda _settings: clean_scanner,
            session_scope_factory=lambda: _commit_failing_scope(session_factory),
        )

    # Every removal was attempted, both failures are reported together, and
    # the transaction failure remains the chained cause.
    assert len(attempted_keys) == 2
    for storage_key in attempted_keys:
        assert storage_key in str(failure.value)
    assert isinstance(failure.value.__cause__, RuntimeError)
    layout = StorageLayout.from_root(settings.storage_root)
    assert len(list(layout.objects.rglob("*.pdf"))) == 2


def test_import_dry_run_commit_failure_needs_no_compensation(
    tmp_path: Path,
    settings,
    session_factory: sessionmaker[Session],
) -> None:
    manifest, source_root = _two_document_manifest(tmp_path)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        run_manifest_import(
            manifest_path=manifest,
            source_root=source_root,
            dry_run=True,
            actor_id="import-dry-run-test",
            settings_provider=lambda: settings,
            scanner_factory=lambda _settings: clean_scanner,
            session_scope_factory=lambda: _commit_failing_scope(session_factory),
        )

    layout = StorageLayout.from_root(settings.storage_root)
    assert list(layout.objects.rglob("*.pdf")) == []
    assert list(layout.temporary.iterdir()) == []


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
                    "version": 2,
                    "documents": [
                        {
                            "path": "existing.pdf",
                            "filename": "Existing.pdf",
                            "collection_key": "internal",
                        }
                    ],
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
