"""Thin coordinator for dependency health checks."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from pdf_bridge.core.config import Settings
from pdf_bridge.services.health import dependency_checks


def check_dependencies(
    settings: Settings,
    session: Session,
    *,
    scanner_probe: Callable[..., bool],
) -> dict[str, str]:
    """Probe database, storage, scanner, and retrieval dependencies."""

    return dependency_checks(settings, session, scanner_probe=scanner_probe)
