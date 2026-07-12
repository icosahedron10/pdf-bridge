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

SettingsProvider = Callable[[], Settings]
ScannerFactory = Callable[[Settings], Scanner]
SessionScopeFactory = Callable[[], AbstractContextManager[Session]]


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
    with session_scope_factory() as session:
        return import_historical_manifest(
            session,
            manifest_path=manifest_path,
            source_root=source_root,
            settings=settings,
            scanner=scanner,
            dry_run=dry_run,
            actor_id=actor_id,
        )
