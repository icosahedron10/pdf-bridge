"""Strict, resumable API-v2 source-PDF reingestion client."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from pdf_bridge.contracts.schemas import (
    CollectionListResponse,
    DocumentDetail,
    HealthResponse,
    UploadAcceptedResponse,
)
from pdf_bridge.services.storage import has_pdf_signature, hash_file, validate_pdf_filename

_MAX_MANIFEST_BYTES = 5 * 1024 * 1024
_MAX_DOCUMENTS = 10_000
_TERMINAL_STATES = {"READY", "REJECTED", "CANCELLED", "DELETED"}
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ReingestionError(RuntimeError):
    """A safe validation or API failure for the migration operator."""


class ManifestDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1_024)
    filename: str = Field(min_length=1, max_length=255)
    collection_key: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$",
    )
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def relative_posix_path(cls, value: str) -> str:
        if (
            "\\" in value
            or any(ord(character) < 32 for character in value)
            or PurePosixPath(value).is_absolute()
        ):
            raise ValueError("manifest paths must be control-free relative POSIX paths")
        parts = PurePosixPath(value).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("manifest paths cannot contain empty, dot, or parent segments")
        return value

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        try:
            return validate_pdf_filename(value)
        except Exception as exc:
            raise ValueError("filename must be a path-free PDF filename") from exc


class ReingestionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[4]
    documents: list[ManifestDocument] = Field(min_length=1, max_length=_MAX_DOCUMENTS)


@dataclass(frozen=True, slots=True)
class ValidatedDocument:
    index: int
    path: Path
    filename: str
    collection_key: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ValidatedManifest:
    manifest_sha256: str
    documents: tuple[ValidatedDocument, ...]
    total_bytes: int


class AcceptedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: uuid.UUID
    operation_id: uuid.UUID
    state: str = Field(min_length=1, max_length=64)


class ReingestionState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    manifest_sha256: Sha256
    accepted: dict[str, AcceptedDocument] = Field(default_factory=dict)


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_manifest(
    manifest_path: Path,
    *,
    source_root: Path,
    bridge_storage_root: Path,
) -> ValidatedManifest:
    """Validate the complete manifest and source set without writing anything."""

    try:
        manifest_file = manifest_path.expanduser().resolve(strict=True)
        source = source_root.expanduser().resolve(strict=True)
        bridge_storage = bridge_storage_root.expanduser().resolve(strict=False)
    except OSError as exc:
        raise ReingestionError("The manifest or source root is unavailable.") from exc
    if not manifest_file.is_file() or not source.is_dir():
        raise ReingestionError("The manifest must be a file and source root must be a directory.")
    if _is_beneath(source, bridge_storage) or _is_beneath(bridge_storage, source):
        raise ReingestionError("The source root and Bridge storage root must not overlap.")
    try:
        if manifest_file.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ReingestionError("The reingestion manifest exceeds the safety limit.")
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest = ReingestionManifest.model_validate(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ReingestionError("The reingestion manifest is invalid.") from exc

    canonical = json.dumps(
        manifest.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    manifest_sha256 = hashlib.sha256(canonical).hexdigest()
    seen_paths: set[str] = set()
    seen_content: set[tuple[str, str]] = set()
    validated: list[ValidatedDocument] = []
    total_bytes = 0
    for index, item in enumerate(manifest.documents):
        try:
            candidate = source.joinpath(*PurePosixPath(item.path).parts).resolve(strict=True)
        except OSError as exc:
            raise ReingestionError(f"Manifest entry {index} source file is unavailable.") from exc
        if not candidate.is_file() or not _is_beneath(candidate, source):
            raise ReingestionError(f"Manifest entry {index} escapes the source root.")
        path_identity = os.path.normcase(str(candidate))
        content_identity = (item.collection_key, item.sha256)
        if path_identity in seen_paths or content_identity in seen_content:
            raise ReingestionError("The manifest contains a duplicate path or collection hash.")
        seen_paths.add(path_identity)
        seen_content.add(content_identity)
        try:
            with candidate.open("rb") as source_file:
                signature = source_file.read(5)
            size_bytes, digest = hash_file(candidate)
        except OSError as exc:
            raise ReingestionError(f"Manifest entry {index} could not be read.") from exc
        if not has_pdf_signature(signature) or digest != item.sha256:
            raise ReingestionError(
                f"Manifest entry {index} is not the declared PDF content."
            )
        total_bytes += size_bytes
        validated.append(
            ValidatedDocument(
                index=index,
                path=candidate,
                filename=item.filename,
                collection_key=item.collection_key,
                sha256=digest,
                size_bytes=size_bytes,
            )
        )
    return ValidatedManifest(
        manifest_sha256=manifest_sha256,
        documents=tuple(validated),
        total_bytes=total_bytes,
    )


def load_state(path: Path, manifest_sha256: str) -> ReingestionState:
    """Read a matching resumable state file, or create an empty state."""

    if not path.exists():
        return ReingestionState(manifest_sha256=manifest_sha256)
    try:
        state = ReingestionState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        raise ReingestionError("The reingestion state file is invalid.") from exc
    if state.manifest_sha256 != manifest_sha256:
        raise ReingestionError("The state file belongs to a different manifest.")
    return state


def save_state(path: Path, state: ReingestionState) -> None:
    """Atomically persist content-free acceptance UUIDs."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ReingestionError("The reingestion state file could not be saved.") from exc


class ReingestionClient:
    """Cookie/CSRF-holding API client for one resumable migration session."""

    def __init__(
        self,
        bridge_url: str,
        *,
        timeout_seconds: float = 30.0,
        verify: bool | str = True,
        identity_header: str | None = None,
        identity: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if (identity_header is None) != (identity is None):
            raise ReingestionError("Identity header and value must be supplied together.")
        headers = {identity_header: identity} if identity_header and identity else None
        self._http = httpx.Client(
            base_url=bridge_url.rstrip("/"),
            timeout=timeout_seconds,
            verify=verify,
            headers=headers,
            follow_redirects=False,
            transport=transport,
        )
        self._csrf_token: str | None = None

    def __enter__(self) -> ReingestionClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self._http.close()

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            error = response.json()["error"]
            code = str(error["code"])
            message = str(error["message"])
        except (ValueError, KeyError, TypeError):
            code = "invalid_error_response"
            message = "Bridge returned an invalid error response."
        raise ReingestionError(f"{code}: {message}")

    def bootstrap(self) -> set[str]:
        """Require readiness, initialize CSRF, and collect all enabled keys."""

        try:
            health_response = self._http.get("/api/v2/health/ready")
            self._raise_for_response(health_response)
            health = HealthResponse.model_validate(health_response.json())
            if health.status != "OK":
                raise ReingestionError("Bridge is not ready for reingestion.")
            cursor: str | None = None
            keys: set[str] = set()
            while True:
                params = {"limit": 100}
                if cursor is not None:
                    params["cursor"] = cursor
                response = self._http.get("/api/v2/collections", params=params)
                self._raise_for_response(response)
                if self._csrf_token is None:
                    self._csrf_token = response.headers.get("x-csrf-token")
                page = CollectionListResponse.model_validate(response.json())
                keys.update(item.key for item in page.items if item.enabled)
                cursor = page.next_cursor
                if cursor is None:
                    break
            if not self._csrf_token:
                raise ReingestionError("Bridge did not establish a CSRF session.")
            return keys
        except httpx.RequestError as exc:
            raise ReingestionError("Bridge could not be reached.") from exc
        except ValidationError as exc:
            raise ReingestionError("Bridge returned an invalid v2 response.") from exc

    def upload(self, item: ValidatedDocument, *, idempotency_key: str) -> AcceptedDocument:
        if self._csrf_token is None:
            raise ReingestionError("The client session was not bootstrapped.")
        headers = {
            "X-CSRF-Token": self._csrf_token,
            "Idempotency-Key": idempotency_key,
        }
        try:
            with item.path.open("rb") as source_file:
                response = self._http.post(
                    f"/api/v2/collections/{item.collection_key}/documents",
                    headers=headers,
                    files={"file": (item.filename, source_file, "application/pdf")},
                )
            self._raise_for_response(response)
            accepted = UploadAcceptedResponse.model_validate(response.json())
        except OSError as exc:
            raise ReingestionError(f"Source entry {item.index} could not be reopened.") from exc
        except httpx.RequestError as exc:
            raise ReingestionError("Bridge upload acceptance is unknown; rerun unchanged.") from exc
        except ValidationError as exc:
            raise ReingestionError("Bridge returned an invalid upload response.") from exc
        return AcceptedDocument(
            document_id=accepted.document.id,
            operation_id=accepted.operation.id,
            state=accepted.document.state.value,
        )

    def document(self, document_id: uuid.UUID) -> DocumentDetail:
        try:
            response = self._http.get(f"/api/v2/documents/{document_id}")
            self._raise_for_response(response)
            return DocumentDetail.model_validate(response.json())
        except httpx.RequestError as exc:
            raise ReingestionError("Bridge document status could not be read.") from exc
        except ValidationError as exc:
            raise ReingestionError("Bridge returned an invalid document response.") from exc


def apply_manifest(
    manifest: ValidatedManifest,
    *,
    state_path: Path,
    client: ReingestionClient,
    max_outstanding: int = 5,
    wait_seconds: float = 0.0,
    poll_seconds: float = 2.0,
) -> ReingestionState:
    """Resume acceptance while never exceeding the bounded outstanding set."""

    if not 1 <= max_outstanding <= 5 or wait_seconds < 0 or poll_seconds <= 0:
        raise ValueError("reingestion concurrency and wait values are invalid")
    configured = client.bootstrap()
    missing = sorted({item.collection_key for item in manifest.documents} - configured)
    if missing:
        raise ReingestionError(
            "Manifest uses unavailable collections: " + ", ".join(missing)
        )
    state = load_state(state_path, manifest.manifest_sha256)
    accepted = dict(state.accepted)
    valid_indices = {str(item.index) for item in manifest.documents}
    if not set(accepted).issubset(valid_indices) or len(
        {item.document_id for item in accepted.values()}
    ) != len(accepted):
        raise ReingestionError("The state file contains invalid or duplicate acceptances.")
    deadline = time.monotonic() + wait_seconds

    while True:
        for index, retained in list(accepted.items()):
            if retained.state in _TERMINAL_STATES:
                continue
            detail = client.document(retained.document_id)
            accepted[index] = retained.model_copy(update={"state": detail.state.value})
        state = ReingestionState(
            manifest_sha256=manifest.manifest_sha256,
            accepted=accepted,
        )
        save_state(state_path, state)

        outstanding = sum(item.state not in _TERMINAL_STATES for item in accepted.values())
        pending = [item for item in manifest.documents if str(item.index) not in accepted]
        while pending and outstanding < max_outstanding:
            item = pending.pop(0)
            idempotency_key = (
                f"reingest-{manifest.manifest_sha256[:48]}-{item.index:06d}"
            )
            accepted[str(item.index)] = client.upload(
                item,
                idempotency_key=idempotency_key,
            )
            outstanding += 1
            state = ReingestionState(
                manifest_sha256=manifest.manifest_sha256,
                accepted=accepted,
            )
            save_state(state_path, state)

        if len(accepted) == len(manifest.documents) and outstanding == 0:
            return state
        if wait_seconds == 0 or time.monotonic() >= deadline:
            return state
        time.sleep(poll_seconds)
