from __future__ import annotations

from litestar.testing import TestClient

from pdf_bridge import api


def test_health_endpoints_report_dependency_state(client: TestClient, monkeypatch) -> None:
    live = client.get("/api/v1/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "ok", "checks": {"process": "ok"}}

    monkeypatch.setattr(api, "clamd_ping", lambda **_kwargs: True)
    for path in ("ready", "dependencies"):
        healthy = client.get(f"/api/v1/health/{path}")
        assert healthy.status_code == 200
        assert healthy.json()["checks"] == {
            "database": "ok",
            "storage": "ok",
            "scanner": "ok",
        }

    monkeypatch.setattr(api, "clamd_ping", lambda **_kwargs: False)
    degraded = client.get("/api/v1/health/dependencies")
    assert degraded.status_code == 503
    assert degraded.json()["status"] == "degraded"
    assert degraded.json()["checks"]["scanner"] == "error"


def test_request_ids_are_bounded_and_log_safe(client: TestClient) -> None:
    accepted = client.get("/api/v1/health/live", headers={"X-Request-ID": "job:123.safe"})
    assert accepted.headers["x-request-id"] == "job:123.safe"

    rejected = client.get("/api/v1/health/live", headers={"X-Request-ID": "not log safe"})
    assert rejected.headers["x-request-id"] != "not log safe"
    assert len(rejected.headers["x-request-id"]) == 36
