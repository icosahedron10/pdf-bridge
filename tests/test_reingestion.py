from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import httpx
import pytest

from pdf_bridge.persistence.models import DocumentState
from pdf_bridge.services.reingestion import (
    ReingestionClient,
    ReingestionError,
    apply_manifest,
    validate_manifest,
)


def _write_manifest(root: Path, count: int = 1) -> Path:
    documents = []
    for index in range(count):
        name = f"source-{index}.pdf"
        content = f"%PDF-1.4\nsource {index}\n%%EOF\n".encode()
        (root / name).write_bytes(content)
        documents.append(
            {
                "path": name,
                "filename": f"Source {index}.pdf",
                "collection_key": "customer",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    path = root / "manifest.json"
    path.write_text(json.dumps({"version": 4, "documents": documents}), encoding="utf-8")
    return path


def test_manifest_validation_hashes_every_source_and_rejects_storage_overlap(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    manifest_path = _write_manifest(source, 2)
    validated = validate_manifest(
        manifest_path,
        source_root=source,
        bridge_storage_root=tmp_path / "bridge-storage",
    )

    assert len(validated.documents) == 2
    assert validated.total_bytes == sum(item.size_bytes for item in validated.documents)
    assert len(validated.manifest_sha256) == 64
    with pytest.raises(ReingestionError, match="must not overlap"):
        validate_manifest(
            manifest_path,
            source_root=source,
            bridge_storage_root=source / "bridge-storage",
        )


def test_manifest_rejects_declared_hash_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    manifest_path = _write_manifest(source)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["documents"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReingestionError, match="declared PDF content"):
        validate_manifest(
            manifest_path,
            source_root=source,
            bridge_storage_root=tmp_path / "bridge-storage",
        )


def _state_counts() -> dict[str, int]:
    return {state.value: 0 for state in DocumentState}


def test_apply_uses_v2_csrf_idempotency_and_caps_outstanding_at_five(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    manifest = validate_manifest(
        _write_manifest(source, 6),
        source_root=source,
        bridge_storage_root=tmp_path / "bridge-storage",
    )
    uploads: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/health/ready":
            return httpx.Response(
                200,
                json={"status": "OK", "checks": []},
            )
        if request.url.path == "/api/v2/collections":
            return httpx.Response(
                200,
                headers={"X-CSRF-Token": "csrf-token"},
                json={
                    "items": [
                        {
                            "key": "customer",
                            "display_name": "Customer",
                            "description": "Customer documents.",
                            "audience": "customer",
                            "enabled": True,
                            "counts": {"total": 0, "by_state": _state_counts()},
                        }
                    ],
                    "limit": 100,
                    "next_cursor": None,
                    "has_more": False,
                },
            )
        if request.method == "POST" and request.url.path.endswith("/documents"):
            uploads.append(request)
            assert request.headers["x-csrf-token"] == "csrf-token"
            assert request.headers["idempotency-key"].startswith("reingest-")
            document_id = uuid.uuid4()
            operation_id = uuid.uuid4()
            now = "2026-07-13T12:00:00Z"
            return httpx.Response(
                202,
                json={
                    "document": {
                        "id": str(document_id),
                        "collection_key": "customer",
                        "original_filename": "Source.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": 20,
                        "sha256": "a" * 64,
                        "created_by": "migration",
                        "state": "PREFLIGHTING",
                        "created_at": now,
                        "updated_at": now,
                        "ready_at": None,
                        "failure": None,
                        "allowed_actions": [],
                    },
                    "operation": {
                        "id": str(operation_id),
                        "operation_type": "PREFLIGHT",
                        "state": "QUEUED",
                        "phase": "QUEUED",
                        "priority": "NORMAL",
                        "attempt": 1,
                        "retryable": True,
                        "created_at": now,
                        "updated_at": now,
                        "completed_at": None,
                    },
                    "idempotent_replay": False,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with ReingestionClient(
        "https://bridge.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        state = apply_manifest(
            manifest,
            state_path=tmp_path / "state.json",
            client=client,
            wait_seconds=0,
        )

    assert len(uploads) == 5
    assert len(state.accepted) == 5
    assert all(item.state == "PREFLIGHTING" for item in state.accepted.values())
