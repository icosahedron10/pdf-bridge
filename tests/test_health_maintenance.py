from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.orm import Session

from pdf_bridge.persistence.db import Base, build_engine
from pdf_bridge.services.health import readiness


def test_disabled_worker_never_reports_http_ready(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    for name in ("objects", "artifacts", "temporary", "quarantine"):
        (storage / name).mkdir(parents=True, exist_ok=True)
    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = SimpleNamespace(
        storage_root=storage,
        worker_enabled=False,
        clamd_host="clamav",
        clamd_port=3310,
        clamd_timeout_seconds=1.0,
    )
    try:
        with Session(engine) as session:
            response = readiness(
                settings,  # type: ignore[arg-type]
                session,
                scanner_probe=lambda **_kwargs: True,
                provider_checks=None,
            )
    finally:
        engine.dispose()

    worker = next(check for check in response.checks if check.component == "worker")
    assert worker.status == "DISABLED"
    assert response.status == "NOT_READY"
