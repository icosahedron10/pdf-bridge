"""Search page: operator workspace access to the external retrieval service."""

from __future__ import annotations

import streamlit as st

import bridge_ui as ui

_MODE_HELP = {
    "hybrid": "Fuses keyword and semantic rankings (recommended).",
    "keyword": "Exact-term BM25 matching.",
    "semantic": "Dense-vector similarity.",
}


def _document_label(client, document_id: str) -> str:
    """Resolve a hit's filename, caching lookups for the session."""

    cache: dict[str, str] = st.session_state.setdefault("search_doc_labels", {})
    if document_id not in cache:
        try:
            detail = client.document(document_id)
            cache[document_id] = detail.get("original_filename", document_id)
        except Exception:
            cache[document_id] = document_id
    return cache[document_id]


def render() -> None:
    """Render retrieval search over configured collections."""

    ui.apply_chrome(
        "Search",
        "Query published content through the stable external retrieval contract.",
    )
    client = ui.get_client()

    collections_payload = ui.guarded(client.collections)
    if collections_payload is None:
        return
    options = ui.collection_options(collections_payload)
    if not options:
        st.info("No collections are configured.")
        return

    with st.form("search-form"):
        query = st.text_input("Query", max_chars=1000, placeholder="What are you looking for?")
        form_columns = st.columns([2, 3])
        mode = form_columns[0].radio(
            "Mode",
            options=("hybrid", "keyword", "semantic"),
            horizontal=True,
            help=" ".join(f"**{name}**: {text}" for name, text in _MODE_HELP.items()),
        )
        selected_collections = form_columns[1].multiselect(
            "Collections",
            options=list(options),
            default=list(options)[:1],
            format_func=options.get,
            help="Pick one collection for ranked hits with snippets; several "
            "collections return match totals only.",
        )
        submitted = st.form_submit_button("Search", type="primary", icon=":material/search:")

    if not submitted:
        st.caption(
            "Search covers published (active) content only. Pending uploads stay "
            "in the private screening index and are never retrievable."
        )
        return
    if not query.strip():
        st.warning("Enter a query first.")
        return
    if not selected_collections:
        st.warning("Select at least one collection.")
        return

    include_hits = len(selected_collections) == 1
    response = ui.guarded(
        lambda: client.search(
            query=query.strip(),
            mode=mode,
            collections=selected_collections,
            include_hits=include_hits,
            page=1,
            page_size=20,
        )
    )
    if response is None:
        return

    groups = response.get("groups", [])
    if not include_hits:
        st.markdown("##### Matches per collection")
        st.dataframe(
            [
                {
                    "Collection": options.get(group["collection_key"], group["collection_key"]),
                    "Matches": group["total"],
                }
                for group in groups
            ],
            width="stretch",
            hide_index=True,
        )
        st.caption("Narrow to a single collection to see ranked hits and snippets.")
        return

    group = groups[0] if groups else {"total": 0, "hits": []}
    st.markdown(f"##### {group.get('total', 0)} match(es)")
    if not group.get("hits"):
        st.info("No published content matched this query.")
        return
    for hit in group["hits"]:
        document_id = str(hit["document_id"])
        with st.container(border=True):
            columns = st.columns([6, 2, 2])
            columns[0].markdown(f"**{_document_label(client, document_id)}**")
            columns[1].metric("Score", f"{hit['score']:.3f}")
            if columns[2].button("Open document", key=f"hit-{document_id}"):
                st.query_params.clear()
                st.query_params["doc"] = document_id
                st.switch_page("views/library.py")
            snippet = hit.get("snippet", "")
            if snippet:
                st.markdown(
                    f'<div class="pdfb-excerpt">{snippet}</div>', unsafe_allow_html=True
                )


render()
