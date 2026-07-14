"""Shared Streamlit chrome: client access, formatting, badges, and errors."""

from __future__ import annotations

import html
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import streamlit as st

from bridge_client import BridgeClient, BridgeProblem, BridgeUnreachable

DEFAULT_BASE_URL = os.environ.get("PDF_BRIDGE_URL", "http://127.0.0.1:8000")

T = TypeVar("T")

# One semantic tone per lifecycle family so every page colors states the same way.
_TONES = {
    "ok": ("#e5f3ec", "#116149"),
    "info": ("#e7eef7", "#1d5183"),
    "warn": ("#faf0da", "#7a5410"),
    "danger": ("#fbe9e9", "#96302e"),
    "neutral": ("#eceef1", "#4d5661"),
}

DOCUMENT_STATE_TONES = {
    "ANALYZING": "info",
    "REVIEW_REQUIRED": "warn",
    "INGESTING": "info",
    "INGEST_FAILED": "danger",
    "INGESTED": "ok",
    "REPLACING": "info",
    "REPLACE_FAILED": "danger",
    "DELETING": "info",
    "DELETE_FAILED": "danger",
    "CLEANUP_PENDING": "info",
    "CLEANUP_FAILED": "danger",
    "REJECTED": "neutral",
    "CANCELLED": "neutral",
    "DELETED": "neutral",
}

OPERATION_STATE_TONES = {
    "QUEUED": "neutral",
    "RUNNING": "info",
    "SUCCEEDED": "ok",
    "FAILED": "danger",
    "CANCELLED": "neutral",
}

SCAN_STATE_TONES = {
    "PENDING": "neutral",
    "CLEAN": "ok",
    "INFECTED": "danger",
    "ERROR": "danger",
}

FINDING_LABEL_TONES = {
    "near_duplicate": "danger",
    "likely_revision": "warn",
    "potential_contradiction": "danger",
    "consistent_overlap": "ok",
    "unrelated": "neutral",
    "uncertain": "neutral",
}

ANALYSIS_PHASES = (
    "QUEUED",
    "EXTRACTING",
    "COMPARING",
    "AWAITING_DECISION",
    "DELETING_EXISTING",
    "INGESTING",
    "CLEANING_UP",
    "COMPLETE",
)

_PAGE_CSS = """
<style>
.block-container { padding-top: 2.4rem; }
.pdfb-badge {
    display: inline-block;
    padding: 0.14rem 0.55rem;
    border-radius: 999px;
    font-size: 0.74rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    white-space: nowrap;
}
.pdfb-kv { color: rgba(49, 51, 63, 0.6); font-size: 0.8rem; margin-bottom: 0.1rem; }
.pdfb-mono { font-family: "Source Code Pro", monospace; font-size: 0.8rem; word-break: break-all; }
.pdfb-excerpt {
    border-left: 3px solid #d5a846;
    background: rgba(213, 168, 70, 0.07);
    padding: 0.5rem 0.75rem;
    border-radius: 0 6px 6px 0;
    font-size: 0.85rem;
    white-space: pre-wrap;
}
</style>
"""


def apply_chrome(title: str, caption: str) -> None:
    """Render the shared page header and inject the app stylesheet."""

    st.markdown(_PAGE_CSS, unsafe_allow_html=True)
    st.title(title)
    st.caption(caption)


def get_client() -> BridgeClient:
    """Return the per-browser-session bridge client, rebuilding on URL change."""

    base_url = st.session_state.get("bridge_base_url", DEFAULT_BASE_URL)
    client: BridgeClient | None = st.session_state.get("bridge_client")
    if client is None or client.base_url != base_url.rstrip("/"):
        if client is not None:
            client.close()
        client = BridgeClient(base_url)
        st.session_state["bridge_client"] = client
    return client


def render_connection_settings() -> None:
    """Render the sidebar control for the PDF Bridge base URL."""

    with st.sidebar:
        st.text_input(
            "PDF Bridge URL",
            key="bridge_base_url",
            help="Base URL of the running PDF Bridge service.",
        )


def badge(text: str, tone: str = "neutral") -> str:
    """Return a colored status pill as inline HTML."""

    background, foreground = _TONES.get(tone, _TONES["neutral"])
    return (
        f'<span class="pdfb-badge" style="background:{background};color:{foreground};">'
        f"{html.escape(text)}</span>"
    )


def state_badge(state: str) -> str:
    """Badge for a document lifecycle state."""

    return badge(state.replace("_", " ").title(), DOCUMENT_STATE_TONES.get(state, "neutral"))


def render_problem(error: BridgeProblem) -> None:
    """Render an API problem with its stable code and any duplicate match."""

    st.error(f"**{error.title}** — {error.detail}")
    meta = f"`{error.code}` · HTTP {error.status}"
    if error.request_id:
        meta += f" · request `{error.request_id}`"
    st.caption(meta)
    if error.duplicate:
        duplicate = error.duplicate
        st.info(
            "Matching document already in this collection: "
            f"**{duplicate.get('filename', 'unknown')}** "
            f"({fmt_bytes(duplicate.get('size_bytes', 0))}, "
            f"state {duplicate.get('state', 'unknown')})."
        )


def render_unreachable(error: BridgeUnreachable) -> None:
    """Render a connection failure with recovery guidance."""

    st.error(f"**PDF Bridge is unreachable.** {error}")
    st.markdown(
        "Start the service (`docker compose up --build` or "
        "`uvicorn pdf_bridge.app:app`), then adjust the URL in the sidebar "
        "if it is not listening on the default address."
    )


def guarded(operation: Callable[[], T]) -> T | None:
    """Run one API call and render any bridge failure instead of raising."""

    try:
        return operation()
    except BridgeProblem as error:
        render_problem(error)
        return None
    except BridgeUnreachable as error:
        render_unreachable(error)
        return None


def fmt_bytes(size: int | None) -> str:
    """Format a byte count in binary units."""

    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(size)} B"


def parse_dt(value: str | None) -> datetime | None:
    """Parse an API ISO 8601 timestamp."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fmt_dt(value: str | None) -> str:
    """Format an API timestamp as a compact UTC string."""

    parsed = parse_dt(value)
    if parsed is None:
        return "—"
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def fmt_relative(value: str | None) -> str:
    """Format an API timestamp as a coarse relative age."""

    parsed = parse_dt(value)
    if parsed is None:
        return "—"
    seconds = max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def collection_options(collections_payload: dict[str, Any]) -> dict[str, str]:
    """Map collection keys to display labels for select widgets."""

    return {
        item["key"]: f"{item['display_name']} ({item['key']})"
        for item in collections_payload.get("items", [])
    }


def render_filename_warnings(warnings: list[dict[str, Any]]) -> None:
    """Render advisory filename-family warnings with their matched documents."""

    if not warnings:
        return
    st.warning(
        f"{len(warnings)} advisory filename "
        f"warning{'s' if len(warnings) != 1 else ''} — similar names already "
        "exist in this collection. Uploads still proceed; review before deciding."
    )
    for warning in warnings:
        matched = warning.get("matched", {})
        shared = ", ".join(warning.get("shared_tokens", [])) or "—"
        st.markdown(
            f"- `{warning.get('kind', 'warning')}` at "
            f"{warning.get('similarity', 0):.0%} similarity to "
            f"**{matched.get('filename', 'unknown')}** "
            f"(state {matched.get('state', '?')}) · shared tokens: {shared}"
        )


def phase_progress(phase: str | None) -> float:
    """Return the fractional progress of an operation phase."""

    if phase is None:
        return 0.0
    try:
        index = ANALYSIS_PHASES.index(phase)
    except ValueError:
        return 0.0
    return (index + 1) / len(ANALYSIS_PHASES)
