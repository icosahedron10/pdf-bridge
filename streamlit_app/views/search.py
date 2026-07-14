"""Optional operator-only proxy to the external active-corpus retrieval service."""

from __future__ import annotations

import html

import streamlit as st

import bridge_ui as ui
from bridge_client import BridgeProblem, BridgeUnreachable


def _open_document(document_id: str) -> None:
    st.query_params["document"] = document_id
    st.switch_page("views/library.py")


def _run_search(client, **kwargs):
    try:
        return client.search(**kwargs)
    except BridgeProblem as error:
        if error.status == 503:
            st.warning("Operator search is not configured or its retrieval service is unavailable.")
            st.caption(
                f"`{error.code}`" + (f" · request `{error.request_id}`" if error.request_id else "")
            )
            return None
        ui.render_problem(error)
    except BridgeUnreachable as error:
        ui.render_unreachable(error)
    return None


def render() -> None:
    ui.apply_chrome(
        "Search",
        "Run an optional operator diagnostic against one configured active collection.",
    )
    client = ui.get_client()
    collections = ui.guarded(lambda: client.collections(limit=100))
    if collections is None:
        return
    options = ui.collection_options(collections)
    if not options:
        st.info("No enabled collections are configured.")
        return

    with st.form("operator-search"):
        collection_key = st.selectbox(
            "Collection",
            options=list(options),
            format_func=options.get,
        )
        query = st.text_input(
            "Query",
            max_chars=1000,
            placeholder="Enter a precise retrieval diagnostic",
        )
        mode_column, limit_column = st.columns(2)
        mode = mode_column.selectbox(
            "Mode",
            options=("hybrid", "semantic", "keyword"),
            format_func=str.title,
        )
        limit = limit_column.number_input(
            "Result limit",
            min_value=1,
            max_value=100,
            value=20,
        )
        submitted = st.form_submit_button(
            "Run operator search",
            type="primary",
            icon=":material/search:",
        )

    st.caption(
        "Bridge validates every hit against the requested collection and READY state. "
        "It never exposes the upstream credential."
    )
    if not submitted:
        return
    if not query.strip():
        st.warning("Enter a non-blank query.")
        return

    payload = _run_search(
        client,
        collection_key=collection_key,
        query=query,
        mode=mode,
        limit=int(limit),
    )
    if payload is None:
        return
    results = payload.get("results", [])
    st.subheader(f"Results · {len(results)}")
    if not results:
        st.info("No READY document chunks matched this query.")
        return

    for hit in results:
        document_id = str(hit["document_id"])
        with st.container(border=True):
            heading, score, action = st.columns([5, 1, 2])
            heading.markdown(
                f"**#{hit.get('rank', '?')} · {hit.get('original_filename', 'Unknown')}**"
            )
            location = " / ".join(hit.get("heading_path", []))
            if hit.get("page_start") is not None:
                location = f"pages {hit['page_start']}–{hit['page_end']}" + (
                    f" · {location}" if location else ""
                )
            heading.caption(location or "Document-level result")
            score.metric("Score", f"{float(hit.get('score', 0)):.3f}")
            if action.button(
                "Inspect document",
                key=f"search-open::{document_id}::{hit.get('rank')}",
                use_container_width=True,
            ):
                _open_document(document_id)
            st.markdown(
                f'<div class="pdfb-excerpt">{html.escape(str(hit.get("excerpt", "")))}</div>',
                unsafe_allow_html=True,
            )
            st.caption(
                f"Document `{document_id}` · revision `{hit.get('prepared_revision_id', '—')}`"
                + (f" · chunk `{hit['chunk_id']}`" if hit.get("chunk_id") else "")
            )


render()
