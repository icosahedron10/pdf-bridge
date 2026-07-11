from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from pdf_bridge.models import Document, DocumentState, ScanState, utc_now
from pdf_bridge.scanner import ScannerProtocolError, ScannerUnavailableError, ScanResult
from pdf_bridge.storage import StorageLayout, stream_upload
from tests.conftest import PDF_A, PDF_B


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
        json={"filename": "revision.pdf", "size_bytes": len(PDF_B)},
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
        data={"idempotency_key": "form-key-456"},
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

    from pdf_bridge import api

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


def test_untrusted_host_is_rejected(client: TestClient) -> None:
    response = client.get("/library", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_concurrent_distinct_uploads_are_all_registered(app) -> None:
    # Build FastAPI/Pydantic route adapters once before worker threads exercise
    # runtime concurrency; schema generation itself is not the subject here.
    app.openapi()

    def upload(index: int) -> tuple[int, str]:
        with TestClient(app) as worker:
            page = worker.get("/upload")
            token_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
            assert token_match
            key = f"concurrent-file-key-{index}"
            response = worker.post(
                "/api/v1/uploads",
                headers={"X-CSRF-Token": token_match.group(1), "Idempotency-Key": key},
                files={
                    "file": (
                        f"concurrent-{index}.pdf",
                        PDF_A + f"% distinct {index}\n".encode(),
                        "application/pdf",
                    )
                },
                data={"idempotency_key": key, "possible_duplicate_confirmed": "false"},
            )
            return response.status_code, response.json().get("document", {}).get("id", "")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(upload, range(4)))
    assert [status for status, _document_id in results] == [201, 201, 201, 201]
    assert len({document_id for _status, document_id in results}) == 4


def test_interrupted_upload_removes_partial_temporary_file(tmp_path) -> None:
    class InterruptedReader:
        def __init__(self) -> None:
            self.reads = 0

        async def read(self, _size: int = -1) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return PDF_A[:20]
            raise ConnectionError("client disconnected")

    layout = StorageLayout.from_root(tmp_path / "interrupted-storage")
    with pytest.raises(ConnectionError, match="disconnected"):
        asyncio.run(stream_upload(InterruptedReader(), layout, max_bytes=1024))
    assert list(layout.temporary.iterdir()) == []
