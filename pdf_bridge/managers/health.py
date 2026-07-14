"""Thin health-check coordinator."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import HealthResponse
from pdf_bridge.core.config import Settings
from pdf_bridge.services.health import liveness, readiness


def live() -> HealthResponse:
    return liveness()


def ready(
    settings: Settings,
    session: Session,
    *,
    scanner_probe: Callable[..., bool],
    provider_checks: Mapping[str, tuple[bool, str | None]] | None,
) -> HealthResponse:
    return readiness(
        settings,
        session,
        scanner_probe=scanner_probe,
        provider_checks=provider_checks,
    )
