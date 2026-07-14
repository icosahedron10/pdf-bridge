from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

STREAMLIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STREAMLIT_ROOT))

from streamlit_app.tests.fake_bridge import StatefulBridgeClient  # noqa: E402


def _app(
    view_name: str,
    client: StatefulBridgeClient,
    *,
    document_id: str | None = None,
) -> AppTest:
    app = AppTest.from_file(str(STREAMLIT_ROOT / "views" / view_name), default_timeout=10)
    app.session_state["bridge_base_url"] = client.base_url
    app.session_state["bridge_client"] = client
    if document_id is not None:
        app.query_params["document"] = document_id
    return app


def _element_with_key(elements: Any, key: str) -> Any:
    matches = [element for element in elements if element.key == key]
    assert len(matches) == 1, f"expected one element with key {key!r}, found {len(matches)}"
    return matches[0]


def _button_with_label(app: AppTest, label: str) -> Any:
    matches = [button for button in app.button if button.label == label]
    assert len(matches) == 1, f"expected one {label!r} button, found {len(matches)}"
    return matches[0]


def _text(app: AppTest) -> str:
    elements = [
        *app.markdown,
        *app.caption,
        *app.info,
        *app.warning,
        *app.error,
        *app.success,
        *app.subheader,
    ]
    return "\n".join(str(element.value) for element in elements)


def test_clear_upload_is_tracked_and_refreshes_until_preflight_stops() -> None:
    client = StatefulBridgeClient()
    app = _app("upload.py", client).run()

    app.file_uploader[0].set_value(
        ("new-handbook.pdf", b"%PDF-1.7 deterministic", "application/pdf")
    )
    app.run()

    assert "Filename advisory" in _text(app)
    assert "existing-handbook.pdf" in _text(app)
    _button_with_label(app, "Upload 1 document").click()
    app.run()

    assert not app.exception
    assert len(client.uploads) == 1
    upload = client.uploads[0]
    assert upload["filename"] == "new-handbook.pdf"
    assert upload["content"] == b"%PDF-1.7 deterministic"
    document_id = str(upload["document_id"])
    assert app.session_state["tracked_documents"] == [
        {
            "document_id": document_id,
            "operation_id": f"op::{document_id}::preflight::1",
        }
    ]
    assert "Recent intake" in _text(app)
    assert "Polling every two seconds while durable work is open." in _text(app)
    first_read_count = client.document_reads[document_id]

    client.advance(document_id)
    app.run()

    assert client.document_reads[document_id] > first_read_count
    assert "Review Required" in _text(app)
    assert "Polling every two seconds while durable work is open." not in _text(app)

    _button_with_label(app, "Clear recent intake").click()
    app.run()

    assert app.session_state["tracked_documents"] == []


@pytest.mark.parametrize(
    ("document_id", "button_label", "expected_action", "expected_target"),
    [
        ("doc-review-keep", "Keep and publish", "KEEP", None),
        (
            "doc-review-replace",
            "Confirm replacement",
            "REPLACE",
            "doc-replacement-target",
        ),
        ("doc-review-cancel", "Confirm cancellation", "CANCEL", None),
    ],
)
def test_review_submits_revision_bound_keep_replace_and_cancel(
    document_id: str,
    button_label: str,
    expected_action: str,
    expected_target: str | None,
) -> None:
    client = StatefulBridgeClient()
    app = _app("workspace.py", client, document_id=document_id).run()

    assert not app.exception
    assert "The sealed revision is clear for publication." in _text(app)
    assert "Use the verified package." in _text(app)
    _button_with_label(app, button_label).click()
    app.run()

    assert not app.exception
    assert len(client.decisions) == 1
    decision = client.decisions[0]
    assert decision["document_id"] == document_id
    assert decision["prepared_revision_id"] == f"revision::{document_id}"
    assert decision["action"] == expected_action
    assert decision["target_document_id"] == expected_target
    assert len(str(decision["idempotency_key"])) >= 8
    expected_state = "DELETING" if expected_action == "CANCEL" else "PUBLISHING"
    assert client.documents_by_id[document_id]["state"] == expected_state


def test_retry_resumes_the_exact_failed_document_and_returns_to_review() -> None:
    client = StatefulBridgeClient()
    app = _app("workspace.py", client, document_id="doc-retry").run()

    assert "The formatter was temporarily unavailable." in _text(app)
    _button_with_label(app, "Retry eligible phase").click()
    app.run()

    assert not app.exception
    assert client.retries == [
        {
            "document_id": "doc-retry",
            "idempotency_key": client.retries[0]["idempotency_key"],
            "attempt": 2,
        }
    ]
    assert client.documents_by_id["doc-retry"]["state"] == "PREFLIGHTING"
    assert "Attempt 2" in _text(app)
    assert "Polling every two seconds while durable work is open." in _text(app)

    client.advance("doc-retry")
    app.run()

    assert client.documents_by_id["doc-retry"]["state"] == "REVIEW_REQUIRED"
    assert "The sealed revision is clear for publication." in _text(app)


def test_library_switches_rendered_and_raw_markdown_and_pages_chunk_provenance() -> None:
    client = StatefulBridgeClient()
    app = _app("library.py", client, document_id="doc-ready").run()
    inspector = _element_with_key(app.radio, "document-inspector::doc-ready")

    inspector.set_value("Markdown")
    app.run()

    assert "# Installation\n\nUse the verified package." in [
        element.value for element in app.markdown
    ]
    page = _element_with_key(app.selectbox, "markdown-page::doc-ready")
    page.select(1)
    app.run()

    assert "2 formatter slice(s)" in _text(app)
    markdown_view = _element_with_key(app.radio, "markdown-view::doc-ready::2")
    markdown_view.set_value("Raw")
    app.run()

    assert [element.value for element in app.code] == ["## Windows\n\nRun setup.exe."]
    assert _element_with_key(app.radio, "markdown-view::doc-ready::2").value == "Raw"

    _element_with_key(app.radio, "document-inspector::doc-ready").set_value("Chunks")
    app.run()

    assert [expander.label for expander in app.expander] == ["Chunk 0 · pages 1–1 · Installation"]
    assert "Chunk `chunk-installation`" in _text(app)
    assert "text `" + "d" * 64 + "`" in _text(app)
    _element_with_key(app.button, "cursor-next::document-chunks").click()
    app.run()

    assert [expander.label for expander in app.expander] == [
        "Chunk 1 · pages 2–2 · Installation / Windows"
    ]
    assert "Chunk `chunk-windows`" in _text(app)
    assert "Page 2" in _text(app)


def test_fresh_session_recovers_running_work_and_manual_refresh_reads_durable_state() -> None:
    client = StatefulBridgeClient()
    app = _app("library.py", client, document_id="doc-recovery").run()

    assert "Polling every two seconds while durable work is open." in _text(app)
    _element_with_key(app.radio, "document-inspector::doc-recovery").set_value("Lifecycle")
    app.run()
    assert "Upsert Active Points" in _text(app)
    reads_while_running = client.document_reads["doc-recovery"]

    client.advance("doc-recovery")
    app.run()

    assert client.document_reads["doc-recovery"] > reads_while_running
    assert "Polling every two seconds while durable work is open." not in _text(app)
    assert _button_with_label(app, "Refresh document")
    reads_before_refresh = client.document_reads["doc-recovery"]
    _button_with_label(app, "Refresh document").click()
    app.run()
    assert client.document_reads["doc-recovery"] > reads_before_refresh

    restarted = _app("library.py", client, document_id="doc-recovery").run()

    assert not restarted.exception
    assert "Ready" in _text(restarted)
    with pytest.raises(KeyError):
        restarted.session_state["tracked_documents"]
    _element_with_key(restarted.radio, "document-inspector::doc-recovery").set_value("Lifecycle")
    restarted.run()
    assert "Publication verification" in _text(restarted)
    assert "payload revision verified" in _text(restarted)


def test_high_priority_delete_shows_checkpoint_proofs_then_content_free_history() -> None:
    client = StatefulBridgeClient()
    app = _app("library.py", client, document_id="doc-delete").run()

    _button_with_label(app, "Confirm high-priority deletion").click()
    app.run()

    assert not app.exception
    assert len(client.deletions) == 1
    assert client.documents_by_id["doc-delete"]["state"] == "DELETING"
    _element_with_key(app.radio, "document-inspector::doc-delete").set_value("Lifecycle")
    app.run()
    assert "Verified deletion" in _text(app)
    assert "active zero 2026-07-13 15:00 UTC" in _text(app)
    assert "screening zero 2026-07-13 15:00 UTC" in _text(app)

    client.advance("doc-delete")
    history = _app("library.py", client).run()
    library_mode = next(radio for radio in history.radio if radio.label == "Library mode")
    library_mode.set_value("Terminal history")
    history.run()

    assert not history.exception
    assert "Document `doc-delete`" in _text(history)
    assert "Deleted" in _text(history)
    assert f"source `{'a' * 64}`" in _text(history)
    assert f"manifest `{'b' * 64}`" in _text(history)
    assert "delete-me.pdf" not in _text(history)
