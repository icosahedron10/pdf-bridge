"""Runtime dependency health checks."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from pdf_bridge.core.config import Settings

logger = logging.getLogger(__name__)


def dependency_checks(
    settings: Settings,
    session: Session,
    *,
    scanner_probe: Callable[..., bool],
) -> dict[str, str]:
    """Check the database, canonical storage, and malware scanner."""

    checks: dict[str, str] = {}
    try:
        session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        logger.exception("readiness database check failed")
        checks["database"] = "error"
    root = Path(settings.storage_root)
    storage_directories = (root, root / "objects", root / "temporary", root / "quarantine")
    checks["storage"] = (
        "ok"
        if all(path.is_dir() and os.access(path, os.W_OK) for path in storage_directories)
        else "error"
    )
    checks["scanner"] = (
        "ok"
        if scanner_probe(
            host=settings.clamd_host,
            port=settings.clamd_port,
            timeout=min(settings.clamd_timeout, 2.0),
        )
        else "error"
    )
    return checks
