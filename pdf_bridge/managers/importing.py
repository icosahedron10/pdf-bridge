"""Thin dependency and transaction orchestration for historical imports."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import HistoricalImportResponse
from pdf_bridge.core.config import Settings
from pdf_bridge.services.historical_import import import_historical_manifest
from pdf_bridge.services.scanner import Scanner
from pdf_bridge.services.storage import StorageLayout, remove_storage_key, storage_key_for

SettingsProvider = Callable[[], Settings]
ScannerFactory = Callable[[Settings], Scanner]
SessionScopeFactory = Callable[[], AbstractContextManager[Session]]


class HistoricalImportCommitError(RuntimeError):
    """Raised when the import transaction failed and cleanup is incomplete."""


def _compensate_promoted_objects(
    settings: Settings, response: HistoricalImportResponse
) -> list[str]:
    """Remove every canonical object the failed import promoted.

    Every removal is attempted even after one fails; the storage keys that
    could not be removed are returned for operator follow-up.
    """

    layout = StorageLayout.from_root(settings.storage_root)
    failed_keys: list[str] = []
    for item in response.items:
        if item.document_id is None:
            continue
        storage_key = storage_key_for(item.document_id)
        try:
            remove_storage_key(layout, storage_key, missing_ok=True)
        except OSError:
            failed_keys.append(storage_key)
    return failed_keys


def run_manifest_import(
    *,
    manifest_path: Path,
    source_root: Path,
    dry_run: bool,
    actor_id: str,
    settings_provider: SettingsProvider,
    scanner_factory: ScannerFactory,
    session_scope_factory: SessionScopeFactory,
) -> HistoricalImportResponse:
    """Resolve runtime dependencies and execute one transactional import."""

    settings = settings_provider()
    scanner = scanner_factory(settings)
    response: HistoricalImportResponse | None = None
    try:
        with session_scope_factory() as session:
            response = import_historical_manifest(
                session,
                manifest_path=manifest_path,
                source_root=source_root,
                settings=settings,
                scanner=scanner,
                dry_run=dry_run,
                actor_id=actor_id,
            )
    except Exception as transaction_error:
        # The import service compensates failures raised while processing the
        # manifest. A populated response means only the surrounding commit
        # failed after every canonical object had already been promoted.
        if response is not None:
            failed_keys = _compensate_promoted_objects(settings, response)
            if failed_keys:
                raise HistoricalImportCommitError(
                    "the import transaction failed and these canonical objects "
                    "could not be removed: " + ", ".join(failed_keys)
                ) from transaction_error
        raise
    return response
