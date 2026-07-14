"""Library page: catalog browsing, document detail, download, and deletion."""

from __future__ import annotations

from typing import Any

import streamlit as st

import bridge_ui as ui

_DOCUMENT_STATES = (
    "ANALYZING",
    "REVIEW_REQUIRED",
    "INGESTING",
    "INGEST_FAILED",
    "INGESTED",
    "REPLACING",
    "REPLACE_FAILED",
    "DELETING",
    "DELETE_FAILED",
    "CLEANUP_PENDING",
    "CLEANUP_FAILED",
    "REJECTED",
    "CANCELLED",
    "DELETED",
)

# Retained states can be downloaded; deletion applies to published content.
_DOWNLOADABLE_STATES = {
    "REVIEW_REQUIRED",
    "INGESTING",
    "INGEST_FAILED",
    "INGESTED",
    "REPLACING",
    "REPLACE_FAILED",
}


def _selected_document_id() -> str | None:
    return st.query_params.get("doc")


def _select_document(document_id: str | None) -> None:
    if document_id is None:
        st.query_params.pop("doc", None)
    else:
        st.query_params["doc"] = document_id
    st.rerun()


def _render_catalog(client) -> None:
    filter_columns = st.columns([3, 2, 2, 1])
    collections_payload = ui.guarded(client.collections) or {"items": []}
    options = {"": "All collections"} | ui.collection_options(collections_payload)
    collection_key = filter_columns[0].selectbox(
        "Collection", options=list(options), format_func=options.get, key="lib_collection"
    )
    scope = filter_columns[1].selectbox(
        "Scope",
        options=("all", "library", "queue"),
        format_func={
            "all": "All documents",
            "library": "Published library",
            "queue": "Intake queue",
        }.get,
        key="lib_scope",
    )
    state = filter_columns[2].selectbox(
        "State", options=("", *_DOCUMENT_STATES), key="lib_state",
        format_func=lambda value: value.replace("_", " ").title() if value else "Any state",
    )
    page = filter_columns[3].number_input("Page", min_value=1, value=1, key="lib_page")

    payload = ui.guarded(
        lambda: client.documents(
            scope=scope,
            state=state or None,
            collection_key=collection_key or None,
            page=int(page),
            page_size=25,
        )
    )
    if payload is None:
        return
    items = payload.get("items", [])
    st.caption(
        f"{payload.get('total', 0)} document{'s' if payload.get('total') != 1 else ''} · "
        f"page {payload.get('page', 1)} of {max(payload.get('pages', 1), 1)}"
    )
    if not items:
        st.info("No documents match these filters.")
        return
    selected = _selected_document_id()
    for item in items:
        document_id = str(item["id"])
        with st.container(border=True):
            columns = st.columns([5, 2, 2, 2, 2])
            columns[0].markdown(f"**{item['original_filename']}**")
            columns[0].caption(
                f"{item['collection_key']} · {ui.fmt_bytes(item['size_bytes'])}"
                f" · uploaded {ui.fmt_relative(item['uploaded_at'])}"
            )
            columns[1].markdown(ui.state_badge(item["state"]), unsafe_allow_html=True)
            columns[2].markdown(
                ui.badge(
                    f"scan {item['scan_state']}",
                    ui.SCAN_STATE_TONES.get(item["scan_state"], "neutral"),
                ),
                unsafe_allow_html=True,
            )
            if item.get("ingested_at"):
                columns[3].caption(f"published {ui.fmt_relative(item['ingested_at'])}")
            if columns[4].button(
                "Open" if document_id != selected else "Selected",
                key=f"doc-{document_id}",
                disabled=document_id == selected,
            ):
                _select_document(document_id)


def _render_audit_ledger(events: list[dict[str, Any]]) -> None:
    if not events:
        st.caption("No audit events recorded.")
        return
    st.dataframe(
        [
            {
                "When": ui.fmt_dt(event["occurred_at"]),
                "Event": event["event_type"],
                "Actor": f"{event['actor_type']}:{event['actor_id']}",
                "Details": ", ".join(
                    f"{key}={value}" for key, value in (event.get("details") or {}).items()
                ),
            }
            for event in events
        ],
        width="stretch",
        hide_index=True,
    )


def _render_document_detail(client, document_id: str) -> None:
    detail = ui.guarded(lambda: client.document(document_id))
    if detail is None:
        return
    st.markdown(f"### {detail['original_filename']}")
    st.markdown(
        ui.state_badge(detail["state"])
        + " "
        + ui.badge(detail["collection_key"], "info")
        + " "
        + ui.badge(
            f"scan {detail['scan_state']}",
            ui.SCAN_STATE_TONES.get(detail["scan_state"], "neutral"),
        ),
        unsafe_allow_html=True,
    )
    metric_columns = st.columns(4)
    metric_columns[0].metric("Size", ui.fmt_bytes(detail["size_bytes"]))
    metric_columns[1].metric("Pages", detail.get("page_count") or "—")
    metric_columns[2].metric("Chunks", detail.get("chunk_count") or "—")
    metric_columns[3].metric("Analysis revision", detail.get("analysis_revision", 0))

    st.markdown(
        f'<div class="pdfb-kv">SHA-256</div>'
        f'<div class="pdfb-mono">{detail["sha256"]}</div>',
        unsafe_allow_html=True,
    )
    detail_columns = st.columns(2)
    detail_columns[0].caption(
        f"Uploaded {ui.fmt_dt(detail['uploaded_at'])} by `{detail['uploader_identity']}`"
    )
    if detail.get("scanned_at"):
        detail_columns[1].caption(
            f"Scanned {ui.fmt_dt(detail['scanned_at'])}"
            + (f" · {detail['scan_engine']}" if detail.get("scan_engine") else "")
        )
    if detail.get("rejection_reason"):
        st.error(f"Rejected: {detail['rejection_reason']}")
    if detail.get("last_error"):
        st.error(f"Last error: {detail['last_error']}")
    if detail.get("replaced_by_document_id"):
        st.info("This document was replaced.")
        if st.button("Open replacement", key="open-replacement"):
            _select_document(str(detail["replaced_by_document_id"]))

    action_columns = st.columns([2, 2, 5])
    if detail["state"] in _DOWNLOADABLE_STATES:
        if action_columns[0].button("Fetch PDF", icon=":material/download:"):
            fetched = ui.guarded(lambda: client.document_content(document_id))
            if fetched is not None:
                st.session_state["fetched_pdf"] = (document_id, *fetched)
        cached = st.session_state.get("fetched_pdf")
        if cached and cached[0] == document_id:
            _, content, filename = cached
            action_columns[1].download_button(
                "Save file", data=content, file_name=filename, mime="application/pdf"
            )
            if hasattr(st, "pdf"):
                with st.expander("Preview", expanded=False):
                    st.pdf(content)

    if detail["state"] == "INGESTED":
        with action_columns[2].popover("Request deletion…", icon=":material/delete:"):
            st.warning(
                "Deletion removes the published index points, canonical bytes, "
                "and analysis artifacts, keeping only audit metadata."
            )
            reason = st.text_input("Reason (optional)", max_chars=500, key="del-reason")
            if st.button("Queue verified deletion", key="del-confirm"):
                result = ui.guarded(
                    lambda: client.request_deletion(document_id, reason=reason or None)
                )
                if result is not None:
                    st.toast("Deletion queued", icon=":material/delete:")
                    st.rerun()

    tabs = st.tabs(["Audit ledger", "Operations", "Decisions", "Analysis"])
    with tabs[0]:
        _render_audit_ledger(detail.get("audit_events", []))
    with tabs[1]:
        operations = detail.get("operations", [])
        if operations:
            st.dataframe(
                [
                    {
                        "Type": op["operation_type"],
                        "State": op["state"],
                        "Phase": op["phase"],
                        "Attempt": op["attempt"],
                        "Created": ui.fmt_dt(op["created_at"]),
                        "Completed": ui.fmt_dt(op.get("completed_at")),
                        "Error": op.get("error") or "",
                    }
                    for op in operations
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No operations recorded.")
    with tabs[2]:
        decisions = detail.get("decisions", [])
        if decisions:
            st.dataframe(
                [
                    {
                        "Action": decision["action"],
                        "Revision": decision["analysis_revision"],
                        "Actor": decision["actor_id"],
                        "Override": decision["advisory_override"],
                        "At": ui.fmt_dt(decision["created_at"]),
                    }
                    for decision in decisions
                ],
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No operator decisions recorded.")
    with tabs[3]:
        analysis = detail.get("analysis")
        if analysis:
            st.markdown(
                ui.badge(
                    f"revision {analysis['revision']} · {analysis['status']}",
                    "ok" if analysis["status"] == "COMPLETE" else "info",
                )
                + " "
                + ui.badge(
                    f"{analysis['candidate_count']} candidate(s)",
                    "warn" if analysis["candidate_count"] else "ok",
                ),
                unsafe_allow_html=True,
            )
            if analysis.get("incomplete_reasons"):
                st.warning("; ".join(analysis["incomplete_reasons"]))
            ui.render_filename_warnings(analysis.get("filename_warnings", []))
        else:
            st.caption("No analysis attached to this document.")


def render() -> None:
    """Render the collection library and document detail."""

    ui.apply_chrome(
        "Library",
        "Browse the collection-partitioned catalog and each document's full history.",
    )
    client = ui.get_client()
    _render_catalog(client)
    selected = _selected_document_id()
    if selected:
        st.divider()
        _render_document_detail(client, selected)


render()
