"""Safe, streaming filesystem primitives for canonical PDF storage."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

PDF_HEADER_BYTES = 1024
DEFAULT_CHUNK_BYTES = 1024 * 1024


class StorageError(RuntimeError):
    pass


class InvalidFilenameError(StorageError):
    pass


class InvalidPdfError(StorageError):
    pass


class FileTooLargeError(StorageError):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"file exceeds the configured {max_bytes}-byte limit")
        self.max_bytes = max_bytes


class UnsafePathError(StorageError):
    pass


class AsyncReadable(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class StorageLayout:
    root: Path
    objects: Path
    temporary: Path
    quarantine: Path

    @classmethod
    def from_root(cls, root: Path) -> StorageLayout:
        resolved = root.expanduser().resolve(strict=False)
        layout = cls(
            root=resolved,
            objects=resolved / "objects",
            temporary=resolved / "temporary",
            quarantine=resolved / "quarantine",
        )
        for directory in (layout.root, layout.objects, layout.temporary, layout.quarantine):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return layout


@dataclass(frozen=True, slots=True)
class StagedFile:
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PromotedFile:
    storage_key: str
    path: Path


_WHITESPACE = re.compile(r"\s+")


def validate_pdf_filename(filename: str) -> str:
    """Validate and normalize a display name; it is never used as a disk path."""

    if not isinstance(filename, str):
        raise InvalidFilenameError("filename must be text")
    candidate = unicodedata.normalize("NFKC", filename).strip()
    if not candidate or candidate in {".", ".."}:
        raise InvalidFilenameError("filename cannot be blank")
    if any(character in candidate for character in ("/", "\\", "\x00")):
        raise InvalidFilenameError("filename must not contain path separators")
    if any(unicodedata.category(character) == "Cc" for character in candidate):
        raise InvalidFilenameError("filename must not contain control characters")
    candidate = _WHITESPACE.sub(" ", candidate)
    if len(candidate) > 255:
        raise InvalidFilenameError("filename must be 255 characters or fewer")
    if not candidate.casefold().endswith(".pdf"):
        raise InvalidFilenameError("only .pdf files are accepted")
    return candidate


def normalize_filename(filename: str) -> str:
    """Return a stable filename key for case-insensitive duplicate warnings."""

    return validate_pdf_filename(filename).casefold()


def has_pdf_signature(header: bytes) -> bool:
    """Perform format allowlisting without attempting to parse untrusted PDF data."""

    return header[:PDF_HEADER_BYTES].lstrip(b"\x00\t\n\x0c\r ").startswith(b"%PDF-")


def _temporary_path(layout: StorageLayout) -> Path:
    return layout.temporary / f"{uuid.uuid4()}.upload"


async def stream_upload(
    upload: AsyncReadable,
    layout: StorageLayout,
    *,
    max_bytes: int,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> StagedFile:
    """Stream an UploadFile-like object to disk while hashing and enforcing size."""

    if max_bytes <= 0 or chunk_bytes <= 0:
        raise ValueError("max_bytes and chunk_bytes must be positive")

    destination = _temporary_path(layout)
    digest = hashlib.sha256()
    header = bytearray()
    size = 0
    try:
        with destination.open("xb") as output:
            try:
                os.chmod(destination, 0o600)
            except OSError:
                # Windows and some mounted filesystems do not implement POSIX modes.
                pass
            while True:
                chunk = await upload.read(chunk_bytes)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise StorageError("upload reader returned non-byte content")
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLargeError(max_bytes)
                if len(header) < PDF_HEADER_BYTES:
                    header.extend(chunk[: PDF_HEADER_BYTES - len(header)])
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        if size == 0:
            raise InvalidPdfError("empty files are not valid PDFs")
        if not has_pdf_signature(bytes(header)):
            raise InvalidPdfError("file does not have a valid PDF signature")
        return StagedFile(path=destination, size_bytes=size, sha256=digest.hexdigest())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def copy_source_to_temporary(
    source: Path,
    layout: StorageLayout,
    *,
    max_bytes: int,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> StagedFile:
    """Copy a trusted-root historical source through the same validation path."""

    if max_bytes <= 0 or chunk_bytes <= 0:
        raise ValueError("max_bytes and chunk_bytes must be positive")
    destination = _temporary_path(layout)
    digest = hashlib.sha256()
    header = bytearray()
    size = 0
    try:
        with source.open("rb") as input_file, destination.open("xb") as output:
            while chunk := input_file.read(chunk_bytes):
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLargeError(max_bytes)
                if len(header) < PDF_HEADER_BYTES:
                    header.extend(chunk[: PDF_HEADER_BYTES - len(header)])
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0 or not has_pdf_signature(bytes(header)):
            raise InvalidPdfError("source file does not have a valid PDF signature")
        return StagedFile(path=destination, size_bytes=size, sha256=digest.hexdigest())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def storage_key_for(document_id: uuid.UUID) -> str:
    identifier = str(document_id)
    return f"objects/{identifier[:2]}/{identifier}.pdf"


def resolve_storage_key(layout: StorageLayout, storage_key: str) -> Path:
    """Resolve a database storage key and reject absolute/traversing values."""

    pure = PurePosixPath(storage_key)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise UnsafePathError("invalid storage key")
    resolved = layout.root.joinpath(*pure.parts).resolve(strict=False)
    try:
        resolved.relative_to(layout.root)
    except ValueError as exc:
        raise UnsafePathError("storage key escapes the storage root") from exc
    return resolved


def promote_staged_file(
    staged: StagedFile, layout: StorageLayout, document_id: uuid.UUID
) -> PromotedFile:
    """Atomically promote a scanned file to its UUID-derived canonical name."""

    key = storage_key_for(document_id)
    destination = resolve_storage_key(layout, key)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        raise StorageError("canonical storage key already exists")
    os.replace(staged.path, destination)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return PromotedFile(storage_key=key, path=destination)


def remove_storage_key(
    layout: StorageLayout, storage_key: str, *, missing_ok: bool = False
) -> None:
    path = resolve_storage_key(layout, storage_key)
    path.unlink(missing_ok=missing_ok)


def validate_source_path(source_root: Path, candidate: Path | str) -> Path:
    """Resolve a manifest path, including symlinks, beneath an approved source root."""

    root = source_root.expanduser().resolve(strict=True)
    requested = Path(candidate).expanduser()
    if not requested.is_absolute():
        requested = root / requested
    resolved = requested.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError("manifest source path escapes source_root") from exc
    if not resolved.is_file():
        raise UnsafePathError("manifest source path is not a regular file")
    return resolved


def hash_file(path: Path, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as file:
        while chunk := file.read(chunk_bytes):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()
