from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from litestar.testing import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.app import create_app
from pdf_bridge.managers import document as document_manager
from pdf_bridge.persistence.models import (
    AuditEvent,
    Document,
    DocumentState,
    OperationState,
    OperationType,
    QueueOperation,
    ScanState,
    utc_now,
)
from pdf_bridge.services.scanner import ScannerProtocolError, ScannerUnavailableError, ScanResult
from pdf_bridge.services.storage import StorageLayout, promote_staged_file, stream_upload
from tests.conftest import PDF_A, PDF_B, clean_scanner


def test_upload_preview_duplicate_and_idempotent_replay(
    client: TestClient, csrf_headers: dict[str, str], upload_pdf
) -> None:
    response = upload_pdf()
    assert response.status_code == 201, response.text
    payload = response.json()
    document_id = payload["document"]["id"]
    operation_id = payload["operation_id"]
    assert payload["document"]["state"] == "QUEUED"

    replay = upload_pdf()
    assert replay.status_code == 201
    assert replay.json()["document"]["id"] == document_id
    assert replay.json()["idempotent_replay"] is True

    preview = client.get(f"/api/v1/documents/{document_id}/content")
    assert preview.status_code == 200
    assert preview.content == PDF_A
    assert preview.headers["content-type"].startswith("application/pdf")
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in preview.headers["content-security-policy"]
    assert preview.headers["cross-origin-resource-policy"] == "same-origin"

    exact = upload_pdf(filename="renamed.pdf", key="upload-key-0002")
    assert exact.status_code == 409
    assert exact.json()["code"] == "exact-duplicate"
    assert exact.json()["duplicate"]["document_id"] == document_id

    queue = client.get("/api/v1/queue")
    assert queue.status_code == 200
    assert queue.json()["items"][0]["id"] == operation_id


def test_possible_duplicate_requires_confirmation(
    client: TestClient, csrf_headers: dict[str, str], upload_pdf
) -> None:
    assert len(PDF_A) == len(PDF_B)
    first = upload_pdf(filename="revision.pdf", contents=PDF_A, key="revision-key-1")
    assert first.status_code == 201

    preflight = client.post(
        "/api/v1/uploads/preflight",
        headers=csrf_headers,
        json={
            "filename": "revision.pdf",
            "size_bytes": len(PDF_B),
            "collection_key": "customer",
        },
    )
    assert preflight.status_code == 200
    assert preflight.json()["requires_confirmation"] is True
    assert len(preflight.json()["possible_duplicates"]) == 1

    blocked = upload_pdf(filename="revision.pdf", contents=PDF_B, key="revision-key-2")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "possible-duplicate-confirmation-required"

    accepted = upload_pdf(
        filename="revision.pdf",
        contents=PDF_B,
        key="revision-key-2",
        confirm=True,
    )
    assert accepted.status_code == 201, accepted.text


def test_invalid_uploads_and_csrf_fail_closed(
    client: TestClient, csrf_headers: dict[str, str], upload_pdf
) -> None:
    no_csrf = client.post(
        "/api/v1/uploads/preflight",
        json={"filename": "document.pdf", "size_bytes": 10},
    )
    assert no_csrf.status_code == 403
    assert no_csrf.json()["code"] == "csrf-check-failed"

    cross_origin = client.post(
        "/api/v1/uploads/preflight",
        headers={**csrf_headers, "Origin": "https://attacker.example"},
        json={"filename": "document.pdf", "size_bytes": 10},
    )
    assert cross_origin.status_code == 403
    assert cross_origin.json()["code"] == "cross-origin-request"

    preflight_options = client.options(
        "/api/v1/uploads/preflight",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in preflight_options.headers

    invalid_signature = upload_pdf(contents=b"not a pdf", key="invalid-key-1")
    assert invalid_signature.status_code == 422
    assert invalid_signature.json()["code"] == "invalid-pdf"

    invalid_name = upload_pdf(filename="../escape.pdf", key="invalid-key-2")
    assert invalid_name.status_code == 422
    assert invalid_name.json()["code"] == "invalid-filename"

    invalid_mime = upload_pdf(content_type="text/plain", key="invalid-key-3")
    assert invalid_mime.status_code == 422
    assert invalid_mime.json()["code"] == "invalid-content-type"

    mismatched_key = client.post(
        "/api/v1/uploads",
        headers={**csrf_headers, "Idempotency-Key": "header-key-123"},
        files={"file": ("document.pdf", PDF_A, "application/pdf")},
        data={"idempotency_key": "form-key-456", "collection_key": "customer"},
    )
    assert mismatched_key.status_code == 422
    assert mismatched_key.json()["code"] == "idempotency-key-mismatch"


def test_scanner_and_size_failures_do_not_queue(
    app, client: TestClient, csrf_headers: dict[str, str], upload_pdf
) -> None:
    app.state.scanner = lambda _path: ScanResult(
        state=ScanState.INFECTED,
        engine="test-clamd",
        signature="Eicar-Signature",
        scanned_at=utc_now(),
    )
    infected = upload_pdf(key="infected-key-1")
    assert infected.status_code == 422
    assert infected.json()["code"] == "scan-not-clean"

    def unavailable(_path):
        raise ScannerUnavailableError("offline")

    app.state.scanner = unavailable
    unavailable_response = upload_pdf(key="unavailable-key-1")
    assert unavailable_response.status_code == 503
    assert unavailable_response.json()["code"] == "scanner-unavailable"

    app.state.scanner = lambda _path: ScanResult(
        state=ScanState.CLEAN, engine="test-clamd", scanned_at=utc_now()
    )
    app.state.settings.max_upload_bytes = 16
    oversized = upload_pdf(key="oversized-key-1")
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "upload-too-large"


def test_scanner_protocol_failure_is_fail_closed(app, upload_pdf) -> None:
    def malformed_response(_path):
        raise ScannerProtocolError("unexpected clamd response")

    app.state.scanner = malformed_response
    response = upload_pdf(key="protocol-failure-key")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "scanner-unavailable"


def test_encrypted_marker_is_not_parsed_by_bridge(app, upload_pdf) -> None:
    encrypted_looking = b"%PDF-1.7\n1 0 obj\n<< /Encrypt 2 0 R >>\nendobj\n%%EOF\n"
    response = upload_pdf(
        filename="encrypted-looking.pdf",
        contents=encrypted_looking,
        key="encrypted-marker-key",
    )
    assert response.status_code == 201
    assert response.json()["document"]["scan_state"] == "CLEAN"


def test_cancel_only_before_claim(
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    job_headers: dict[str, str],
) -> None:
    first = upload_pdf(key="cancel-key-1")
    operation_id = first.json()["operation_id"]
    document_id = first.json()["document"]["id"]
    cancelled = client.delete(f"/api/v1/queue/{operation_id}", headers=csrf_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["document"]["state"] == "CANCELLED"
    assert client.get(f"/api/v1/documents/{document_id}/content").status_code == 409

    second = upload_pdf(filename="claimed.pdf", contents=PDF_B, key="cancel-key-2")
    claimed_operation = second.json()["operation_id"]
    claim = client.post(
        "/api/v1/jobs/batches/claim",
        headers=job_headers,
        json={"request_id": "jenkins-cancel-race", "limit": 10},
    )
    assert claim.status_code == 200
    conflict = client.delete(f"/api/v1/queue/{claimed_operation}", headers=csrf_headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "operation-already-claimed"


def test_cancel_cleanup_failure_can_be_retried(
    app,
    client: TestClient,
    csrf_headers: dict[str, str],
    upload_pdf,
    monkeypatch,
) -> None:
    uploaded = upload_pdf(filename="cleanup.pdf", key="cleanup-cancel-key")
    document_id = uploaded.json()["document"]["id"]
    operation_id = uploaded.json()["operation_id"]

    from pdf_bridge.controllers import api

    original_remove = api.remove_storage_key

    def unavailable_storage(*_args, **_kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(api, "remove_storage_key", unavailable_storage)
    failed = client.delete(f"/api/v1/queue/{operation_id}", headers=csrf_headers)
    assert failed.status_code == 500
    assert failed.json()["code"] == "storage-cleanup-failed"

    with app.state.test_session_factory() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.state == DocumentState.CANCEL_CLEANUP
        assert document.storage_key is not None

    monkeypatch.setattr(api, "remove_storage_key", original_remove)
    retried = client.delete(f"/api/v1/queue/{operation_id}", headers=csrf_headers)
    assert retried.status_code == 200
    assert retried.json()["document"]["state"] == "CANCELLED"
    with app.state.test_session_factory() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.state == DocumentState.CANCELLED
        assert document.storage_key is None


def test_trusted_host_accepts_arbitrary_valid_ports(client: TestClient) -> None:
    matched = client.get("/library", headers={"Host": "testserver.local:49152"})
    unmatched = client.get(
        "/api/v1/does-not-exist",
        headers={"Host": "testserver.local:65535"},
    )

    assert matched.status_code == 200
    assert unmatched.status_code == 404


@pytest.mark.parametrize(
    ("path", "host"),
    (
        ("/library", "evil.example:443"),
        ("/api/v1/does-not-exist", "evil.example"),
        ("/library", "testserver.local:not-a-port"),
        ("/library", "testserver.local:"),
        ("/library", "testserver..example"),
        ("/api/v1/does-not-exist", ""),
    ),
)
def test_invalid_hosts_are_rejected_before_routing(
    client: TestClient,
    path: str,
    host: str,
) -> None:
    response = client.get(path, headers={"Host": host})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["status_code"] == 400
    assert response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"


def test_trusted_host_wildcard_accepts_only_subdomains(
    settings,
    session_factory: sessionmaker[Session],
) -> None:
    wildcard_settings = settings.model_copy(
        update={"allowed_hosts": ["*.example.test"]}
    )

    def db_provider():
        with session_factory() as session:
            yield session

    application = create_app(
        wildcard_settings,
        scanner=clean_scanner,
        db_provider=db_provider,
    )
    with TestClient(
        application,
        base_url="http://bridge.example.test",
        raise_server_exceptions=True,
    ) as test_client:
        accepted = test_client.get(
            "/api/v1/health/live",
            headers={"Host": "bridge.example.test:8443"},
        )
        rejected_apex = test_client.get(
            "/api/v1/health/live",
            headers={"Host": "example.test:8443"},
        )
        rejected_empty_label = test_client.get(
            "/api/v1/health/live",
            headers={"Host": ".example.test:8443"},
        )
        rejected_double_dot = test_client.get(
            "/api/v1/health/live",
            headers={"Host": "evil..example.test:8443"},
        )

    assert accepted.status_code == 200
    assert rejected_apex.status_code == 400
    assert rejected_empty_label.status_code == 400
    assert rejected_double_dot.status_code == 400


def test_concurrent_distinct_uploads_are_all_registered(
    app,
    client: TestClient,
) -> None:
    def upload(index: int) -> tuple[int, str]:
        # The client fixture keeps the single lifespan open. Additional
        # non-context clients preserve separate browser sessions while sharing
        # the app's transition lock and lifespan-owned state.
        worker = TestClient(
            app,
            base_url="http://testserver.local",
            raise_server_exceptions=True,
        )
        page = worker.get("/upload")
        token_match = re.search(
            r'<meta name="csrf-token" content="([^"]+)"', page.text
        )
        assert token_match
        key = f"concurrent-file-key-{index}"
        response = worker.post(
            "/api/v1/uploads",
            headers={
                "X-CSRF-Token": token_match.group(1),
                "Idempotency-Key": key,
            },
            files={
                "file": (
                    f"concurrent-{index}.pdf",
                    PDF_A + f"% distinct {index}\n".encode(),
                    "application/pdf",
                )
            },
            data={
                "idempotency_key": key,
                "possible_duplicate_confirmed": "false",
                "collection_key": "customer",
            },
        )
        return response.status_code, response.json().get("document", {}).get("id", "")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(upload, range(4)))
    assert [status for status, _document_id in results] == [201, 201, 201, 201]
    assert len({document_id for _status, document_id in results}) == 4


@pytest.mark.parametrize("matching_winner", [True, False])
def test_idempotency_commit_race_uses_the_winning_catalog_row(
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    matching_winner: bool,
) -> None:
    racing_session = session_factory()
    winner_id = uuid.uuid4()
    winner_operation_id = uuid.uuid4()

    def database_override():
        yield racing_session

    def race_commit() -> None:
        racing_session.rollback()
        with session_factory() as winner_session:
            filename = "race.pdf" if matching_winner else "different.pdf"
            collection_key = "customer" if matching_winner else "internal"
            winner = Document(
                id=winner_id,
                original_filename=filename,
                normalized_filename=filename,
                storage_key=f"objects/{winner_id}.pdf",
                size_bytes=len(PDF_A),
                sha256=(
                    hashlib.sha256(PDF_A).hexdigest()
                    if matching_winner
                    else hashlib.sha256(PDF_B).hexdigest()
                ),
                idempotency_key="commit-race-key",
                state=DocumentState.QUEUED,
                collection_key=collection_key,
                scan_state=ScanState.CLEAN,
                scan_engine="test-clamd",
                scanned_at=utc_now(),
                uploader_identity="race-winner",
            )
            operation = QueueOperation(
                id=winner_operation_id,
                document=winner,
                operation_type=OperationType.INGEST,
                state=OperationState.QUEUED,
                attempt=1,
            )
            winner_session.add_all([winner, operation])
            winner_session.commit()
        raise IntegrityError("simulated idempotency race", {}, Exception("unique"))

    application = create_app(
        settings,
        scanner=clean_scanner,
        db_provider=database_override,
    )
    monkeypatch.setattr(racing_session, "commit", race_commit)
    try:
        with TestClient(
            application,
            base_url="http://testserver.local",
            raise_server_exceptions=True,
        ) as race_client:
            page = race_client.get("/upload")
            token_match = re.search(
                r'<meta name="csrf-token" content="([^"]+)"', page.text
            )
            assert token_match
            response = race_client.post(
                "/api/v1/uploads",
                headers={
                    "X-CSRF-Token": token_match.group(1),
                    "Idempotency-Key": "commit-race-key",
                },
                files={"file": ("race.pdf", PDF_A, "application/pdf")},
                data={
                    "idempotency_key": "commit-race-key",
                    "possible_duplicate_confirmed": "false",
                    "collection_key": "customer",
                },
            )
    finally:
        racing_session.close()

    if matching_winner:
        assert response.status_code == 201, response.text
        assert response.json()["idempotent_replay"] is True
        assert response.json()["document"]["id"] == str(winner_id)
        assert response.json()["operation_id"] == str(winner_operation_id)
    else:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "idempotency-key-conflict"
    # The losing request promoted canonical bytes before its commit lost the
    # race; resolving the conflict must also remove that orphaned object.
    assert list((settings.storage_root / "objects").rglob("*.pdf")) == []
    assert list((settings.storage_root / "quarantine").iterdir()) == []


def _assert_upload_state_is_clean(
    settings, session_factory: sessionmaker[Session]
) -> None:
    """Assert no catalog rows or canonical/staged bytes survived a failed upload."""

    assert list((settings.storage_root / "objects").rglob("*.pdf")) == []
    assert list((settings.storage_root / "quarantine").iterdir()) == []
    assert list((settings.storage_root / "temporary").iterdir()) == []
    with session_factory() as session:
        assert session.scalars(select(Document)).all() == []
        assert session.scalars(select(AuditEvent)).all() == []


def _upload_through(client: TestClient, *, key: str) -> object:
    page = client.get("/upload")
    token_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert token_match
    return client.post(
        "/api/v1/uploads",
        headers={"X-CSRF-Token": token_match.group(1), "Idempotency-Key": key},
        files={"file": ("compensated.pdf", PDF_A, "application/pdf")},
        data={
            "idempotency_key": key,
            "possible_duplicate_confirmed": "false",
            "collection_key": "customer",
        },
    )


def test_commit_failure_rolls_back_and_removes_promoted_bytes(
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_session = session_factory()

    def database_override():
        yield request_session

    def failing_commit() -> None:
        raise RuntimeError("simulated commit failure")

    application = create_app(settings, scanner=clean_scanner, db_provider=database_override)
    monkeypatch.setattr(request_session, "commit", failing_commit)
    try:
        with TestClient(
            application,
            base_url="http://testserver.local",
            raise_server_exceptions=False,
        ) as client:
            response = _upload_through(client, key="commit-failure-key")
    finally:
        request_session.close()

    assert response.status_code == 500
    _assert_upload_state_is_clean(settings, session_factory)


def test_audit_flush_failure_rolls_back_and_removes_promoted_bytes(
    settings,
    session_factory: sessionmaker[Session],
) -> None:
    request_session = session_factory()

    @event.listens_for(request_session, "before_flush")
    def reject_audit_rows(session, _flush_context, _instances) -> None:
        if any(isinstance(item, AuditEvent) for item in session.new):
            raise RuntimeError("simulated audit insert failure")

    def database_override():
        yield request_session

    application = create_app(settings, scanner=clean_scanner, db_provider=database_override)
    try:
        with TestClient(
            application,
            base_url="http://testserver.local",
            raise_server_exceptions=False,
        ) as client:
            response = _upload_through(client, key="audit-flush-failure-key")
    finally:
        request_session.close()

    assert response.status_code == 500
    _assert_upload_state_is_clean(settings, session_factory)


def test_upload_compensation_failure_is_surfaced_not_swallowed(
    settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_unlink = Path.unlink

    def selective_unlink(self: Path, missing_ok: bool = False) -> None:
        if "objects" in self.parts:
            raise OSError("canonical cleanup blocked")
        original_unlink(self, missing_ok=missing_ok)

    def failing_commit() -> None:
        raise RuntimeError("simulated commit failure")

    with session_factory() as session:
        monkeypatch.setattr(session, "commit", failing_commit)
        monkeypatch.setattr(Path, "unlink", selective_unlink)
        with pytest.raises(OSError, match="canonical cleanup blocked") as failure:
            document_manager.upload_document(
                session,
                settings=settings,
                scanner=clean_scanner,
                transition_lock=threading.RLock(),
                file=io.BytesIO(PDF_A),
                filename="cleanup-failure.pdf",
                content_type="application/pdf",
                collection_key="customer",
                possible_duplicate_confirmed=False,
                header_idempotency_key="cleanup-failure-key",
                form_idempotency_key=None,
                actor_type="anonymous",
                actor_id="compensation-test",
            )

    # The transaction failure stays visible as the chained context, and the
    # orphaned object remains on disk because its removal was refused.
    assert isinstance(failure.value.__context__, RuntimeError)
    monkeypatch.setattr(Path, "unlink", original_unlink)
    orphaned = list((settings.storage_root / "objects").rglob("*.pdf"))
    assert len(orphaned) == 1
    with session_factory() as session:
        assert session.scalars(select(Document)).all() == []


def test_uploads_stage_in_quarantine_before_private_promotion(
    app,
    client: TestClient,
    upload_pdf,
    settings,
) -> None:
    observed: dict[str, object] = {}

    def capturing_scanner(path: Path) -> ScanResult:
        observed["parent"] = path.parent
        observed["mode"] = path.stat().st_mode & 0o777
        return ScanResult(state=ScanState.CLEAN, engine="test-clamd", scanned_at=utc_now())

    app.state.scanner = capturing_scanner
    response = upload_pdf(key="quarantine-staging-key")

    assert response.status_code == 201, response.text
    assert observed["parent"] == settings.storage_root / "quarantine"
    if os.name == "posix":
        assert observed["mode"] == 0o600
    assert list((settings.storage_root / "quarantine").iterdir()) == []
    stored = list((settings.storage_root / "objects").rglob("*.pdf"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == PDF_A


@pytest.mark.skipif(os.name != "posix", reason="POSIX private file modes")
def test_staged_and_promoted_files_keep_private_posix_modes(tmp_path) -> None:
    layout = StorageLayout.from_root(tmp_path / "mode-storage")
    staged = stream_upload(io.BytesIO(PDF_A), layout, max_bytes=1024 * 1024)
    assert staged.path.stat().st_mode & 0o777 == 0o600
    promoted = promote_staged_file(staged, layout, uuid.uuid4())
    assert promoted.path.stat().st_mode & 0o777 == 0o600
    for directory in (layout.objects, layout.temporary, layout.quarantine):
        assert directory.stat().st_mode & 0o777 == 0o700


def test_staging_permission_failures_propagate(tmp_path, monkeypatch) -> None:
    layout = StorageLayout.from_root(tmp_path / "denied-storage")

    def denied_open(*_args, **_kwargs):
        raise PermissionError("private staging refused")

    monkeypatch.setattr(os, "open", denied_open)
    with pytest.raises(PermissionError, match="private staging refused"):
        stream_upload(io.BytesIO(PDF_A), layout, max_bytes=1024 * 1024)


def test_liveness_responds_while_an_upload_blocks_a_worker_thread(
    app,
    client: TestClient,
) -> None:
    scan_entered = threading.Event()
    release_scan = threading.Event()

    def blocking_scanner(_path: Path) -> ScanResult:
        scan_entered.set()
        assert release_scan.wait(timeout=15), "test released the scanner"
        return ScanResult(state=ScanState.CLEAN, engine="test-clamd", scanned_at=utc_now())

    app.state.scanner = blocking_scanner

    with ThreadPoolExecutor(max_workers=1) as executor:
        # Both the upload and the liveness probe go through the same client,
        # so both coroutines share the fixture's single event loop.
        pending_upload = executor.submit(_upload_through, client, key="blocking-upload-key")
        try:
            assert scan_entered.wait(timeout=15), "upload never reached the scanner"
            # The upload handler is parked in a worker thread; the event loop
            # must still answer liveness immediately.
            live = client.get("/api/v1/health/live")
            assert live.status_code == 200
            assert not pending_upload.done()
        finally:
            release_scan.set()
        response = pending_upload.result(timeout=30)
    assert response.status_code == 201, response.text


def test_interrupted_upload_removes_partial_quarantine_file(tmp_path) -> None:
    class InterruptedReader:
        def __init__(self) -> None:
            self.reads = 0

        def read(self, _size: int = -1) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return PDF_A[:20]
            raise ConnectionError("client disconnected")

    layout = StorageLayout.from_root(tmp_path / "interrupted-storage")
    with pytest.raises(ConnectionError, match="disconnected"):
        stream_upload(InterruptedReader(), layout, max_bytes=1024)
    assert list(layout.quarantine.iterdir()) == []
    assert list(layout.temporary.iterdir()) == []
