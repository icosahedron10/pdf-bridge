"""Review queue: durable upload workspace, evidence review, and decisions."""

from __future__ import annotations

from typing import Any

import streamlit as st

import bridge_ui as ui
from bridge_client import new_idempotency_key

_DECISION_HELP = (
    "Keep publishes this document alongside existing content. Replace prepares "
    "the new analysis, deletes the old document, then publishes — a short "
    "availability gap is accepted so old and new content are never active "
    "together. Cancel purges the upload's bytes, artifacts, and index points."
)


def _selected_upload_id() -> str | None:
    return st.query_params.get("upload")


def _select_upload(upload_id: str | None) -> None:
    if upload_id is None:
        st.query_params.pop("upload", None)
    else:
        st.query_params["upload"] = upload_id
    st.rerun()


def _decision_key(upload_id: str, action: str, revision: int) -> str:
    """Stable idempotency key per decision so a retried click replays."""

    keys: dict[str, str] = st.session_state.setdefault("decision_idempotency_keys", {})
    return keys.setdefault(f"{upload_id}::{action}::{revision}", new_idempotency_key())


def _render_queue_list(client) -> None:
    filter_columns = st.columns([2, 3, 2])
    open_only = filter_columns[0].toggle("Open work only", value=True)
    collections_payload = ui.guarded(client.collections) or {"items": []}
    options = {"": "All collections"} | ui.collection_options(collections_payload)
    collection_key = filter_columns[1].selectbox(
        "Collection", options=list(options), format_func=options.get, key="queue_collection"
    )
    page = filter_columns[2].number_input("Page", min_value=1, value=1, key="queue_page")

    payload = ui.guarded(
        lambda: client.uploads(
            open_only=open_only,
            collection_key=collection_key or None,
            page=int(page),
            page_size=25,
        )
    )
    if payload is None:
        return
    items = payload.get("items", [])
    st.caption(
        f"{payload.get('total', 0)} upload{'s' if payload.get('total') != 1 else ''} · "
        f"page {payload.get('page', 1)} of {max(payload.get('pages', 1), 1)}"
    )
    if not items:
        st.info("Nothing here. Open work appears as soon as an upload is accepted.")
        return

    selected = _selected_upload_id()
    for item in items:
        document = item["document"]
        upload_id = str(item["upload_id"])
        with st.container(border=True):
            columns = st.columns([5, 2, 2, 2, 2])
            columns[0].markdown(f"**{document['original_filename']}**")
            columns[0].caption(
                f"{document['collection_key']} · {ui.fmt_bytes(document['size_bytes'])}"
                f" · {ui.fmt_relative(document['uploaded_at'])}"
            )
            columns[1].markdown(ui.state_badge(document["state"]), unsafe_allow_html=True)
            if item.get("review_required"):
                columns[2].markdown(
                    ui.badge("Needs decision", "warn"), unsafe_allow_html=True
                )
            operation = item.get("operation") or {}
            phase = operation.get("phase")
            if phase and phase != "COMPLETE" and item.get("open"):
                columns[3].caption(phase.replace("_", " ").title())
            if columns[4].button(
                "Review" if upload_id != selected else "Selected",
                key=f"select-{upload_id}",
                disabled=upload_id == selected,
            ):
                _select_upload(upload_id)


def _render_findings(findings: list[dict[str, Any]]) -> None:
    for finding in findings:
        role = finding.get("role", "model")
        if not finding.get("valid", False):
            st.markdown(
                f"- **{role.title()}** ({finding.get('model_id', '?')}): "
                + ui.badge("invalid output", "neutral")
                + f" {finding.get('error') or ''}",
                unsafe_allow_html=True,
            )
            continue
        label = finding.get("label") or "uncertain"
        tone = ui.FINDING_LABEL_TONES.get(label, "neutral")
        summary = finding.get("summary") or "No summary."
        st.markdown(
            f"- **{role.title()}** ({finding.get('model_id', '?')}): "
            + ui.badge(label.replace("_", " "), tone)
            + f" — {summary}",
            unsafe_allow_html=True,
        )


def _render_excerpts(candidate: dict[str, Any]) -> None:
    incoming = candidate.get("incoming_excerpts", [])
    existing = candidate.get("candidate_excerpts", [])
    if not incoming and not existing:
        return
    left, right = st.columns(2)
    left.markdown("**Incoming document**")
    for excerpt in incoming:
        left.caption(f"Pages {excerpt['page_start']}–{excerpt['page_end']}")
        left.markdown(
            f'<div class="pdfb-excerpt">{excerpt["text"]}</div>', unsafe_allow_html=True
        )
    right.markdown("**Existing candidate**")
    for excerpt in existing:
        right.caption(f"Pages {excerpt['page_start']}–{excerpt['page_end']}")
        right.markdown(
            f'<div class="pdfb-excerpt">{excerpt["text"]}</div>', unsafe_allow_html=True
        )


def _render_candidates(client, upload_id: str) -> list[dict[str, Any]]:
    page_key = f"evidence_page::{upload_id}"
    page = st.session_state.get(page_key, 1)
    payload = ui.guarded(
        lambda: client.upload_analysis(upload_id, page=page, page_size=10)
    )
    if payload is None:
        return []
    candidates = payload.get("candidates", [])
    total = payload.get("total_candidates", 0)
    pages = max(payload.get("pages", 1), 1)
    st.markdown(f"##### Candidate evidence · {total} qualifying candidate(s)")
    if not candidates:
        st.caption("No active or pending content resembles this document.")
        return []
    for candidate in candidates:
        matched = candidate.get("document", {})
        source = candidate.get("source", "active")
        title = (
            f"#{candidate['rank']} · {matched.get('filename', 'unknown')} "
            f"· fused score {candidate.get('fused_score', 0):.3f}"
        )
        with st.expander(title, expanded=candidate["rank"] == 1):
            badge_row = " ".join(
                [
                    ui.badge(
                        "active content" if source == "active" else "pending screening",
                        "info" if source == "active" else "warn",
                    ),
                    ui.badge(matched.get("state", "?"),
                             ui.DOCUMENT_STATE_TONES.get(matched.get("state", ""), "neutral")),
                ]
                + ([ui.badge("replacement eligible", "ok")]
                   if candidate.get("replacement_eligible") else [])
                + ([ui.badge("not classified (overflow)", "neutral")]
                   if candidate.get("overflow") else [])
            )
            st.markdown(badge_row, unsafe_allow_html=True)
            metric_columns = st.columns(4)
            metric_columns[0].metric("Max cosine", f"{candidate.get('max_cosine', 0):.3f}")
            metric_columns[1].metric("Strong chunks", candidate.get("strong_cosine_chunks", 0))
            metric_columns[2].metric("Moderate chunks", candidate.get("moderate_cosine_chunks", 0))
            metric_columns[3].metric("BM25 strong", candidate.get("bm25_strong_placements", 0))
            reasons = candidate.get("reasons", [])
            if reasons:
                st.caption("Qualified because: " + "; ".join(reasons))
            findings = candidate.get("findings", [])
            if findings:
                st.markdown("**Model findings** (advisory, explanation-only)")
                _render_findings(findings)
            _render_excerpts(candidate)
    if pages > 1:
        new_page = st.number_input(
            "Evidence page", min_value=1, max_value=pages, value=page, key=f"np-{upload_id}"
        )
        if new_page != page:
            st.session_state[page_key] = int(new_page)
            st.rerun()
    return candidates


def _render_decision_panel(
    client, upload: dict[str, Any], candidates: list[dict[str, Any]]
) -> None:
    analysis = upload.get("analysis") or {}
    revision = analysis.get("revision", 1)
    upload_id = str(upload["upload_id"])
    st.markdown("##### Operator decision")
    st.caption(_DECISION_HELP)
    if analysis.get("incomplete_reasons"):
        st.warning(
            "Analysis is incomplete — advisory checks could not all run: "
            + "; ".join(analysis["incomplete_reasons"])
        )

    def _submit(action: str, target: str | None = None) -> None:
        result = ui.guarded(
            lambda: client.decide(
                upload_id,
                analysis_revision=revision,
                action=action,
                target_document_id=target,
                idempotency_key=_decision_key(upload_id, action, revision),
            )
        )
        if result is not None:
            replay = " (replayed)" if result.get("idempotent_replay") else ""
            st.toast(f"Decision recorded: {action}{replay}", icon=":material/gavel:")
            st.rerun()

    keep_column, replace_column, cancel_column = st.columns(3)
    with keep_column:
        if st.button("Keep and publish", type="primary", icon=":material/check:"):
            _submit("keep")
    with replace_column:
        eligible = {
            str(candidate["document"]["document_id"]): (
                f"#{candidate['rank']} {candidate['document'].get('filename', 'unknown')}"
            )
            for candidate in candidates
            if candidate.get("replacement_eligible")
        }
        with st.popover("Replace existing…", icon=":material/swap_horiz:"):
            if not eligible:
                st.caption(
                    "No replacement-eligible candidate on this evidence page. "
                    "Only published documents in the same collection qualify."
                )
            else:
                target = st.selectbox(
                    "Document to replace",
                    options=list(eligible),
                    format_func=eligible.get,
                    key=f"replace-target-{upload_id}",
                )
                st.warning(
                    "The old document is deleted and verified before the new "
                    "one publishes; a brief availability gap is expected."
                )
                if st.button("Confirm replacement", type="primary", key=f"rp-{upload_id}"):
                    _submit("replace", target)
    with cancel_column:
        with st.popover("Cancel intake…", icon=":material/delete_forever:"):
            st.warning(
                "Cancelling purges the uploaded bytes, analysis artifacts, and "
                "screening index points. Only a content-free audit hash remains."
            )
            if st.button("Confirm cancellation", key=f"cx-{upload_id}"):
                _submit("cancel")


def _render_detail(client, upload_id: str) -> None:
    upload = ui.guarded(lambda: client.upload_status(upload_id))
    if upload is None:
        return
    document = upload["document"]
    st.markdown(f"### {document['original_filename']}")
    st.markdown(
        ui.state_badge(document["state"])
        + " "
        + ui.badge(document["collection_key"], "info")
        + " "
        + ui.badge(
            f"scan {document['scan_state']}",
            ui.SCAN_STATE_TONES.get(document["scan_state"], "neutral"),
        ),
        unsafe_allow_html=True,
    )
    info_columns = st.columns(4)
    info_columns[0].metric("Size", ui.fmt_bytes(document["size_bytes"]))
    info_columns[1].metric("Uploaded", ui.fmt_relative(document["uploaded_at"]))
    analysis = upload.get("analysis")
    info_columns[2].metric("Pages", (analysis or {}).get("page_count") or "—")
    info_columns[3].metric("Chunks", (analysis or {}).get("chunk_count") or "—")
    st.markdown(
        f'<div class="pdfb-kv">SHA-256</div>'
        f'<div class="pdfb-mono">{document["sha256"]}</div>',
        unsafe_allow_html=True,
    )

    operation = upload.get("operation") or {}
    if operation:
        st.markdown("##### Current operation")
        state = operation.get("state", "?")
        phase = operation.get("phase", "QUEUED")
        op_columns = st.columns([2, 5])
        op_columns[0].markdown(
            ui.badge(
                f"{operation.get('operation_type', '?')} · {state}",
                ui.OPERATION_STATE_TONES.get(state, "neutral"),
            )
            + " "
            + ui.badge(f"attempt {operation.get('attempt', 1)}", "neutral"),
            unsafe_allow_html=True,
        )
        if state == "RUNNING" or (state == "QUEUED" and upload.get("open")):
            op_columns[1].progress(
                ui.phase_progress(phase), text=phase.replace("_", " ").title()
            )
        if operation.get("error"):
            st.error(f"Last attempt failed: {operation['error']}")
            if operation.get("retryable") and st.button(
                "Retry without a new decision", icon=":material/replay:"
            ):
                if ui.guarded(lambda: client.retry_upload(upload_id)) is not None:
                    st.toast("Retry queued", icon=":material/replay:")
                    st.rerun()

    replacement = upload.get("replacement")
    if replacement:
        st.markdown("##### Replacement progress")
        tone = {"SUCCEEDED": "ok", "FAILED": "danger"}.get(replacement["state"], "info")
        st.markdown(ui.badge(replacement["state"], tone), unsafe_allow_html=True)
        if replacement.get("error"):
            st.error(replacement["error"])

    decision = upload.get("decision")
    if decision:
        st.markdown("##### Decision")
        st.markdown(
            ui.badge(decision["action"], "info")
            + f" by `{decision['actor_id']}` at {ui.fmt_dt(decision['created_at'])}"
            + (" against revision " + str(decision["analysis_revision"])),
            unsafe_allow_html=True,
        )

    if analysis:
        st.markdown("##### Analysis")
        summary_bits = [
            ui.badge(
                f"revision {analysis['revision']} · {analysis['status']}",
                "ok" if analysis["status"] == "COMPLETE" else "info",
            ),
            ui.badge(
                "semantic complete" if analysis["semantic_complete"] else "semantic incomplete",
                "ok" if analysis["semantic_complete"] else "warn",
            ),
            ui.badge(
                "classification complete"
                if analysis["classification_complete"]
                else "classification incomplete",
                "ok" if analysis["classification_complete"] else "warn",
            ),
        ]
        if analysis.get("auto_ingest_eligible"):
            summary_bits.append(ui.badge("auto-ingest eligible", "ok"))
        st.markdown(" ".join(summary_bits), unsafe_allow_html=True)
        ui.render_filename_warnings(analysis.get("filename_warnings", []))

    candidates: list[dict[str, Any]] = []
    if upload.get("analysis_url") and analysis:
        candidates = _render_candidates(client, upload_id)

    if upload.get("review_required") and document["state"] == "REVIEW_REQUIRED" and analysis:
        st.divider()
        _render_decision_panel(client, upload, candidates)
    elif upload.get("open") and document["state"] not in ("INGESTED",):
        with st.popover("Cancel this intake…", icon=":material/delete_forever:"):
            st.warning(
                "Cancelling unpublished work purges its bytes, artifacts, and "
                "index points after recording a content-free manifest hash."
            )
            if st.button("Confirm cancellation", key=f"cancel-open-{upload_id}"):
                if ui.guarded(lambda: client.cancel_upload(upload_id)) is not None:
                    st.toast("Cancellation queued", icon=":material/delete_forever:")
                    _select_upload(None)


def render() -> None:
    """Render the durable review queue and the selected upload's detail."""

    ui.apply_chrome(
        "Review queue",
        "Durable intake work: restore after refresh, inspect evidence, decide.",
    )
    client = ui.get_client()
    selected = _selected_upload_id()

    st.subheader("Queue")
    _render_queue_list(client)

    if not selected:
        st.info("Select an upload to inspect its evidence and record a decision.")
        return

    st.divider()
    upload = ui.guarded(lambda: client.upload_status(selected))
    if upload is None:
        return
    # Auto-refresh only while the worker is busy; a static page while a human
    # reads evidence keeps popovers and scroll position stable.
    worker_busy_states = {"ANALYZING", "INGESTING", "REPLACING", "DELETING", "CLEANUP_PENDING"}
    refresh = "4s" if upload["document"]["state"] in worker_busy_states else None
    if refresh is None:
        if st.button("Refresh", icon=":material/refresh:"):
            st.rerun()
    else:
        st.caption("Refreshing automatically while the worker is busy…")

    @st.fragment(run_every=refresh)
    def detail_section() -> None:
        _render_detail(client, selected)

    detail_section()


render()
