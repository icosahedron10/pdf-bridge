"""Collection-scoped intake with filename advisory and live operation polling."""

from __future__ import annotations

from typing import Any

import streamlit as st

import bridge_ui as ui


def _file_identity(collection_key: str, file: Any) -> str:
    file_identity = getattr(file, "file_id", None) or f"{file.name}:{file.size}"
    return f"{collection_key}:{file_identity}"


def _name_advisory(client, collection_key: str, filename: str) -> dict[str, Any] | None:
    cache: dict[str, dict[str, Any]] = st.session_state.setdefault("name_advisories", {})
    key = f"{collection_key}:{filename}"
    if key not in cache:
        result = ui.guarded(lambda: client.name_check(collection_key, filename=filename))
        if result is None:
            return None
        cache[key] = result
    return cache[key]


def _track(result: dict[str, Any]) -> None:
    document = result.get("document", {})
    operation = result.get("operation", {})
    document_id = str(document.get("id", ""))
    if not document_id:
        raise RuntimeError("upload response omitted document.id")
    tracked: list[dict[str, str]] = st.session_state.setdefault("tracked_documents", [])
    tracked[:] = [item for item in tracked if item.get("document_id") != document_id]
    tracked.insert(
        0,
        {
            "document_id": document_id,
            "operation_id": str(operation.get("id", "")),
        },
    )
    del tracked[12:]


def _upload_one(client, collection_key: str, file: Any) -> bool:
    identity = _file_identity(collection_key, file)
    result = ui.guarded(
        lambda: client.upload(
            collection_key,
            filename=file.name,
            content=file,
            idempotency_key=ui.idempotency_key("upload", identity),
        )
    )
    if result is None:
        return False
    _track(result)
    replay = " · idempotent replay" if result.get("idempotent_replay") else ""
    st.toast(f"Accepted {file.name}{replay}", icon=":material/task_alt:")
    return True


def _render_intake(client) -> None:
    collections = ui.guarded(lambda: client.collections(limit=100))
    if collections is None:
        return
    options = ui.collection_options(collections)
    if not options:
        st.warning("No enabled collection can accept documents.")
        return

    collection_key = st.selectbox(
        "Destination collection",
        options=list(options),
        format_func=options.get,
        help="The logical collection is immutable after admission.",
    )
    files = st.file_uploader(
        "PDF documents",
        type=["pdf"],
        accept_multiple_files=True,
        help=(
            "Each file is bounded, validated, scanned, and durably admitted before "
            "preflight begins."
        ),
    )
    if not files:
        st.info("Choose one or more PDFs to run the filename-only advisory before intake.")
        return
    selection_limit = ui.max_upload_files()
    if len(files) > selection_limit:
        st.error(
            f"Select at most {selection_limit} PDFs at once. This workspace is sized for "
            "a small durable queue."
        )
        return

    st.markdown("#### Filename advisory")
    for file in files:
        with st.container(border=True):
            left, right = st.columns([5, 2])
            left.markdown(f"**{file.name}**")
            right.caption(ui.fmt_bytes(file.size))
            advisory = _name_advisory(client, collection_key, file.name)
            if advisory is not None:
                ui.render_name_matches(advisory.get("matches", []))

    st.caption(
        "This advisory reads filenames only. Semantic preflight starts after durable "
        "upload and cannot be skipped."
    )
    if st.button(
        f"Upload {len(files)} document{'s' if len(files) != 1 else ''}",
        type="primary",
        icon=":material/upload_file:",
    ):
        progress = st.progress(0.0, text="Submitting documents…")
        succeeded = 0
        for index, file in enumerate(files):
            progress.progress(index / len(files), text=f"Accepting {file.name}…")
            file.seek(0)
            succeeded += int(_upload_one(client, collection_key, file))
        progress.progress(1.0, text=f"Accepted {succeeded} of {len(files)}")
        if succeeded:
            st.rerun()


def _open_document(document_id: str, *, review: bool) -> None:
    st.query_params["document"] = document_id
    st.switch_page("views/workspace.py" if review else "views/library.py")


def _render_tracking(client) -> bool:
    tracked: list[dict[str, str]] = st.session_state.get("tracked_documents", [])
    if not tracked:
        return False

    st.divider()
    st.subheader("Recent intake")
    any_running = False
    retained: list[dict[str, str]] = []
    for item in tracked:
        document_id = item["document_id"]
        document = ui.guarded(lambda document_id=document_id: client.document(document_id))
        if document is None:
            retained.append(item)
            continue
        retained.append(item)
        operation = document.get("current_operation") or {}
        operation_id = str(operation.get("id") or item.get("operation_id") or "")
        if operation_id and operation.get("state") in {"QUEUED", "RUNNING"}:
            detailed = ui.guarded(lambda operation_id=operation_id: client.operation(operation_id))
            if detailed is not None:
                operation = detailed
        any_running = any_running or document.get("state") in ui.WORKING_DOCUMENT_STATES

        with st.container(border=True):
            heading, state_column, action = st.columns([5, 2, 2])
            heading.markdown(f"**{document.get('original_filename', document_id)}**")
            heading.caption(
                f"{document.get('collection_key', '—')} · "
                f"{ui.fmt_bytes(document.get('size_bytes'))} · "
                f"{ui.fmt_relative(document.get('created_at'))}"
            )
            state_column.markdown(
                ui.state_badge(str(document.get("state", "UNKNOWN"))),
                unsafe_allow_html=True,
            )
            needs_review = document.get("state") == "REVIEW_REQUIRED"
            if action.button(
                "Review" if needs_review else "Inspect",
                key=f"track-open::{document_id}",
                use_container_width=True,
            ):
                _open_document(document_id, review=needs_review)
            if operation:
                ui.render_operation(operation)
    st.session_state["tracked_documents"] = retained
    if st.button("Clear recent intake", icon=":material/backspace:"):
        st.session_state["tracked_documents"] = []
        st.rerun()
    return any_running


def render() -> None:
    ui.apply_chrome(
        "Intake",
        "Admit scanned PDFs, then follow durable preflight and publication work by document.",
    )
    client = ui.get_client()
    _render_intake(client)

    @st.fragment(run_every="2s")
    def tracking_fragment() -> None:
        running = _render_tracking(client)
        if running:
            st.caption("Polling every two seconds while durable work is open.")

    tracking_fragment()


render()
