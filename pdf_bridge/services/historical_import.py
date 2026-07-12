"""Validation and business workflow for controlled historical imports."""

from __future__ import annotations

import unicodedata
from pathlib import Path

from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import HistoricalImportResponse
from pdf_bridge.core.config import Settings
from pdf_bridge.services.lifecycle import (
    import_historical_manifest as import_catalog_records,
)
from pdf_bridge.services.scanner import Scanner
from pdf_bridge.services.storage import StorageLayout

MAX_IMPORT_MANIFEST_BYTES = 32 * 1024 * 1024


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
    """Validate an import envelope and delegate catalog/storage ingestion."""

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

    return import_catalog_records(
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
