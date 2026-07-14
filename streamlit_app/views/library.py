"""Current catalog, document artifact inspection, deletion, and terminal history."""

from __future__ import annotations

import base64
import html
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

import bridge_ui as ui

_CURRENT_STATES = (
    "PREFLIGHTING",
    "PREFLIGHT_FAILED",
    "REVIEW_REQUIRED",
    "PUBLISHING",
    "PUBLISH_FAILED",
    "READY",
    "DELETING",
    "DELETE_FAILED",
)


def _selected_document_id() -> str | None:
    value = st.query_params.get("document")
    return str(value) if value else None


def _select_document(document_id: str | None) -> None:
    if document_id is None:
        st.query_params.pop("document", None)
    else:
        st.query_params["document"] = document_id
    st.rerun()


def _collections(client) -> dict[str, str] | None:
    payload = ui.guarded(lambda: client.collections(limit=100))
    if payload is None:
        return None
    return ui.collection_options(payload)


def _render_catalog(client, collections: dict[str, str]) -> None:
    filters = st.columns([3, 3])
    collection_key = filters[0].selectbox(
        "Collection",
        options=list(collections),
        format_func=collections.get,
        key="library-collection",
    )
    states = {"": "All current states"} | {
        state: state.replace("_", " ").title() for state in _CURRENT_STATES
    }
    state = filters[1].selectbox(
        "State",
        options=list(states),
        format_func=states.get,
        key="library-state",
    )
    scope = (collection_key, state)
    cursor = ui.cursor_for("library-documents", scope)
    payload = ui.guarded(
        lambda: client.documents(
            collection_key,
            state=state or None,
            cursor=cursor,
            limit=25,
        )
    )
    if payload is None:
        return
    items = payload.get("items", [])
    if not items:
        st.info("No current documents match these filters.")
        return
    selected = _selected_document_id()
    for document in items:
        document_id = str(document["id"])
        with st.container(border=True):
            heading, state_column, action = st.columns([5, 2, 2])
            heading.markdown(f"**{document['original_filename']}**")
            heading.caption(
                f"{ui.fmt_bytes(document.get('size_bytes'))} · "
                f"created {ui.fmt_dt(document.get('created_at'))} · `{document_id}`"
            )
            state_column.markdown(ui.state_badge(document["state"]), unsafe_allow_html=True)
            if action.button(
                "Selected" if selected == document_id else "Inspect",
                key=f"library-open::{document_id}",
                disabled=selected == document_id,
                use_container_width=True,
            ):
                _select_document(document_id)
            failure = document.get("failure")
            if failure:
                st.caption(f"`{failure.get('code', 'failure')}` · {failure.get('message', '')}")
    ui.render_cursor_controls("library-documents", payload)


def _load_source(client, document_id: str) -> tuple[bytes, str, str] | None:
    cache: dict[str, tuple[bytes, str, str]] = st.session_state.setdefault("source_cache", {})
    if document_id not in cache:
        result = ui.guarded(lambda: client.source(document_id))
        if result is None:
            return None
        cache.clear()
        cache[document_id] = result
    return cache[document_id]


def _render_source(client, document: dict[str, Any]) -> None:
    source = document.get("source", {})
    facts = st.columns(4)
    facts[0].metric("MIME", source.get("content_type", "—"))
    facts[1].metric("Size", ui.fmt_bytes(source.get("size_bytes")))
    facts[2].metric("Scan", source.get("scan_state", "—"))
    facts[3].metric("Available", "Yes" if source.get("available") else "No")
    st.markdown(
        '<div class="pdfb-kv">Source SHA-256</div>'
        f'<div class="pdfb-mono">{html.escape(str(source.get("sha256", "—")))}</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"Admitted by `{source.get('created_by', '—')}` at {ui.fmt_dt(source.get('created_at'))} · "
        f"scanner {source.get('scan_engine') or '—'} at {ui.fmt_dt(source.get('scanned_at'))}"
    )
    if not source.get("available"):
        st.warning("Source reads are blocked for this lifecycle state.")
        return

    document_id = str(document["id"])
    if st.button("Load source PDF", icon=":material/picture_as_pdf:"):
        _load_source(client, document_id)
    cached: dict[str, tuple[bytes, str, str]] = st.session_state.get("source_cache", {})
    loaded = cached.get(document_id)
    if loaded is None:
        return
    content, filename, content_type = loaded
    st.download_button(
        "Download original PDF",
        data=content,
        file_name=filename,
        mime=content_type,
        icon=":material/download:",
    )
    if st.toggle("Preview PDF in browser", value=False, key=f"preview-source::{document_id}"):
        encoded = base64.b64encode(content).decode("ascii")
        components.html(
            '<iframe title="Source PDF preview" '
            f'src="data:application/pdf;base64,{encoded}" '
            'style="width:100%;height:720px;border:1px solid #dfe3e7;border-radius:6px"></iframe>',
            height=740,
            scrolling=False,
        )


def _render_markdown(client, document: dict[str, Any]) -> None:
    document_id = str(document["id"])
    payload = ui.guarded(lambda: client.markdown(document_id))
    if payload is None:
        return
    st.markdown(
        '<div class="pdfb-kv">Canonical Markdown SHA-256</div>'
        f'<div class="pdfb-mono">{html.escape(str(payload.get("markdown_sha256", "—")))}</div>',
        unsafe_allow_html=True,
    )
    st.caption(f"Prepared revision `{payload.get('prepared_revision_id', '—')}`")
    pages = payload.get("pages", [])
    if not pages:
        st.info("No Markdown pages were returned.")
        return
    selected_page = st.selectbox(
        "Page",
        options=range(len(pages)),
        format_func=lambda index: f"Page {pages[index].get('page_number', index + 1)}",
        key=f"markdown-page::{document_id}",
    )
    page = pages[selected_page]
    st.caption(
        f"{page.get('slice_count', 0)} formatter slice(s) · "
        f"Markdown `{page.get('markdown_sha256', '—')}` · "
        f"source projection `{page.get('source_projection_sha256', '—')}`"
    )
    markdown = str(page.get("markdown", ""))
    view = st.radio(
        "Markdown view",
        options=("Rendered", "Raw"),
        horizontal=True,
        key=f"markdown-view::{document_id}::{page.get('page_number', selected_page + 1)}",
    )
    if view == "Rendered":
        with st.container(border=True):
            st.markdown(markdown)
    else:
        st.code(markdown, language="markdown", wrap_lines=True)


def _render_chunks(client, document: dict[str, Any]) -> None:
    document_id = str(document["id"])
    revision = document.get("prepared_revision") or {}
    revision_id = str(revision.get("id", ""))
    cursor = ui.cursor_for("document-chunks", (document_id, revision_id))
    payload = ui.guarded(lambda: client.chunks(document_id, cursor=cursor, limit=20))
    if payload is None:
        return
    st.caption(
        f"Prepared revision `{payload.get('prepared_revision_id', '—')}` · "
        "numeric vectors are never exposed"
    )
    items = payload.get("items", [])
    if not items:
        st.info("No public chunks were returned.")
        return
    for chunk in items:
        headings = " / ".join(chunk.get("heading_path", [])) or "No heading"
        title = (
            f"Chunk {chunk.get('chunk_index', '?')} · pages "
            f"{chunk.get('page_start')}–{chunk.get('page_end')} · {headings}"
        )
        with st.expander(title):
            st.caption(
                f"Chunk `{chunk.get('id', '—')}` · {chunk.get('token_count', 0)} tokens · "
                f"text `{chunk.get('text_sha256', '—')}`"
            )
            st.markdown(str(chunk.get("markdown", "")))
    ui.render_cursor_controls("document-chunks", payload)


def _render_lifecycle(client, document: dict[str, Any]) -> None:
    operation = document.get("current_operation")
    if operation:
        detail = ui.guarded(lambda: client.operation(str(operation["id"])))
        st.markdown("#### Current operation")
        ui.render_operation(detail or operation, show_timing=detail is not None)

    revision = document.get("prepared_revision")
    if revision:
        st.markdown("#### Prepared revision")
        metrics = st.columns(4)
        metrics[0].metric("Revision", revision.get("revision_number", "—"))
        metrics[1].metric("Status", revision.get("status", "—"))
        metrics[2].metric("Pages", revision.get("page_count", "—"))
        metrics[3].metric("Chunks", revision.get("chunk_count", "—"))
        st.caption(
            f"content `{revision.get('content_profile_id', '—')}` · "
            f"index `{revision.get('index_profile_id', '—')}` · "
            f"policy `{revision.get('preflight_policy_id', '—')}`"
        )
        st.caption(
            f"formatter `{revision.get('formatter_model_id', '—')}` · "
            f"dense `{revision.get('dense_model_id', '—')}` / "
            f"{revision.get('dense_dimension', '—')} · "
            f"sparse `{revision.get('sparse_model_id', '—')}`"
        )

    publication = document.get("publication")
    if publication:
        st.markdown("#### Publication verification")
        metrics = st.columns(4)
        metrics[0].metric("Status", publication.get("status", "—"))
        metrics[1].metric("Expected points", publication.get("expected_points", "—"))
        metrics[2].metric("Verified points", publication.get("verified_points", "—"))
        metrics[3].metric("Verified", ui.fmt_dt(publication.get("verified_at")))
        verification_labels = (
            "verified" if publication.get("payload_revision_verified") else "not verified",
            "verified" if publication.get("vector_schema_verified") else "not verified",
            "verified" if publication.get("screening_zero_verified") else "not verified",
        )
        st.caption(
            f"payload revision {verification_labels[0]} · "
            f"vector schema {verification_labels[1]} · screening zero {verification_labels[2]}"
        )

    decision = document.get("decision")
    if decision:
        st.markdown("#### Immutable decision")
        st.markdown(
            ui.badge(str(decision.get("action", "UNKNOWN")).title(), "info"),
            unsafe_allow_html=True,
        )
        st.caption(
            f"Actor `{decision.get('actor_id', '—')}` · "
            f"{ui.fmt_dt(decision.get('created_at'))}"
        )
        st.caption(
            f"Revision `{decision.get('prepared_revision_id', '—')}` · "
            f"manifest `{decision.get('prepared_manifest_sha256', '—')}`"
        )

    replacement = document.get("replacement")
    if replacement:
        st.markdown("#### Replacement linkage")
        st.write(
            f"Old `{replacement.get('old_document_id')}` "
            f"({replacement.get('old_document_state')}) → "
            f"new `{replacement.get('new_document_id')}` ({replacement.get('new_document_state')})"
        )

    deletion = document.get("deletion")
    if deletion:
        st.markdown("#### Verified deletion")
        st.markdown(
            ui.badge(str(deletion.get("phase", "UNKNOWN")).replace("_", " ").title(), "info"),
            unsafe_allow_html=True,
        )
        st.caption(
            f"active zero {ui.fmt_dt(deletion.get('active_zero_verified_at'))} · "
            f"screening zero {ui.fmt_dt(deletion.get('screening_zero_verified_at'))} · "
            f"storage purged {ui.fmt_dt(deletion.get('storage_purged_at'))}"
        )


def _render_events(client, document: dict[str, Any]) -> None:
    document_id = str(document["id"])
    cursor = ui.cursor_for("document-events", (document_id,))
    payload = ui.guarded(lambda: client.events(document_id, cursor=cursor, limit=25))
    if payload is None:
        return
    items = payload.get("items", [])
    if not items:
        st.info("No audit events were returned.")
        return
    for event in items:
        with st.container(border=True):
            heading, time = st.columns([4, 2])
            heading.markdown(
                f"**{str(event.get('event_type', 'EVENT')).replace('_', ' ').title()}**"
            )
            time.caption(ui.fmt_dt(event.get("occurred_at")))
            st.caption(
                f"{event.get('actor_type', '—')} · `{event.get('actor_id', '—')}`"
                + (f" · operation `{event['operation_id']}`" if event.get("operation_id") else "")
            )
            if event.get("attributes"):
                st.json(event["attributes"], expanded=False)
    ui.render_cursor_controls("document-events", payload)


def _retry(client, document: dict[str, Any]) -> None:
    operation = document.get("current_operation") or {}
    document_id = str(document["id"])
    result = ui.guarded(
        lambda: client.retry(
            document_id,
            idempotency_key=ui.idempotency_key(
                "retry", document_id, operation.get("id"), operation.get("attempt")
            ),
        )
    )
    if result is not None:
        st.toast("Retry accepted", icon=":material/replay:")
        st.rerun()


def _delete(client, document: dict[str, Any]) -> None:
    document_id = str(document["id"])
    result = ui.guarded(
        lambda: client.delete(
            document_id,
            idempotency_key=ui.idempotency_key("delete", document_id),
        )
    )
    if result is not None:
        st.toast("High-priority deletion accepted", icon=":material/delete_sweep:")
        st.rerun()


def _render_actions(client, document: dict[str, Any]) -> None:
    actions = set(document.get("allowed_actions", []))
    retry, deletion, spacer = st.columns([2, 2, 5])
    if "RETRY" in actions and retry.button(
        "Retry failed phase",
        icon=":material/replay:",
        use_container_width=True,
    ):
        _retry(client, document)
    if "DELETE" in actions:
        with deletion.popover(
            "Delete document",
            icon=":material/delete_sweep:",
            use_container_width=True,
        ):
            st.warning(
                "This immediately blocks reads and queues HIGH-priority verified "
                "point and storage deletion."
            )
            if st.button("Confirm high-priority deletion", type="primary"):
                _delete(client, document)
    spacer.empty()


def _render_document(client, document_id: str) -> dict[str, Any] | None:
    document = ui.guarded(lambda: client.document(document_id))
    if document is None:
        return None
    heading, status = st.columns([5, 2])
    heading.markdown(f"### {document['original_filename']}")
    heading.caption(
        f"{document['collection_key']} · {ui.fmt_bytes(document.get('size_bytes'))} · "
        f"`{document_id}`"
    )
    status.markdown(ui.state_badge(document["state"]), unsafe_allow_html=True)
    _render_actions(client, document)

    inspector = st.radio(
        "Document inspector",
        options=("Source", "Markdown", "Chunks", "Lifecycle", "Events"),
        horizontal=True,
        label_visibility="collapsed",
        key=f"document-inspector::{document_id}",
    )
    st.markdown('<div class="pdfb-rule"></div>', unsafe_allow_html=True)
    if inspector == "Source":
        _render_source(client, document)
    elif inspector == "Markdown":
        _render_markdown(client, document)
    elif inspector == "Chunks":
        _render_chunks(client, document)
    elif inspector == "Lifecycle":
        _render_lifecycle(client, document)
    else:
        _render_events(client, document)
    return document


def _render_history(client, collections: dict[str, str]) -> None:
    filters = st.columns([3, 3])
    collection_options = {"": "All collections"} | collections
    collection_key = filters[0].selectbox(
        "Collection",
        options=list(collection_options),
        format_func=collection_options.get,
        key="history-collection",
    )
    dispositions = {
        "": "All terminal dispositions",
        "CANCELLED": "Cancelled",
        "REJECTED": "Rejected",
        "DELETED": "Deleted",
    }
    disposition = filters[1].selectbox(
        "Disposition",
        options=list(dispositions),
        format_func=dispositions.get,
        key="history-disposition",
    )
    scope = (collection_key, disposition)
    cursor = ui.cursor_for("terminal-history", scope)
    payload = ui.guarded(
        lambda: client.history(
            collection_key=collection_key or None,
            disposition=disposition or None,
            cursor=cursor,
            limit=25,
        )
    )
    if payload is None:
        return
    items = payload.get("items", [])
    if not items:
        st.info("No terminal tombstones match these filters.")
        return
    for tombstone in items:
        with st.container(border=True):
            heading, disposition_column = st.columns([5, 2])
            heading.markdown(f"**Document `{tombstone.get('document_id', '—')}`**")
            heading.caption(
                f"{tombstone.get('collection_key', '—')} · "
                f"{ui.fmt_dt(tombstone.get('occurred_at'))} · "
                f"actor `{tombstone.get('actor_id', '—')}`"
            )
            disposition_column.markdown(
                ui.badge(str(tombstone.get("disposition", "UNKNOWN")).title(), "neutral"),
                unsafe_allow_html=True,
            )
            st.caption(
                f"source `{tombstone.get('source_sha256', '—')}` · "
                f"manifest `{tombstone.get('manifest_sha256') or '—'}` · "
                f"reason `{tombstone.get('reason_code') or '—'}`"
            )
    ui.render_cursor_controls("terminal-history", payload)


def render() -> None:
    ui.apply_chrome(
        "Library",
        "Inspect current source and prepared artifacts, or audit content-free terminal history.",
    )
    client = ui.get_client()
    collections = _collections(client)
    if not collections:
        return
    mode = st.radio(
        "Library mode",
        options=("Current catalog", "Terminal history"),
        horizontal=True,
        label_visibility="collapsed",
    )
    if mode == "Terminal history":
        st.query_params.pop("document", None)
        _render_history(client, collections)
        return

    _render_catalog(client, collections)
    selected = _selected_document_id()
    if selected is None:
        st.info(
            "Select a current document to inspect its source, Markdown, chunks, "
            "lifecycle, and events."
        )
        return
    st.divider()
    initial = ui.guarded(lambda: client.document(selected))
    if initial is None:
        return
    refresh = "2s" if initial.get("state") in ui.WORKING_DOCUMENT_STATES else None
    if refresh:
        st.caption("Polling every two seconds while durable work is open.")
    elif st.button("Refresh document", icon=":material/refresh:"):
        st.rerun()

    @st.fragment(run_every=refresh)
    def document_fragment() -> None:
        _render_document(client, selected)

    document_fragment()


render()
