"""Shared Streamlit chrome, lifecycle formatting, and cursor navigation."""

from __future__ import annotations

import html
import os
from collections.abc import Callable, Hashable
from datetime import UTC, datetime
from typing import Any, TypeVar

import streamlit as st

from bridge_client import BridgeClient, BridgeProblem, BridgeUnreachable, new_idempotency_key

DEFAULT_BASE_URL = os.environ.get("PDF_BRIDGE_URL", "http://127.0.0.1:8000")
DEFAULT_MAX_UPLOAD_FILES = 5
MAX_UPLOAD_FILES_CEILING = 20

T = TypeVar("T")


def max_upload_files() -> int:
    """Return the bounded operator selection cap or fail on bad deployment config."""

    raw = os.environ.get(
        "PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES", str(DEFAULT_MAX_UPLOAD_FILES)
    )
    if not raw.isdecimal():
        raise RuntimeError(
            "PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES must be an integer from 1 through 20"
        )
    value = int(raw)
    if not 1 <= value <= MAX_UPLOAD_FILES_CEILING:
        raise RuntimeError(
            "PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES must be an integer from 1 through 20"
        )
    return value

_TONES = {
    "ok": ("#e4f2eb", "#145c47", "#b9dacd"),
    "info": ("#e9eff6", "#274f77", "#c7d6e6"),
    "warn": ("#f8eed9", "#765313", "#e6cf9d"),
    "danger": ("#f8e7e5", "#8c302c", "#e8beb9"),
    "neutral": ("#eef0f2", "#4c5661", "#d8dde2"),
}

DOCUMENT_STATE_TONES = {
    "PREFLIGHTING": "info",
    "PREFLIGHT_FAILED": "danger",
    "REVIEW_REQUIRED": "warn",
    "PUBLISHING": "info",
    "PUBLISH_FAILED": "danger",
    "READY": "ok",
    "DELETING": "info",
    "DELETE_FAILED": "danger",
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

WORKING_DOCUMENT_STATES = {"PREFLIGHTING", "PUBLISHING", "DELETING"}
FAILED_DOCUMENT_STATES = {"PREFLIGHT_FAILED", "PUBLISH_FAILED", "DELETE_FAILED"}
TERMINAL_DOCUMENT_STATES = {"REJECTED", "CANCELLED", "DELETED"}

_PHASE_SEQUENCES = {
    "PREFLIGHT": (
        "QUEUED",
        "EXTRACTING",
        "CHECKING_ELIGIBILITY",
        "PACKING_FORMATTER_BATCHES",
        "FORMATTING_MARKDOWN",
        "VALIDATING_MARKDOWN",
        "CHUNKING_MARKDOWN",
        "EMBEDDING_DENSE",
        "EMBEDDING_SPARSE",
        "UPSERTING_SCREENING_POINTS",
        "DISCOVERING_CANDIDATES",
        "CLASSIFYING_CANDIDATES",
        "SEALING_REVISION",
        "AWAITING_DECISION",
        "COMPLETE",
    ),
    "PUBLISH": (
        "QUEUED",
        "DELETE_ACTIVE_POINTS",
        "VERIFY_ACTIVE_ZERO",
        "UPSERT_ACTIVE_POINTS",
        "VERIFY_ACTIVE_POINTS",
        "REMOVE_SCREENING_POINTS",
        "VERIFY_SCREENING_REMOVAL",
        "COMPLETE",
    ),
    "DELETE": (
        "QUEUED",
        "DELETE_ACTIVE_POINTS",
        "VERIFY_ACTIVE_ZERO",
        "DELETE_SCREENING_POINTS",
        "VERIFY_SCREENING_ZERO",
        "PURGE_STORAGE",
        "COMMIT_TOMBSTONE",
        "COMPLETE",
    ),
}

_PAGE_CSS = """
<style>
:root {
    --pdfb-ink: #20262d;
    --pdfb-muted: #66717d;
    --pdfb-line: #dfe3e7;
    --pdfb-panel: #f7f8f9;
}
.block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1500px; }
[data-testid="stSidebar"] { border-right: 1px solid var(--pdfb-line); }
h1 { letter-spacing: -0.025em; }
.pdfb-eyebrow {
    color: var(--pdfb-muted); font-size: 0.72rem; font-weight: 700;
    letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: -0.5rem;
}
.pdfb-tag {
    display: inline-block; padding: 0.12rem 0.48rem; border: 1px solid;
    border-radius: 6px; font-size: 0.72rem; font-weight: 650;
    letter-spacing: 0.015em; line-height: 1.4; white-space: nowrap;
}
.pdfb-kv { color: var(--pdfb-muted); font-size: 0.74rem; margin-bottom: 0.08rem; }
.pdfb-mono {
    font-family: "Source Code Pro", ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 0.79rem; overflow-wrap: anywhere;
}
.pdfb-excerpt {
    border-left: 3px solid #c5973f; background: #fbf8f0; padding: 0.65rem 0.8rem;
    border-radius: 0 6px 6px 0; font-size: 0.85rem; white-space: pre-wrap;
}
.pdfb-rule { border-top: 1px solid var(--pdfb-line); margin: 0.6rem 0 1rem; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 8px; }
.stButton > button, .stDownloadButton > button { border-radius: 6px; }
</style>
"""


def apply_chrome(title: str, caption: str, *, eyebrow: str = "PDF Bridge") -> None:
    """Render the shared restrained operator header and stylesheet."""

    st.markdown(_PAGE_CSS, unsafe_allow_html=True)
    st.markdown(f'<p class="pdfb-eyebrow">{html.escape(eyebrow)}</p>', unsafe_allow_html=True)
    st.title(title)
    st.caption(caption)


def get_client() -> BridgeClient:
    """Return one cookie-preserving client for the Streamlit browser session."""

    base_url = st.session_state.setdefault("bridge_base_url", DEFAULT_BASE_URL).strip()
    identity_header_name = os.environ.get("PDF_BRIDGE_STREAMLIT_IDENTITY_HEADER")
    identity: str | None = None
    if identity_header_name is not None:
        if identity_header_name != identity_header_name.strip():
            raise RuntimeError(
                "PDF_BRIDGE_STREAMLIT_IDENTITY_HEADER must name a valid proxy header"
            )
        if identity_header_name:
            identity = st.context.headers.get(identity_header_name)
            if identity is None or not identity.strip():
                raise RuntimeError(
                    "The configured proxy identity header was not present on the Streamlit request"
                )
            identity = identity.strip()
        else:
            identity_header_name = None
    client: BridgeClient | None = st.session_state.get("bridge_client")
    if (
        client is None
        or client.base_url != base_url.rstrip("/")
        or client.identity_header_name != identity_header_name
        or client.identity != identity
    ):
        if client is not None:
            client.close()
        client = BridgeClient(
            base_url,
            identity_header_name=identity_header_name,
            identity=identity,
        )
        st.session_state["bridge_client"] = client
    return client


def render_connection_settings() -> None:
    """Render the service boundary control in the sidebar."""

    with st.sidebar:
        st.caption("SERVICE")
        st.caption("Deployment-owned API endpoint")
        st.code(DEFAULT_BASE_URL, language=None)
        client: BridgeClient | None = st.session_state.get("bridge_client")
        if client is not None and client.csrf_token:
            st.caption("Session established · CSRF ready")
        else:
            st.caption("Session starts on the first authenticated request")


def badge(text: str, tone: str = "neutral") -> str:
    """Return a compact, rectangular semantic status tag."""

    background, foreground, border = _TONES.get(tone, _TONES["neutral"])
    return (
        '<span class="pdfb-tag" '
        f'style="background:{background};color:{foreground};border-color:{border};">'
        f"{html.escape(text)}</span>"
    )


def state_badge(state: str) -> str:
    return badge(state.replace("_", " ").title(), DOCUMENT_STATE_TONES.get(state, "neutral"))


def operation_badge(operation: dict[str, Any]) -> str:
    state = str(operation.get("state", "UNKNOWN"))
    operation_type = str(operation.get("operation_type", "WORK"))
    return badge(
        f"{operation_type.title()} · {state.title()}",
        OPERATION_STATE_TONES.get(state, "neutral"),
    )


def render_problem(error: BridgeProblem) -> None:
    """Render the one v2 error envelope without exposing protected detail."""

    st.error(error.message)
    metadata = f"`{error.code}` · HTTP {error.status}"
    if error.retryable:
        metadata += " · retryable"
    if error.request_id:
        metadata += f" · request `{error.request_id}`"
    st.caption(metadata)


def render_unreachable(error: BridgeUnreachable) -> None:
    st.error(f"PDF Bridge is unreachable or returned an invalid response. {error}")
    st.caption("Check the service URL and API v2 readiness, then retry.")


def guarded(operation: Callable[[], T]) -> T | None:
    """Run one API call and render an operator-safe failure."""

    try:
        return operation()
    except BridgeProblem as error:
        render_problem(error)
    except BridgeUnreachable as error:
        render_unreachable(error)
    return None


def fmt_bytes(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    raise AssertionError("unreachable byte unit")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fmt_dt(value: str | None) -> str:
    parsed = parse_dt(value)
    if parsed is None:
        return "—"
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def fmt_relative(value: str | None) -> str:
    parsed = parse_dt(value)
    if parsed is None:
        return "—"
    seconds = max(int((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 604800:
        return f"{seconds // 86400}d ago"
    return fmt_dt(value)


def collection_options(payload: dict[str, Any]) -> dict[str, str]:
    return {
        str(item["key"]): str(item.get("display_name") or item["key"])
        for item in payload.get("items", [])
        if item.get("enabled", True)
    }


def idempotency_key(namespace: str, *parts: object) -> str:
    """Keep one key stable across a network retry of the same logical action."""

    keys: dict[str, str] = st.session_state.setdefault("idempotency_keys", {})
    material = "::".join([namespace, *(str(part) for part in parts)])
    return keys.setdefault(material, new_idempotency_key())


def clear_idempotency_key(namespace: str, *parts: object) -> None:
    keys: dict[str, str] = st.session_state.setdefault("idempotency_keys", {})
    material = "::".join([namespace, *(str(part) for part in parts)])
    keys.pop(material, None)


def cursor_for(key: str, scope: tuple[Hashable, ...]) -> str | None:
    """Return the active opaque cursor and reset it when filters change."""

    state_key = f"cursor::{key}"
    state = st.session_state.get(state_key)
    if not isinstance(state, dict) or state.get("scope") != scope:
        state = {"scope": scope, "stack": [None]}
        st.session_state[state_key] = state
    stack = state.get("stack")
    if not isinstance(stack, list) or not stack:
        raise RuntimeError(f"cursor state for {key!r} is corrupt")
    cursor = stack[-1]
    if cursor is not None and not isinstance(cursor, str):
        raise RuntimeError(f"cursor state for {key!r} contains a non-string cursor")
    return cursor


def render_cursor_controls(key: str, payload: dict[str, Any]) -> None:
    """Render Back/Next controls backed by opaque cursor history."""

    state_key = f"cursor::{key}"
    state = st.session_state.get(state_key)
    if not isinstance(state, dict) or not isinstance(state.get("stack"), list):
        raise RuntimeError(f"cursor controls for {key!r} were used before cursor_for")
    stack: list[str | None] = state["stack"]
    previous, position, following = st.columns([1, 2, 1])
    if previous.button(
        "Previous",
        key=f"cursor-prev::{key}",
        disabled=len(stack) == 1,
        use_container_width=True,
    ):
        stack.pop()
        st.rerun()
    position.caption(
        f"Page {len(stack)} · up to {payload.get('limit', len(payload.get('items', [])))} rows"
    )
    next_cursor = payload.get("next_cursor")
    if following.button(
        "Next",
        key=f"cursor-next::{key}",
        disabled=not payload.get("has_more") or not isinstance(next_cursor, str),
        use_container_width=True,
    ):
        stack.append(next_cursor)
        st.rerun()


def operation_progress(operation: dict[str, Any]) -> float:
    operation_type = str(operation.get("operation_type", ""))
    phase = str(operation.get("phase", "QUEUED"))
    sequence = _PHASE_SEQUENCES.get(operation_type, ("QUEUED", "COMPLETE"))
    try:
        index = sequence.index(phase)
    except ValueError:
        return 0.08
    return max(0.03, min((index + 1) / len(sequence), 1.0))


def render_operation(operation: dict[str, Any], *, show_timing: bool = False) -> None:
    """Render one durable operation consistently across polling screens."""

    columns = st.columns([2, 2, 1, 1])
    columns[0].markdown(operation_badge(operation), unsafe_allow_html=True)
    columns[1].caption(str(operation.get("phase", "QUEUED")).replace("_", " ").title())
    priority = str(operation.get("priority", "NORMAL")).title()
    columns[2].caption(f"{priority} priority")
    columns[3].caption(f"Attempt {operation.get('attempt', 1)}")
    if operation.get("state") in {"QUEUED", "RUNNING"}:
        st.progress(
            operation_progress(operation),
            text=str(operation.get("phase", "QUEUED")).replace("_", " ").title(),
        )
    failure = operation.get("failure")
    if isinstance(failure, dict):
        st.error(str(failure.get("message", "The operation failed.")))
        st.caption(
            f"`{failure.get('code', 'operation_failed')}`"
            + (" · retryable" if failure.get("retryable") else "")
        )
    if show_timing:
        timing = []
        if operation.get("queue_position") is not None:
            timing.append(f"queue position {operation['queue_position']}")
        if operation.get("queue_age_seconds") is not None:
            timing.append(f"queued {float(operation['queue_age_seconds']):.0f}s")
        if operation.get("phase_age_seconds") is not None:
            timing.append(f"phase age {float(operation['phase_age_seconds']):.0f}s")
        if timing:
            st.caption(" · ".join(timing))


def render_name_matches(matches: list[dict[str, Any]]) -> None:
    if not matches:
        st.caption("No exact-name or filename-family matches in this collection.")
        return
    st.warning("Filename advisory: review these existing documents before upload.")
    for match in matches:
        similarity = match.get("similarity")
        suffix = f" · {float(similarity):.0%} similar" if similarity is not None else ""
        st.markdown(
            f"- **{match.get('original_filename', 'Unknown')}** · "
            f"{str(match.get('kind', 'MATCH')).replace('_', ' ').title()}"
            f" · {match.get('state', 'UNKNOWN')}{suffix}"
        )
