"""Upload page: preflight advisories, scanned intake, and live tracking."""

from __future__ import annotations

from typing import Any

import streamlit as st

import bridge_ui as ui
from bridge_client import BridgeProblem, BridgeUnreachable, new_idempotency_key


def _file_key(collection_key: str, name: str, size: int) -> str:
    return f"{collection_key}::{name}::{size}"


def _preflight_cached(client, *, collection_key: str, name: str, size: int) -> dict[str, Any]:
    """Run preflight once per selected file and remember the outcome."""

    cache: dict[str, Any] = st.session_state.setdefault("preflight_cache", {})
    key = _file_key(collection_key, name, size)
    if key not in cache:
        try:
            cache[key] = client.preflight(
                filename=name, size_bytes=size, collection_key=collection_key
            )
        except (BridgeProblem, BridgeUnreachable) as error:
            cache[key] = {"error": error}
    return cache[key]


def _stable_idempotency_key(file_key: str) -> str:
    """Reuse one idempotency key per selected file so retries replay safely."""

    keys: dict[str, str] = st.session_state.setdefault("upload_idempotency_keys", {})
    return keys.setdefault(file_key, new_idempotency_key())


def _upload_one(client, file, collection_key: str) -> None:
    file_key = _file_key(collection_key, file.name, file.size)
    idempotency_key = _stable_idempotency_key(file_key)
    try:
        accepted = client.upload(
            filename=file.name,
            content=file.getvalue(),
            collection_key=collection_key,
            idempotency_key=idempotency_key,
        )
    except BridgeProblem as error:
        st.session_state.setdefault("upload_failures", {})[file_key] = error
        return
    except BridgeUnreachable as error:
        st.session_state.setdefault("upload_failures", {})[file_key] = error
        return
    st.session_state.setdefault("upload_failures", {}).pop(file_key, None)
    st.session_state["upload_idempotency_keys"].pop(file_key, None)
    upload = accepted.get("upload", {})
    tracked: list[str] = st.session_state.setdefault("tracked_uploads", [])
    upload_id = str(upload.get("upload_id", ""))
    if upload_id and upload_id not in tracked:
        tracked.insert(0, upload_id)
    replayed = " (idempotent replay)" if accepted.get("idempotent_replay") else ""
    st.toast(f"Accepted {file.name}{replayed}", icon=":material/check_circle:")


def _render_intake_form(client) -> None:
    collections_payload = ui.guarded(client.collections)
    if collections_payload is None:
        return
    options = ui.collection_options(collections_payload)
    if not options:
        st.info("No collections are configured; uploads need a target collection.")
        return

    collection_key = st.selectbox(
        "Target collection",
        options=list(options),
        format_func=options.get,
        help="Duplicates are blocked only inside the selected collection.",
    )
    files = st.file_uploader(
        "PDF files",
        type=["pdf"],
        accept_multiple_files=True,
        help="Each file is streamed, hashed, structurally checked, and "
        "scanned by ClamAV before analysis is queued.",
    )
    if not files:
        st.caption(
            "Encrypted, malformed, image-only, empty, or over-budget PDFs are "
            "rejected during analysis; OCR is not included."
        )
        return

    st.markdown("##### Preflight review")
    failures: dict[str, Any] = st.session_state.get("upload_failures", {})
    for file in files:
        file_key = _file_key(collection_key, file.name, file.size)
        with st.container(border=True):
            header, size_column = st.columns([5, 1])
            header.markdown(f"**{file.name}**")
            size_column.caption(ui.fmt_bytes(file.size))
            preflight = _preflight_cached(
                client, collection_key=collection_key, name=file.name, size=file.size
            )
            error = preflight.get("error")
            if isinstance(error, BridgeProblem):
                ui.render_problem(error)
                continue
            if isinstance(error, BridgeUnreachable):
                ui.render_unreachable(error)
                continue
            normalized = preflight.get("normalized_filename", file.name)
            if normalized != file.name:
                st.caption(f"Stored as `{normalized}`")
            ui.render_filename_warnings(preflight.get("warnings", []))
            failure = failures.get(file_key)
            if isinstance(failure, BridgeProblem):
                ui.render_problem(failure)
            elif isinstance(failure, BridgeUnreachable):
                ui.render_unreachable(failure)

    if st.button(
        f"Upload {len(files)} file{'s' if len(files) != 1 else ''}",
        type="primary",
        icon=":material/upload_file:",
    ):
        progress = st.progress(0.0, text="Uploading…")
        for index, file in enumerate(files):
            progress.progress(
                index / len(files), text=f"Scanning and storing {file.name}…"
            )
            _upload_one(client, file, collection_key)
        progress.progress(1.0, text="Done")
        st.rerun()


def _render_tracking(client) -> None:
    tracked: list[str] = st.session_state.get("tracked_uploads", [])
    if not tracked:
        return
    st.divider()
    st.subheader("This session's uploads")
    any_open = False
    statuses: list[dict[str, Any]] = []
    for upload_id in tracked[:10]:
        status = ui.guarded(lambda upload_id=upload_id: client.upload_status(upload_id))
        if status is None:
            continue
        statuses.append(status)
        if status.get("open"):
            any_open = True

    for status in statuses:
        document = status["document"]
        operation = status.get("operation") or {}
        columns = st.columns([4, 2, 2, 3])
        columns[0].markdown(f"**{document['original_filename']}**")
        columns[1].markdown(ui.state_badge(document["state"]), unsafe_allow_html=True)
        columns[2].caption(document["collection_key"])
        phase = operation.get("phase")
        if status.get("open") and phase not in (None, "COMPLETE"):
            columns[3].progress(
                ui.phase_progress(phase), text=phase.replace("_", " ").title()
            )
        else:
            columns[3].caption(ui.fmt_relative(document["uploaded_at"]))

    action_columns = st.columns([2, 2, 5])
    if action_columns[0].button("Open review queue", icon=":material/fact_check:"):
        st.switch_page("views/workspace.py")
    if action_columns[1].button("Clear list", icon=":material/backspace:"):
        st.session_state["tracked_uploads"] = []
        st.rerun()
    if any_open:
        st.caption("Refreshing automatically while work is in progress…")


def render() -> None:
    """Render the upload intake page."""

    ui.apply_chrome(
        "Upload",
        "Stream PDFs through malware scanning into durable, reviewable analysis.",
    )
    client = ui.get_client()
    _render_intake_form(client)

    any_open = bool(st.session_state.get("tracked_uploads"))
    refresh = "3s" if any_open else None

    @st.fragment(run_every=refresh)
    def tracking_section() -> None:
        _render_tracking(client)

    tracking_section()


render()
