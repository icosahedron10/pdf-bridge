"""Overview page: dependency health, collection counts, recent intake."""

from __future__ import annotations

import streamlit as st

import bridge_ui as ui

_CHECK_LABELS = {
    "process": "Application process",
    "database": "SQLite catalog",
    "scanner": "ClamAV scanner",
    "storage": "Canonical storage",
    "search": "Retrieval service",
    "qdrant": "Qdrant indexes",
    "embedding": "Embedding provider",
    "llm": "LLM provider",
}


def _render_health(client) -> None:
    st.subheader("Service health")
    health = ui.guarded(lambda: client.health("dependencies"))
    if health is None:
        return
    status = health.get("status", "unknown")
    if status == "ok":
        st.markdown(ui.badge("All dependencies healthy", "ok"), unsafe_allow_html=True)
    else:
        st.markdown(
            ui.badge("Degraded — some dependencies are unavailable", "danger"),
            unsafe_allow_html=True,
        )
    checks = health.get("checks", {})
    if checks:
        columns = st.columns(min(4, max(1, len(checks))))
        for index, (name, value) in enumerate(sorted(checks.items())):
            tone = "ok" if value == "ok" else "danger"
            with columns[index % len(columns)]:
                st.markdown(
                    f'<div class="pdfb-kv">{_CHECK_LABELS.get(name, name)}</div>'
                    + ui.badge(value, tone),
                    unsafe_allow_html=True,
                )
    if status != "ok":
        st.caption(
            "A degraded scanner blocks new uploads; degraded providers pause "
            "analysis. Existing published content stays retrievable."
        )


def _render_collections(client) -> None:
    st.subheader("Collections")
    payload = ui.guarded(client.collections)
    if payload is None:
        return
    items = payload.get("items", [])
    if not items:
        st.info("No collections are configured for this deployment.")
        return
    total_available = sum(item["available_documents"] for item in items)
    total_processing = sum(item["processing_documents"] for item in items)
    metric_columns = st.columns(3)
    metric_columns[0].metric("Collections", len(items))
    metric_columns[1].metric("Published documents", total_available)
    metric_columns[2].metric("In processing", total_processing)
    st.dataframe(
        [
            {
                "Collection": item["display_name"],
                "Key": item["key"],
                "Audience": item["audience"],
                "Published": item["available_documents"],
                "Processing": item["processing_documents"],
                "Description": item["description"],
            }
            for item in items
        ],
        width="stretch",
        hide_index=True,
    )


def _render_recent_uploads(client) -> None:
    st.subheader("Recent intake")
    payload = ui.guarded(lambda: client.uploads(page=1, page_size=8))
    if payload is None:
        return
    items = payload.get("items", [])
    if not items:
        st.info("No uploads yet. Start with the Upload page.")
        return
    for item in items:
        document = item["document"]
        columns = st.columns([4, 2, 2, 2])
        columns[0].markdown(f"**{document['original_filename']}**")
        columns[1].markdown(
            ui.state_badge(document["state"]), unsafe_allow_html=True
        )
        columns[2].caption(document["collection_key"])
        columns[3].caption(ui.fmt_relative(document["uploaded_at"]))
    if st.button("Open review queue", icon=":material/fact_check:"):
        st.switch_page("views/workspace.py")


def render() -> None:
    """Render the operator overview."""

    ui.apply_chrome(
        "PDF Bridge",
        "Durable, reviewable PDF intake into collection-partitioned retrieval.",
    )
    client = ui.get_client()
    _render_health(client)
    st.divider()
    _render_collections(client)
    st.divider()
    _render_recent_uploads(client)


render()
