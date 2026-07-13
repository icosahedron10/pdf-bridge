"""Compressed private analysis storage and the audit manifest hash.

Full analysis data — extracted text, chunk records, dense and sparse
vectors, candidate snapshots, prompts, and raw model output — persists with
the document for as long as it exists, then is purged on cancellation or
deletion. Before purging, a canonical manifest of content, vector, and
result fingerprints is hashed; the audit ledger keeps only that hash and
content-free metadata, never excerpts, vectors, prompts, or raw output.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from typing import Any

from pdf_bridge.services.storage import StorageLayout, resolve_storage_key

ARTIFACT_KINDS = (
    "extracted_text",
    "chunks",
    "vectors",
    "candidates",
    "findings",
)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Stored artifact location and its integrity fingerprint."""

    document_id: uuid.UUID
    analysis_id: uuid.UUID
    kind: str
    storage_key: str
    sha256: str
    size_bytes: int


def _artifact_key(document_id: uuid.UUID, analysis_id: uuid.UUID, kind: str) -> str:
    return f"analysis/{document_id}/{analysis_id}/{kind}.json.gz"


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_artifact(
    layout: StorageLayout,
    document_id: uuid.UUID,
    analysis_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
) -> ArtifactRecord:
    """Write one immutable revision artifact and fingerprint its compressed bytes."""

    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unknown analysis artifact kind {kind!r}")
    if payload.get("analysis_id") != str(analysis_id):
        raise ValueError("artifact payload does not match its analysis id")
    key = _artifact_key(document_id, analysis_id, kind)
    path = resolve_storage_key(layout, key)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    buffer = io.BytesIO()
    # mtime=0 keeps identical payloads byte-identical, so artifact hashes are
    # reproducible fingerprints rather than timestamps in disguise.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressor:
        compressor.write(_canonical_json(payload))
    data = buffer.getvalue()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Publishing via a hard link is atomic and never replaces an
            # existing immutable artifact. The temporary inode is removed
            # after the destination link is durable.
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError(
                    f"artifact {key!r} already exists with different content"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
    return ArtifactRecord(
        document_id=document_id,
        analysis_id=analysis_id,
        kind=kind,
        storage_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def read_artifact(
    layout: StorageLayout,
    storage_key: str,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
) -> dict[str, Any]:
    """Read one artifact after verifying its cataloged size and fingerprint."""

    path = resolve_storage_key(layout, storage_key)
    data = path.read_bytes()
    if len(data) != expected_size_bytes:
        raise ValueError(f"artifact {storage_key!r} size does not match its catalog row")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError(f"artifact {storage_key!r} hash does not match its catalog row")
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"artifact {storage_key!r} did not contain a JSON object")
    return payload


def purge_document_artifacts(layout: StorageLayout, document_id: uuid.UUID) -> None:
    """Remove every retained analysis artifact for a document."""

    directory = resolve_storage_key(layout, f"analysis/{document_id}")
    if not directory.exists():
        return
    if not directory.is_dir():
        raise ValueError(f"artifact root for document {document_id} is not a directory")
    shutil.rmtree(directory)
    if directory.exists():
        raise OSError(f"failed to purge artifact root for document {document_id}")


def _canonical_analysis_history(
    analysis_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for analysis in analysis_history:
        artifacts = analysis.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("analysis history artifacts must be a list")
        item = dict(analysis)
        item["artifacts"] = sorted(
            (dict(artifact) for artifact in artifacts),
            key=lambda artifact: (str(artifact["kind"]), str(artifact["sha256"])),
        )
        normalized.append(item)
    return sorted(
        normalized,
        key=lambda analysis: (int(analysis["revision"]), str(analysis["analysis_id"])),
    )


def analysis_manifest_hash(
    *,
    document_id: uuid.UUID,
    content_sha256: str,
    text_hash: str | None,
    analysis_history: list[dict[str, Any]],
    decision_action: str | None,
    decision_actor: str | None,
    decision_target: str | None,
    uploaded_at: str,
    decided_at: str | None,
) -> str:
    """Hash the canonical analysis manifest kept in the audit ledger.

    The manifest binds content, vector, and result fingerprints to the
    decision, actor, target, and timestamps without retaining any content.
    """

    manifest = {
        "version": 2,
        "document_id": str(document_id),
        "content_sha256": content_sha256,
        "text_hash": text_hash,
        "analyses": _canonical_analysis_history(analysis_history),
        "decision": {
            "action": decision_action,
            "actor": decision_actor,
            "target_document_id": decision_target,
            "decided_at": decided_at,
        },
        "uploaded_at": uploaded_at,
    }
    return hashlib.sha256(_canonical_json(manifest)).hexdigest()
