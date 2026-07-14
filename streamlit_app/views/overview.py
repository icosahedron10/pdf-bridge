"""Operational overview for readiness, collection state, and active work."""

from __future__ import annotations

from collections import Counter
from typing import Any

import streamlit as st

import bridge_ui as ui


def _render_health(client) -> None:
    st.subheader("Service readiness")
    live_column, ready_column = st.columns(2)
    live = ui.guarded(lambda: client.health("live"))
    ready = ui.guarded(lambda: client.health("ready"))

    with live_column:
        if live is not None:
            tone = "ok" if live.get("status") == "OK" else "danger"
            st.markdown(
                ui.badge(f"Process {live.get('status', 'UNKNOWN')}", tone), unsafe_allow_html=True
            )
            st.caption("Liveness confirms the service process can answer requests.")
    with ready_column:
        if ready is not None:
            tone = "ok" if ready.get("status") == "OK" else "danger"
            st.markdown(
                ui.badge(f"Dependencies {ready.get('status', 'UNKNOWN')}", tone),
                unsafe_allow_html=True,
            )
            checks = ready.get("checks", [])
            if checks:
                st.caption(
                    " · ".join(
                        f"{check.get('component', '?')}: {check.get('status', '?')}"
                        for check in checks
                    )
                )


def _state_totals(items: list[dict[str, Any]]) -> Counter[str]:
    totals: Counter[str] = Counter()
    for collection in items:
        by_state = collection.get("counts", {}).get("by_state", {})
        totals.update({str(state): int(count) for state, count in by_state.items()})
    return totals


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _render_operation_metrics(client) -> None:
    st.subheader("Durable work")
    payload = ui.guarded(client.operation_metrics)
    if payload is None:
        return
    metrics = st.columns(4)
    metrics[0].metric("Queued", payload.get("queued", 0))
    metrics[1].metric("Running", payload.get("running", 0))
    metrics[2].metric("Failed", payload.get("failed", 0))
    metrics[3].metric(
        "Oldest queued",
        _format_age(payload.get("oldest_queued_age_seconds")),
    )
    buckets = payload.get("buckets", [])
    if buckets:
        st.caption(
            "Phase buckets · "
            + " · ".join(
                f"{str(bucket.get('operation_type', 'work')).title()} / "
                f"{str(bucket.get('phase', 'unknown')).replace('_', ' ').title()}: "
                f"{bucket.get('count', 0)}"
                for bucket in buckets
            )
        )
    else:
        st.caption("No queued, running, or failed durable operations.")


def _render_collection_summary(client) -> None:
    st.subheader("Collections")
    cursor = ui.cursor_for("overview-collections", ())
    payload = ui.guarded(lambda: client.collections(cursor=cursor, limit=25))
    if payload is None:
        return
    items = payload.get("items", [])
    if not items:
        st.info("No enabled collections are configured.")
        return

    totals = _state_totals(items)
    metrics = st.columns(5)
    metrics[0].metric("Ready", totals["READY"])
    metrics[1].metric(
        "Processing",
        totals["PREFLIGHTING"] + totals["PUBLISHING"],
    )
    metrics[2].metric("Review", totals["REVIEW_REQUIRED"])
    metrics[3].metric(
        "Failed",
        totals["PREFLIGHT_FAILED"] + totals["PUBLISH_FAILED"] + totals["DELETE_FAILED"],
    )
    metrics[4].metric("Deleting", totals["DELETING"])

    for collection in items:
        counts = collection.get("counts", {})
        by_state = counts.get("by_state", {})
        with st.container(border=True):
            heading, status = st.columns([5, 2])
            heading.markdown(f"**{collection.get('display_name', collection['key'])}**")
            heading.caption(f"`{collection['key']}` · {collection.get('description', '')}")
            status.markdown(
                ui.badge(
                    "Enabled" if collection.get("enabled") else "Disabled",
                    "ok" if collection.get("enabled") else "neutral",
                ),
                unsafe_allow_html=True,
            )
            st.caption(
                f"{counts.get('total', 0)} current · "
                f"{by_state.get('READY', 0)} ready · "
                f"{by_state.get('REVIEW_REQUIRED', 0)} review · "
                f"{by_state.get('PREFLIGHTING', 0) + by_state.get('PUBLISHING', 0)} processing"
            )
    ui.render_cursor_controls("overview-collections", payload)

    options = ui.collection_options(payload)
    selected = st.selectbox(
        "Inspect collection target",
        options=list(options),
        format_func=options.get,
        key="overview-collection-target",
    )
    detail = ui.guarded(lambda: client.collection(selected))
    if detail is None:
        return
    target = detail.get("target", {})
    target_columns = st.columns([2, 2, 3])
    target_columns[0].metric("Schema", f"v{target.get('schema_version', '—')}")
    target_columns[1].metric("Compatible", "Yes" if target.get("schema_compatible") else "No")
    target_columns[2].markdown(
        '<div class="pdfb-kv">Fixed Qdrant target</div>'
        f'<div class="pdfb-mono">{target.get("qdrant_collection_name", "—")}</div>',
        unsafe_allow_html=True,
    )
    failure = target.get("failure")
    if failure:
        st.error(str(failure.get("message", "The collection target is unavailable.")))


def render() -> None:
    ui.apply_chrome(
        "Operations",
        "Readiness, lifecycle pressure, and immutable collection targets at a glance.",
    )
    client = ui.get_client()
    _render_health(client)
    st.divider()
    _render_operation_metrics(client)
    st.divider()
    _render_collection_summary(client)


render()
