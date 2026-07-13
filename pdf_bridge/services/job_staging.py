"""Safe local staging and result-file validation for the Jenkins job client."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from pdf_bridge.contracts.job_contracts import (
    BridgeClientError,
    CliModel,
    PullResult,
    ReportFile,
)
from pdf_bridge.contracts.schemas import (
    BatchClaimResponse,
    BatchManifestItem,
    BatchManifestResponse,
    BatchResultsRequest,
    BatchResultsResponse,
)
from pdf_bridge.persistence.models import OperationType
from pdf_bridge.services.job_http import _ensure_success

MANIFEST_FILENAME = "manifest.json"
COPY_CHUNK_BYTES = 1024 * 1024
MAX_PULL_RESULT_BYTES = 64 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_STAGED_MANIFEST_BYTES = 2 * 1024 * 1024


def _safe_collection_key(collection_key: str) -> str:
    if (
        not collection_key
        or len(collection_key) > 63
        or not collection_key[0].isalnum()
        or not collection_key[0].isascii()
        or collection_key != collection_key.casefold()
        or any(
            not (character.isascii() and (character.isalnum() or character in {"-", "_"}))
            for character in collection_key
        )
    ):
        raise BridgeClientError(
            "server returned an unsafe collection key; expected lowercase ASCII letters, "
            "digits, hyphens, or underscores"
        )
    return collection_key


def _expected_relative_path(*, document_id: uuid.UUID, collection_key: str) -> str:
    safe_collection_key = _safe_collection_key(collection_key)
    return f"pdfs/{safe_collection_key}/{document_id}.pdf"


def _validate_relative_path(
    relative_path: str,
    *,
    document_id: uuid.UUID,
    collection_key: str,
) -> str:
    expected = _expected_relative_path(
        document_id=document_id,
        collection_key=collection_key,
    )
    if relative_path != expected:
        raise BridgeClientError(
            "server returned an unsafe or inconsistent relative_path; expected "
            f"{expected!r}"
        )
    path = PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or str(path) != relative_path
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in relative_path
        or any(ord(character) < 32 for character in relative_path)
    ):
        raise BridgeClientError("server returned an unsafe relative_path")
    return relative_path


class StagedManifestItem(CliModel):
    """Local manifest entry coupled to a safe canonical relative path."""

    operation_id: uuid.UUID
    document_id: uuid.UUID
    operation_type: OperationType
    filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_key: str
    relative_path: str

    @model_validator(mode="after")
    def validate_handoff_metadata(self) -> StagedManifestItem:
        """Correlate collection metadata with the operation and staging path."""

        _validate_relative_path(
            self.relative_path,
            document_id=self.document_id,
            collection_key=self.collection_key,
        )
        return self


class StagedManifest(CliModel):
    """Versioned local manifest written beside a durably staged batch."""

    version: Literal[2] = 2
    batch_id: uuid.UUID
    request_id: str
    claimed_at: str
    lease_expires_at: str
    operations: list[StagedManifestItem]


def _manifest_item(item: BatchManifestItem) -> StagedManifestItem:
    relative_path = _validate_relative_path(
        item.relative_path,
        document_id=item.document_id,
        collection_key=item.collection_key,
    )
    return StagedManifestItem(
        operation_id=item.operation_id,
        document_id=item.document_id,
        operation_type=item.operation_type,
        filename=item.filename,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        collection_key=item.collection_key,
        relative_path=relative_path,
    )


def _local_manifest(remote: BatchManifestResponse) -> StagedManifest:
    if remote.version != 2:
        raise BridgeClientError(f"unsupported server manifest version: {remote.version}")
    return StagedManifest(
        batch_id=remote.batch_id,
        request_id=remote.request_id,
        claimed_at=remote.claimed_at.isoformat(),
        lease_expires_at=remote.lease_expires_at.isoformat(),
        operations=[_manifest_item(item) for item in remote.operations],
    )


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(COPY_CHUNK_BYTES), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _download_operation(
    client: Any,
    remote: BatchManifestItem,
    destination: Path,
) -> None:
    if remote.operation_type != OperationType.INGEST:
        raise BridgeClientError("internal error: attempted to download a DELETE operation")
    if not remote.download_url:
        raise BridgeClientError(f"INGEST operation {remote.operation_id} has no download_url")

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with client.stream_operation(remote.download_url) as response:
        _ensure_success(response)
        with destination.open("xb") as output:
            for block in response.iter_bytes(COPY_CHUNK_BYTES):
                size += len(block)
                if size > remote.size_bytes:
                    raise BridgeClientError(
                        f"operation {remote.operation_id} exceeded declared size "
                        f"{remote.size_bytes}"
                    )
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())

    if size != remote.size_bytes:
        raise BridgeClientError(
            f"operation {remote.operation_id} size mismatch: "
            f"expected {remote.size_bytes}, got {size}"
        )
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != remote.sha256:
        raise BridgeClientError(
            f"operation {remote.operation_id} checksum mismatch: "
            f"expected {remote.sha256}, got {actual_sha256}"
        )


def _manifest_bytes(manifest: StagedManifest) -> bytes:
    body = manifest.model_dump_json(indent=2).encode("utf-8")
    return body + b"\n"


def _write_new_file(path: Path, body: bytes) -> None:
    with path.open("xb") as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())


def _staged_operation_path(root: Path, operation: StagedManifestItem) -> Path:
    relative_path = _validate_relative_path(
        operation.relative_path,
        document_id=operation.document_id,
        collection_key=operation.collection_key,
    )
    root_resolved = root.resolve(strict=True)
    candidate = root
    for part in PurePosixPath(relative_path).parts:
        candidate /= part
        if candidate.is_symlink():
            raise BridgeClientError("staged relative_path contains a symlink")
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except ValueError as exc:
        raise BridgeClientError(
            "staged relative_path resolves outside the batch directory"
        ) from exc
    return candidate


def _validate_existing_batch(final_directory: Path, expected: StagedManifest) -> str:
    if final_directory.is_symlink() or not final_directory.is_dir():
        raise BridgeClientError(
            f"existing batch path is not a real directory: {final_directory}"
        )
    manifest_path = final_directory / MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BridgeClientError(f"existing batch has no regular {MANIFEST_FILENAME}")
    if manifest_path.stat().st_size > MAX_STAGED_MANIFEST_BYTES:
        raise BridgeClientError("existing batch manifest exceeds the client safety limit")
    try:
        actual = StagedManifest.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise BridgeClientError(f"existing batch manifest is invalid: {exc}") from exc
    if actual != expected:
        raise BridgeClientError("existing batch manifest does not match the server batch")

    for operation in actual.operations:
        if operation.operation_type == OperationType.DELETE:
            continue
        path = _staged_operation_path(final_directory, operation)
        if path.is_symlink() or not path.is_file():
            raise BridgeClientError(
                f"staged file is missing or is a symlink: {operation.relative_path}"
            )
        actual_sha256, actual_size = _sha256_file(path)
        if actual_size != operation.size_bytes or actual_sha256 != operation.sha256:
            raise BridgeClientError(
                f"staged file failed verification: {operation.relative_path}"
            )
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def _stage_new_batch(
    client: Any,
    destination_root: Path,
    remote: BatchManifestResponse,
    manifest: StagedManifest,
) -> tuple[Path, str]:
    final_directory = destination_root / str(remote.batch_id)
    if final_directory.exists() or final_directory.is_symlink():
        checksum = _validate_existing_batch(final_directory, manifest)
        return final_directory, checksum

    temporary_directory = Path(
        tempfile.mkdtemp(prefix=f".{remote.batch_id}.tmp-", dir=destination_root)
    )
    try:
        remote_by_id = {item.operation_id: item for item in remote.operations}
        if len(remote_by_id) != len(remote.operations):
            raise BridgeClientError("server manifest contains duplicate operation IDs")
        for item in manifest.operations:
            if item.operation_type == OperationType.DELETE:
                continue
            _download_operation(
                client,
                remote_by_id[item.operation_id],
                _staged_operation_path(temporary_directory, item),
            )

        manifest_body = _manifest_bytes(manifest)
        _write_new_file(temporary_directory / MANIFEST_FILENAME, manifest_body)
        manifest_sha256 = hashlib.sha256(manifest_body).hexdigest()
        os.replace(temporary_directory, final_directory)
        return final_directory, manifest_sha256
    except Exception as original_error:
        try:
            shutil.rmtree(temporary_directory)
        except OSError as cleanup_error:
            raise BridgeClientError(
                f"batch staging failed and temporary cleanup also failed at "
                f"{temporary_directory}: {cleanup_error}"
            ) from original_error
        raise


def _write_json_result(path: Path, model: BaseModel) -> None:
    resolved = path.expanduser().resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.tmp-{uuid.uuid4().hex}")
    body = model.model_dump_json(indent=2).encode("utf-8") + b"\n"
    try:
        _write_new_file(temporary, body)
        os.replace(temporary, resolved)
    finally:
        temporary.unlink(missing_ok=True)


def _read_report(path: Path) -> ReportFile:
    if path.is_symlink() or not path.is_file():
        raise BridgeClientError("report must be a regular, non-symlink file")
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise BridgeClientError(
            f"report exceeds the {MAX_REPORT_BYTES}-byte client safety limit"
        )
    return ReportFile.model_validate_json(path.read_bytes())


def _read_pull_result(path: Path) -> PullResult:
    if path.is_symlink() or not path.is_file():
        raise BridgeClientError("pull result must be a regular, non-symlink file")
    if path.stat().st_size > MAX_PULL_RESULT_BYTES:
        raise BridgeClientError(
            f"pull result exceeds the {MAX_PULL_RESULT_BYTES}-byte client safety limit"
        )
    return PullResult.model_validate_json(path.read_bytes())


def _validate_destination_root(destination: Path) -> Path:
    resolved = destination.expanduser().resolve(strict=False)
    if any(part.casefold().startswith("onedrive") for part in resolved.parts):
        raise BridgeClientError(
            "--destination must not be inside a OneDrive-synchronized path"
        )
    workspace_value = os.environ.get("WORKSPACE")
    if workspace_value:
        workspace = Path(workspace_value).expanduser().resolve(strict=False)
        try:
            resolved.relative_to(workspace)
        except ValueError:
            pass
        else:
            raise BridgeClientError("--destination must be outside the Jenkins workspace")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    if resolved.is_symlink() or not resolved.is_dir():
        raise BridgeClientError("--destination must resolve to a real directory")
    return resolved


def validate_claim_manifest(
    claim: BatchClaimResponse,
    remote: BatchManifestResponse,
    *,
    request_id: str,
) -> None:
    """Correlate a remote manifest with its claim and local request ID."""

    if remote.request_id != request_id:
        raise BridgeClientError("claimed batch request_id does not match the request")
    if remote.batch_id != claim.batch_id:
        raise BridgeClientError("claimed batch ID does not match its manifest")
    if claim.operation_count != len(remote.operations):
        raise BridgeClientError("claimed operation count does not match the manifest")


def prepare_report_submission(
    report_path: Path,
    pull_result_path: Path,
) -> tuple[ReportFile, BatchResultsRequest]:
    """Load and correlate local report artifacts before network submission."""

    parsed = _read_report(report_path)
    pull_result = _read_pull_result(pull_result_path)
    if pull_result.batch_id is None or pull_result.operation_count == 0:
        raise BridgeClientError(
            "pull result contains no batch, so pipeline results cannot be submitted"
        )
    if parsed.batch_id != pull_result.batch_id:
        raise BridgeClientError(
            "pipeline report batch_id does not match the current pull result batch_id"
        )
    request = BatchResultsRequest(
        pipeline_run_id=parsed.pipeline_run_id,
        results=parsed.results,
    )
    return parsed, request


def validate_report_response(
    response: BatchResultsResponse,
    *,
    expected_batch_id: uuid.UUID,
) -> None:
    """Require the server response to identify the submitted batch."""

    if response.batch_id != expected_batch_id:
        raise BridgeClientError("PDF Bridge result response returned the wrong batch_id")
