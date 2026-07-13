"""Validation and business workflow for controlled historical imports.

Manifest version 3 no longer synthesizes already-ingested catalog rows.
Every imported PDF follows the normal intake path: it is copied, hashed,
scanned, registered as ``ANALYZING``, and queued for a real analysis
operation, so historical content earns its way into retrieval through the
same review pipeline as a live upload.
"""

from __future__ import annotations

import unicodedata
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import (
    HistoricalImportItemResult,
    HistoricalImportManifest,
    HistoricalImportResponse,
)
from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.models import ScanState
from pdf_bridge.services.intake import (
    DuplicateDocumentError,
    LifecycleError,
    find_exact_collection_duplicate,
    register_staged_upload,
)
from pdf_bridge.services.scanner import Scanner
from pdf_bridge.services.storage import (
    StorageLayout,
    copy_source_to_temporary,
    remove_storage_key,
    validate_pdf_filename,
    validate_source_path,
)

MAX_IMPORT_MANIFEST_BYTES = 32 * 1024 * 1024


class HistoricalImportCleanupError(RuntimeError):
    """The import failed and one or more promoted PDFs could not be removed."""

    def __init__(self, failed_storage_keys: list[str]) -> None:
        self.failed_storage_keys = tuple(failed_storage_keys)
        super().__init__(
            "historical import failed and these promoted canonical objects "
            "could not be removed: " + ", ".join(failed_storage_keys)
        )


def _compensate_promoted_objects(
    layout: StorageLayout, storage_keys: list[str]
) -> list[str]:
    """Attempt every promoted-object removal and return the failed keys."""

    failed_keys: list[str] = []
    for storage_key in reversed(storage_keys):
        try:
            remove_storage_key(layout, storage_key, missing_ok=True)
        except OSError:
            failed_keys.append(storage_key)
    return failed_keys


def validate_actor_id(actor_id: str) -> str:
    """Normalize and validate the non-secret operator audit identifier."""

    normalized = unicodedata.normalize("NFKC", actor_id).strip()
    if not normalized:
        raise ValueError("--actor-id cannot be blank")
    if len(normalized) > 255:
        raise ValueError("--actor-id must be 255 characters or fewer")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("--actor-id must not contain control characters")
    return normalized


def _import_catalog_records(
    session: Session,
    *,
    manifest_path: Path,
    source_root: Path,
    layout: StorageLayout,
    scanner: Scanner,
    max_bytes: int,
    dry_run: bool,
    actor_id: str,
    configured_collections: set[str],
) -> HistoricalImportResponse:
    """Validate, scan, and optionally register a trusted historical manifest."""

    manifest = HistoricalImportManifest.model_validate_json(manifest_path.read_bytes())
    results: list[HistoricalImportItemResult] = []
    seen_sources: set[Path] = set()
    seen_checksums: set[tuple[str, str]] = set()
    promoted_storage_keys: list[str] = []
    # Each entry follows the same path, duplicate, format, and scan gates as a
    # live upload before dry-run reporting or catalog registration.
    try:
        for entry in manifest.documents:
            if entry.collection_key not in configured_collections:
                raise LifecycleError(
                    "Historical import references an unconfigured collection.",
                    code="collection-not-configured",
                    status=422,
                )
            source = validate_source_path(source_root, entry.path)
            if source in seen_sources:
                raise LifecycleError(
                    "The historical manifest contains the same source path more than once.",
                    code="duplicate-manifest-entry",
                    status=422,
                )
            seen_sources.add(source)
            filename = validate_pdf_filename(entry.filename or source.name)
            staged = copy_source_to_temporary(source, layout, max_bytes=max_bytes)
            try:
                checksum_key = (staged.sha256, entry.collection_key)
                if checksum_key in seen_checksums:
                    raise LifecycleError(
                        "The historical manifest contains duplicate PDF contents "
                        "for one collection.",
                        code="duplicate-manifest-content",
                        status=422,
                    )
                seen_checksums.add(checksum_key)
                duplicate = find_exact_collection_duplicate(
                    session, sha256=staged.sha256, collection_key=entry.collection_key
                )
                if duplicate is not None:
                    raise DuplicateDocumentError(duplicate)
                scan_result = scanner(staged.path)
                if scan_result.state != ScanState.CLEAN:
                    raise LifecycleError(
                        "Historical source did not pass malware scanning.",
                        code="scan-not-clean",
                        status=422,
                    )
                if dry_run:
                    results.append(
                        HistoricalImportItemResult(
                            filename=filename,
                            sha256=staged.sha256,
                            size_bytes=staged.size_bytes,
                            collection_key=entry.collection_key,
                        )
                    )
                    continue

                registration = register_staged_upload(
                    session,
                    staged=staged,
                    layout=layout,
                    filename=filename,
                    collection_key=entry.collection_key,
                    idempotency_key=f"import:{uuid.uuid4()}",
                    actor_type="operator",
                    actor_id=actor_id,
                    scan_result=scan_result,
                )
                if registration.promoted is None:
                    raise RuntimeError(
                        "historical import registration did not promote canonical content"
                    )
                promoted_storage_keys.append(registration.promoted.storage_key)
                results.append(
                    HistoricalImportItemResult(
                        filename=filename,
                        sha256=staged.sha256,
                        size_bytes=staged.size_bytes,
                        collection_key=entry.collection_key,
                        document_id=registration.document.id,
                    )
                )
            finally:
                staged.path.unlink(missing_ok=True)
    except Exception as import_error:
        failed_keys = _compensate_promoted_objects(layout, promoted_storage_keys)
        if failed_keys:
            raise HistoricalImportCleanupError(failed_keys) from import_error
        raise
    return HistoricalImportResponse(
        dry_run=dry_run, imported=0 if dry_run else len(results), items=results
    )


def import_historical_manifest(
    session: Session,
    *,
    manifest_path: Path,
    source_root: Path,
    settings: Settings,
    scanner: Scanner,
    dry_run: bool,
    actor_id: str,
) -> HistoricalImportResponse:
    """Validate an import envelope and delegate catalog/storage registration."""

    resolved_manifest = manifest_path.expanduser().resolve(strict=True)
    resolved_source_root = source_root.expanduser().resolve(strict=True)
    if not resolved_manifest.is_file():
        raise ValueError("manifest path is not a regular file")
    if resolved_manifest.stat().st_size > MAX_IMPORT_MANIFEST_BYTES:
        raise ValueError(
            f"manifest exceeds the {MAX_IMPORT_MANIFEST_BYTES}-byte safety limit"
        )
    if not resolved_source_root.is_dir():
        raise ValueError("source root is not a directory")

    layout = StorageLayout.from_root(settings.storage_root)
    if (
        layout.root == resolved_source_root
        or layout.root in resolved_source_root.parents
        or resolved_source_root in layout.root.parents
    ):
        raise ValueError("source root and bridge storage root must not contain one another")

    return _import_catalog_records(
        session,
        manifest_path=resolved_manifest,
        source_root=resolved_source_root,
        layout=layout,
        scanner=scanner,
        max_bytes=settings.max_upload_bytes,
        dry_run=dry_run,
        actor_id=validate_actor_id(actor_id),
        configured_collections={collection.key for collection in settings.collections},
    )
