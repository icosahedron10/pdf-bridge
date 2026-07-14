"""Document-centric review queue, preflight evidence, decisions, and retry."""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

import bridge_ui as ui

_QUEUE_STATES = {
    "REVIEW_REQUIRED": "Needs decision",
    "PREFLIGHTING": "Preflight running",
    "PREFLIGHT_FAILED": "Preflight failed",
    "PUBLISHING": "Publishing",
    "PUBLISH_FAILED": "Publication failed",
    "DELETING": "Deleting",
    "DELETE_FAILED": "Deletion failed",
}


def _selected_document_id() -> str | None:
    value = st.query_params.get("document")
    return str(value) if value else None


def _select_document(document_id: str | None) -> None:
    if document_id is None:
        st.query_params.pop("document", None)
    else:
        st.query_params["document"] = document_id
    st.rerun()


def _render_queue(client) -> None:
    collections = ui.guarded(lambda: client.collections(limit=100))
    if collections is None:
        return
    options = ui.collection_options(collections)
    if not options:
        st.info("No enabled collections are configured.")
        return

    filters = st.columns([3, 3])
    collection_key = filters[0].selectbox(
        "Collection",
        options=list(options),
        format_func=options.get,
        key="review-collection",
    )
    state = filters[1].selectbox(
        "Work state",
        options=list(_QUEUE_STATES),
        format_func=_QUEUE_STATES.get,
        key="review-state",
    )
    scope = (collection_key, state)
    cursor = ui.cursor_for("review-documents", scope)
    payload = ui.guarded(
        lambda: client.documents(
            collection_key,
            state=state,
            cursor=cursor,
            limit=25,
        )
    )
    if payload is None:
        return
    items = payload.get("items", [])
    if not items:
        st.info(f"No documents are currently {_QUEUE_STATES[state].lower()}.")
        return

    selected = _selected_document_id()
    for document in items:
        document_id = str(document["id"])
        with st.container(border=True):
            heading, status, action = st.columns([5, 2, 2])
            heading.markdown(f"**{document['original_filename']}**")
            heading.caption(
                f"{ui.fmt_bytes(document.get('size_bytes'))} · "
                f"{ui.fmt_relative(document.get('updated_at'))} · `{document_id}`"
            )
            status.markdown(
                ui.state_badge(str(document.get("state", "UNKNOWN"))),
                unsafe_allow_html=True,
            )
            if action.button(
                "Selected" if selected == document_id else "Inspect",
                key=f"review-select::{document_id}",
                disabled=selected == document_id,
                use_container_width=True,
            ):
                _select_document(document_id)
            failure = document.get("failure")
            if failure:
                st.caption(
                    f"Failure `{failure.get('code', 'unknown')}`"
                    + (" · retryable" if failure.get("retryable") else "")
                )
    ui.render_cursor_controls("review-documents", payload)


def _render_completeness(preflight: dict[str, Any]) -> None:
    completeness = preflight.get("completeness", {})
    checks = [
        ("Native text", completeness.get("native_text_eligible")),
        ("Markdown", completeness.get("formatter_complete")),
        ("Vectors", completeness.get("vector_complete")),
        ("Candidates", completeness.get("candidate_discovery_complete")),
        ("Advisory", completeness.get("advisory_complete")),
    ]
    columns = st.columns(len(checks))
    for column, (label, complete) in zip(columns, checks, strict=True):
        column.markdown(ui.badge(label, "ok" if complete else "warn"), unsafe_allow_html=True)
    if completeness.get("clear_for_publication"):
        st.success("The sealed revision is clear for publication.")
    reasons = completeness.get("incomplete_reasons", [])
    if reasons:
        st.warning("Incomplete evidence: " + "; ".join(str(reason) for reason in reasons))


def _render_evidence(evidence: list[dict[str, Any]]) -> None:
    for finding in evidence:
        kind = str(finding.get("kind", "EVIDENCE")).replace("_", " ").title()
        label = str(finding.get("label") or ("Valid" if finding.get("valid") else "Incomplete"))
        tone = "info" if finding.get("valid") else "warn"
        st.markdown(
            f"**{kind}** · {ui.badge(label, tone)}",
            unsafe_allow_html=True,
        )
        if finding.get("summary"):
            st.write(finding["summary"])
        if finding.get("failure_code"):
            st.caption(f"Failure `{finding['failure_code']}`")
        for citation in finding.get("citations", []):
            page_label = f"pages {citation.get('page_start')}–{citation.get('page_end')}"
            st.caption(f"{page_label} · chunk `{citation.get('chunk_id', '—')}`")
            st.markdown(
                f'<div class="pdfb-excerpt">{html.escape(str(citation.get("excerpt", "")))}</div>',
                unsafe_allow_html=True,
            )


def _render_candidates(client, document_id: str, revision_id: str) -> list[dict[str, Any]]:
    cursor_key = "review-candidates"
    cursor = ui.cursor_for(cursor_key, (document_id, revision_id))
    preflight = ui.guarded(lambda: client.preflight(document_id, cursor=cursor, limit=20))
    if preflight is None:
        return []

    _render_completeness(preflight)
    revision = preflight.get("prepared_revision", {})
    profile_columns = st.columns(3)
    profile_columns[0].caption(f"Content profile · `{revision.get('content_profile_id', '—')}`")
    profile_columns[1].caption(f"Index profile · `{revision.get('index_profile_id', '—')}`")
    profile_columns[2].caption(f"Policy · `{revision.get('preflight_policy_id', '—')}`")

    candidate_page = preflight.get("candidates", {})
    candidates = candidate_page.get("items", [])
    st.markdown(f"#### Candidate evidence · {preflight.get('candidate_count', 0)} total")
    if not candidates:
        st.caption("No retained candidate appears on this page.")
    for candidate in candidates:
        matched = candidate.get("document", {})
        title = (
            f"#{candidate.get('rank', '?')} · {matched.get('original_filename', 'Unknown')} "
            f"· fused {float(candidate.get('fused_score', 0)):.3f}"
        )
        with st.expander(title, expanded=candidate.get("rank") == 1):
            tags = [
                ui.badge(str(candidate.get("source", "UNKNOWN")).title(), "info"),
                ui.state_badge(str(matched.get("state", "UNKNOWN"))),
            ]
            if candidate.get("replacement_eligible"):
                tags.append(ui.badge("Replacement eligible", "ok"))
            st.markdown(" ".join(tags), unsafe_allow_html=True)
            scores = st.columns(4)
            scores[0].metric("Cosine", f"{float(candidate.get('max_cosine', 0)):.3f}")
            scores[1].metric("BM25", f"{float(candidate.get('bm25_score', 0)):.3f}")
            scores[2].metric("Fused", f"{float(candidate.get('fused_score', 0)):.3f}")
            scores[3].metric("Chunk pairs", candidate.get("matched_chunk_pair_count", 0))
            if candidate.get("reasons"):
                st.caption("Qualified by: " + "; ".join(candidate["reasons"]))
            _render_evidence(candidate.get("evidence", []))
    ui.render_cursor_controls(cursor_key, candidate_page)
    return candidates


def _submit_decision(
    client,
    *,
    document_id: str,
    revision_id: str,
    action: str,
    target_document_id: str | None = None,
) -> None:
    result = ui.guarded(
        lambda: client.decide(
            document_id,
            prepared_revision_id=revision_id,
            action=action,
            target_document_id=target_document_id,
            idempotency_key=ui.idempotency_key(
                "decision", document_id, revision_id, action, target_document_id
            ),
        )
    )
    if result is not None:
        st.toast(f"{action.title()} accepted", icon=":material/gavel:")
        st.rerun()


def _render_decisions(
    client,
    document: dict[str, Any],
    revision_id: str,
    candidates: list[dict[str, Any]],
) -> None:
    actions = set(document.get("allowed_actions", []))
    if not actions.intersection({"KEEP", "REPLACE", "CANCEL"}):
        return
    st.divider()
    st.subheader("Operator decision")
    st.caption(
        "The decision binds to this exact sealed revision. Replace first removes and "
        "verifies the old active points."
    )
    keep, replace, cancel = st.columns(3)
    document_id = str(document["id"])
    with keep:
        if st.button(
            "Keep and publish",
            type="primary",
            disabled="KEEP" not in actions,
            use_container_width=True,
        ):
            _submit_decision(
                client,
                document_id=document_id,
                revision_id=revision_id,
                action="KEEP",
            )
    with replace:
        eligible = {
            str(candidate["document"]["id"]): candidate["document"]["original_filename"]
            for candidate in candidates
            if candidate.get("replacement_eligible")
        }
        with st.popover(
            "Replace existing",
            disabled="REPLACE" not in actions,
            use_container_width=True,
        ):
            if not eligible:
                st.caption("No replacement-eligible document appears on this candidate page.")
            else:
                target = st.selectbox(
                    "Ready document",
                    options=list(eligible),
                    format_func=eligible.get,
                    key=f"replace-target::{document_id}",
                )
                st.warning(
                    "The old document becomes unavailable before the new revision publishes."
                )
                if st.button("Confirm replacement", type="primary"):
                    _submit_decision(
                        client,
                        document_id=document_id,
                        revision_id=revision_id,
                        action="REPLACE",
                        target_document_id=target,
                    )
    with cancel:
        with st.popover(
            "Cancel intake",
            disabled="CANCEL" not in actions,
            use_container_width=True,
        ):
            st.warning(
                "Cancel purges unpublished content and retains only a content-free tombstone."
            )
            if st.button("Confirm cancellation", key=f"cancel::{document_id}"):
                _submit_decision(
                    client,
                    document_id=document_id,
                    revision_id=revision_id,
                    action="CANCEL",
                )


def _retry(client, document: dict[str, Any]) -> None:
    document_id = str(document["id"])
    current = document.get("current_operation") or {}
    result = ui.guarded(
        lambda: client.retry(
            document_id,
            idempotency_key=ui.idempotency_key(
                "retry", document_id, current.get("id"), current.get("attempt")
            ),
        )
    )
    if result is not None:
        st.toast("Retry accepted", icon=":material/replay:")
        st.rerun()


def _render_detail(client, document_id: str) -> dict[str, Any] | None:
    document = ui.guarded(lambda: client.document(document_id))
    if document is None:
        return None
    heading, status = st.columns([5, 2])
    heading.markdown(f"### {document['original_filename']}")
    heading.caption(f"{document['collection_key']} · `{document_id}`")
    status.markdown(ui.state_badge(document["state"]), unsafe_allow_html=True)

    facts = st.columns(4)
    facts[0].metric("Size", ui.fmt_bytes(document.get("size_bytes")))
    facts[1].metric("Updated", ui.fmt_relative(document.get("updated_at")))
    revision = document.get("prepared_revision") or {}
    facts[2].metric("Pages", revision.get("page_count") if revision else "—")
    facts[3].metric("Chunks", revision.get("chunk_count") if revision else "—")

    operation = document.get("current_operation")
    if operation:
        operation_id = str(operation["id"])
        detail = ui.guarded(lambda: client.operation(operation_id))
        st.markdown("#### Durable operation")
        ui.render_operation(detail or operation, show_timing=detail is not None)

    failure = document.get("failure")
    if failure:
        st.error(str(failure.get("message", "The document workflow failed.")))
    if "RETRY" in document.get("allowed_actions", []):
        if st.button("Retry eligible phase", icon=":material/replay:"):
            _retry(client, document)

    candidates: list[dict[str, Any]] = []
    revision_id = str(revision.get("id", ""))
    if revision_id:
        st.divider()
        st.subheader("Preflight inspection")
        candidates = _render_candidates(client, document_id, revision_id)
        _render_decisions(client, document, revision_id, candidates)
    return document


def render() -> None:
    ui.apply_chrome(
        "Review",
        "Inspect revision-bound preflight evidence, decide explicitly, and retry "
        "exact failed phases.",
    )
    client = ui.get_client()
    _render_queue(client)
    selected = _selected_document_id()
    if selected is None:
        st.info("Select a document to inspect its current operation and preflight evidence.")
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
    def detail_fragment() -> None:
        _render_detail(client, selected)

    detail_fragment()


render()
