"""Content-safe liveness and readiness evaluation."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import HealthCheck, HealthResponse
from pdf_bridge.core.config import Settings

ProviderChecks = Mapping[str, tuple[bool, str | None]]


def liveness() -> HealthResponse:
    """Report process liveness without probing dependencies."""

    return HealthResponse(
        status="OK",
        checks=[HealthCheck(component="process", status="READY")],
    )


def _catalog_check(session: Session) -> HealthCheck:
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        return HealthCheck(
            component="catalog",
            status="NOT_READY",
            failure_code="catalog_unavailable",
            message="The catalog is unavailable.",
        )
    return HealthCheck(component="catalog", status="READY")


def _storage_check(root: Path) -> HealthCheck:
    required = (
        root,
        root / "objects",
        root / "artifacts",
        root / "temporary",
        root / "quarantine",
    )
    if all(path.is_dir() and os.access(path, os.W_OK) for path in required):
        return HealthCheck(component="storage", status="READY")
    return HealthCheck(
        component="storage",
        status="NOT_READY",
        failure_code="storage_unavailable",
        message="Canonical storage is unavailable.",
    )


def _scanner_check(
    settings: Settings,
    scanner_probe: Callable[..., bool],
) -> HealthCheck:
    try:
        ready = scanner_probe(
            host=settings.clamd_host,
            port=settings.clamd_port,
            timeout=min(settings.clamd_timeout_seconds, 2.0),
        )
    except Exception:
        ready = False
    if ready:
        return HealthCheck(component="clamav", status="READY")
    return HealthCheck(
        component="clamav",
        status="NOT_READY",
        failure_code="scanner_unavailable",
        message="Malware screening is unavailable.",
    )


def readiness(
    settings: Settings,
    session: Session,
    *,
    scanner_probe: Callable[..., bool],
    provider_checks: ProviderChecks | None,
) -> HealthResponse:
    """Combine local infrastructure and worker provider readiness."""

    checks = [
        _catalog_check(session),
        _storage_check(settings.storage_root),
        _scanner_check(settings, scanner_probe),
    ]
    if provider_checks is None:
        status = "DISABLED" if not settings.worker_enabled else "NOT_READY"
        failure_code = None if status == "DISABLED" else "worker_unavailable"
        message = None if status == "DISABLED" else "The processing worker is unavailable."
        checks.append(
            HealthCheck(
                component="worker",
                status=status,
                failure_code=failure_code,
                message=message,
            )
        )
    else:
        for component in sorted(provider_checks):
            ready, failure_code = provider_checks[component]
            checks.append(
                HealthCheck(
                    component=component,
                    status="READY" if ready else "NOT_READY",
                    failure_code=None if ready else (failure_code or "dependency_unavailable"),
                    message=None if ready else "A required processing dependency is unavailable.",
                )
            )
    overall = "OK" if all(check.status == "READY" for check in checks) else "NOT_READY"
    return HealthResponse(status=overall, checks=checks)
