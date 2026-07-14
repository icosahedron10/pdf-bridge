from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

STREAMLIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STREAMLIT_ROOT))

import bridge_ui  # noqa: E402


class FakeBridgeClient:
    base_url = "https://bridge.test"
    csrf_token = "test-token"
    identity_header_name = None
    identity = None

    def close(self) -> None:
        raise AssertionError("the stable fake session should not be closed")

    def health(self, probe: str) -> dict[str, Any]:
        if probe == "live":
            return {"status": "OK", "checks": []}
        return {
            "status": "OK",
            "checks": [{"component": "catalog", "status": "READY"}],
        }

    def collections(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        assert cursor is None
        counts = {
            "PREFLIGHTING": 1,
            "PREFLIGHT_FAILED": 0,
            "REVIEW_REQUIRED": 1,
            "PUBLISHING": 0,
            "PUBLISH_FAILED": 0,
            "READY": 4,
            "DELETING": 0,
            "DELETE_FAILED": 0,
            "REJECTED": 0,
            "CANCELLED": 0,
            "DELETED": 0,
        }
        return {
            "items": [
                {
                    "key": "customer",
                    "display_name": "Customer",
                    "description": "Customer-facing documentation",
                    "audience": "customer",
                    "enabled": True,
                    "counts": {"total": sum(counts.values()), "by_state": counts},
                }
            ],
            "limit": limit,
            "next_cursor": None,
            "has_more": False,
        }

    def collection(self, collection_key: str) -> dict[str, Any]:
        assert collection_key == "customer"
        return {
            "target": {
                "schema_version": 2,
                "schema_compatible": True,
                "qdrant_collection_name": "pdf-customer-v2",
            }
        }

    def operation_metrics(self) -> dict[str, Any]:
        return {
            "generated_at": "2026-07-13T15:00:00Z",
            "total": 2,
            "queued": 1,
            "running": 1,
            "failed": 0,
            "oldest_queued_age_seconds": 75.0,
            "buckets": [
                {
                    "operation_type": "PREFLIGHT",
                    "state": "QUEUED",
                    "phase": "EXTRACTING",
                    "count": 1,
                    "oldest_operation_age_seconds": 75.0,
                    "oldest_phase_age_seconds": 45.0,
                },
                {
                    "operation_type": "PUBLISH",
                    "state": "RUNNING",
                    "phase": "UPSERT_ACTIVE_POINTS",
                    "count": 1,
                    "oldest_operation_age_seconds": 20.0,
                    "oldest_phase_age_seconds": 10.0,
                },
            ],
        }

    def documents(
        self,
        collection_key: str,
        *,
        state: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        assert collection_key == "customer"
        assert cursor is None
        return {
            "items": [],
            "limit": limit,
            "next_cursor": None,
            "has_more": False,
        }


def test_default_operations_page_renders_with_api_v2_payloads() -> None:
    app = AppTest.from_file(str(STREAMLIT_ROOT / "app.py"))
    app.session_state["bridge_base_url"] = "https://bridge.test"
    app.session_state["bridge_client"] = FakeBridgeClient()

    app.run(timeout=10)

    assert not app.exception
    assert [title.value for title in app.title] == ["Operations"]
    metric_labels = {metric.label for metric in app.metric}
    assert {
        "Ready",
        "Processing",
        "Review",
        "Failed",
        "Deleting",
        "Queued",
        "Running",
        "Oldest queued",
    } <= metric_labels


def test_blank_compose_identity_header_keeps_forwarding_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PDF_BRIDGE_STREAMLIT_IDENTITY_HEADER", "")
    app = AppTest.from_file(str(STREAMLIT_ROOT / "app.py"))
    app.session_state["bridge_base_url"] = "https://bridge.test"
    app.session_state["bridge_client"] = FakeBridgeClient()

    app.run(timeout=10)

    assert not app.exception


@pytest.mark.parametrize(
    ("view_name", "title"),
    [
        ("upload.py", "Intake"),
        ("workspace.py", "Review"),
        ("library.py", "Library"),
        ("search.py", "Search"),
    ],
)
def test_each_operator_view_has_a_clean_initial_render(view_name: str, title: str) -> None:
    app = AppTest.from_file(str(STREAMLIT_ROOT / "views" / view_name))
    app.session_state["bridge_base_url"] = "https://bridge.test"
    app.session_state["bridge_client"] = FakeBridgeClient()

    app.run(timeout=10)

    assert not app.exception
    assert [item.value for item in app.title] == [title]


def test_upload_selection_limit_defaults_to_expected_queue_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES", raising=False)
    assert bridge_ui.max_upload_files() == 5

    monkeypatch.setenv("PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES", "12")
    assert bridge_ui.max_upload_files() == 12


@pytest.mark.parametrize("value", ["0", "21", "five", " 5"])
def test_upload_selection_limit_fails_on_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES", value)

    with pytest.raises(RuntimeError, match="integer from 1 through 20"):
        bridge_ui.max_upload_files()
