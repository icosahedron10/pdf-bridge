from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from pdf_bridge.controllers import api as api_controller
from pdf_bridge.persistence.models import (
    AnalysisCandidate,
    AnalysisChunk,
    AnalysisStatus,
    CandidateFindingRecord,
    Document,
    DocumentAnalysis,
    DocumentState,
    OperationPhase,
    OperationState,
    OperationType,
    ScanState,
    WorkOperation,
    utc_now,
)
from tests.conftest import PDF_A, PDF_B

pytestmark = pytest.mark.browser


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _select_collection(page: Page, collection: str) -> None:
    page.locator(f'input[name="collection_key"][value="{collection}"]').check()


def _upload_files(
    page: Page,
    base_url: str,
    files: list[dict],
    *,
    collection: str = "customer",
) -> list[str]:
    page.goto(f"{base_url}/upload")
    _select_collection(page, collection)
    page.locator("#pdf-files").set_input_files(files)
    choices = page.locator('input[name="collection_key"]')
    expect(choices.nth(0)).to_be_disabled()
    expect(choices.nth(1)).to_be_disabled()
    ready = page.locator("[data-file-status]", has_text="Ready to upload and analyze")
    expect(ready).to_have_count(len(files))
    page.get_by_role("button", name="Upload ready files").click()
    paths = []
    for file in files:
        row = page.locator("[data-upload-item]").filter(has_text=file["name"])
        link = row.get_by_role("link", name="Document details")
        expect(link).to_be_visible()
        paths.append(link.get_attribute("href"))
    return paths


def _mark_ingested(app, document_id: str) -> None:
    with app.state.test_session_factory() as session:
        document = session.get(Document, uuid.UUID(document_id))
        assert document is not None
        analyze = next(
            item for item in document.operations if item.operation_type == OperationType.ANALYZE
        )
        analyze.state = OperationState.SUCCEEDED
        analyze.phase = OperationPhase.COMPLETE
        analyze.started_at = analyze.started_at or utc_now()
        analyze.completed_at = utc_now()
        document.state = DocumentState.INGESTED
        document.ingested_at = utc_now()
        document.page_count = 1
        document.chunk_count = 4
        session.add(
            WorkOperation(
                document=document,
                operation_type=OperationType.INGEST,
                state=OperationState.SUCCEEDED,
                phase=OperationPhase.COMPLETE,
                attempt=1,
                started_at=utc_now(),
                completed_at=utc_now(),
            )
        )
        session.commit()


def _seed_review(
    app,
    document_id: str,
    *,
    with_candidate: bool,
    incomplete: bool = False,
) -> str | None:
    """Persist a finished analysis so the browser can exercise durable review."""

    with app.state.test_session_factory() as session:
        incoming = session.get(Document, uuid.UUID(document_id))
        assert incoming is not None
        analyze = next(
            item for item in incoming.operations if item.operation_type == OperationType.ANALYZE
        )
        analyze.state = OperationState.SUCCEEDED
        analyze.phase = OperationPhase.AWAITING_DECISION
        analyze.started_at = analyze.started_at or utc_now()
        analyze.completed_at = utc_now()

        analysis = DocumentAnalysis(
            document=incoming,
            revision=1,
            status=AnalysisStatus.COMPLETE,
            pipeline_fingerprint="browser-analysis-v1",
            collection_epoch=1,
            page_count=1,
            chunk_count=1,
            semantic_complete=not incomplete,
            classification_complete=not incomplete,
            incomplete_reasons=(
                ["embedding endpoint unavailable"] if incomplete else []
            ),
            auto_ingest_eligible=False,
            candidate_count=1 if with_candidate else 0,
            classified_count=1 if with_candidate else 0,
            completed_at=utc_now(),
        )
        session.add(analysis)
        session.flush()
        incoming_chunk = AnalysisChunk(
            id=uuid.uuid4(),
            analysis=analysis,
            document_id=incoming.id,
            chunk_index=0,
            page_start=1,
            page_end=1,
            token_count=9,
            text_hash="1" * 64,
            text="The travel policy permits economy flights for domestic travel.",
        )
        session.add(incoming_chunk)

        candidate_id: uuid.UUID | None = None
        if with_candidate:
            candidate = Document(
                original_filename="Travel Policy 2025.pdf",
                normalized_filename="travel policy 2025.pdf",
                storage_key=None,
                size_bytes=2048,
                sha256="c" * 64,
                idempotency_key=f"browser-candidate-{uuid.uuid4()}",
                state=DocumentState.INGESTED,
                scan_state=ScanState.CLEAN,
                uploader_identity="browser-seed",
                collection_key=incoming.collection_key,
                collection_epoch=1,
                ingested_at=utc_now(),
                analysis_revision=1,
            )
            session.add(candidate)
            session.flush()
            candidate_analysis = DocumentAnalysis(
                document=candidate,
                revision=1,
                status=AnalysisStatus.COMPLETE,
                pipeline_fingerprint="browser-analysis-v1",
                collection_epoch=1,
                page_count=1,
                chunk_count=1,
                semantic_complete=True,
                classification_complete=True,
                auto_ingest_eligible=True,
                completed_at=utc_now(),
            )
            session.add(candidate_analysis)
            session.flush()
            candidate_chunk = AnalysisChunk(
                id=uuid.uuid4(),
                analysis=candidate_analysis,
                document_id=candidate.id,
                chunk_index=0,
                page_start=2,
                page_end=2,
                token_count=9,
                text_hash="2" * 64,
                text="<script>alert(1)</script> The policy permits business flights.",
            )
            session.add(candidate_chunk)
            record = AnalysisCandidate(
                analysis=analysis,
                matched_document_id=candidate.id,
                source="active",
                rank=1,
                reasons=["cosine_strong", "filename_family"],
                max_cosine=0.91,
                strong_cosine_chunks=1,
                moderate_cosine_chunks=2,
                bm25_strong_placements=3,
                fused_score=0.048,
                classified=True,
                matched_chunk_pairs=[[0, str(candidate_chunk.id)]],
                document_snapshot={
                    "document_id": str(candidate.id),
                    "filename": candidate.original_filename,
                    "size_bytes": candidate.size_bytes,
                    "state": candidate.state.value,
                    "collection_key": candidate.collection_key,
                },
            )
            session.add(record)
            session.flush()
            session.add(
                CandidateFindingRecord(
                    candidate=record,
                    role="classifier",
                    model_id="browser-classifier",
                    valid=True,
                    label="potential_contradiction",
                    summary="The permitted travel class differs.",
                    evidence=[
                        {
                            "chunk_reference": f"candidate:{candidate_chunk.id}",
                            "quote": "business flights",
                        }
                    ],
                )
            )
            candidate_id = candidate.id

        incoming.analysis_revision = 1
        incoming.page_count = 1
        incoming.chunk_count = 1
        incoming.state = DocumentState.REVIEW_REQUIRED
        session.commit()
        return str(candidate_id) if candidate_id else None


def _seed_open_uploads(app, count: int) -> None:
    """Create enough durable failed rows to exercise restoration pagination."""

    with app.state.test_session_factory() as session:
        for index in range(count):
            document = Document(
                original_filename=f"restored-{index:03d}.pdf",
                normalized_filename=f"restored-{index:03d}.pdf",
                storage_key=f"canonical/customer/restored-{index:03d}.pdf",
                size_bytes=1024 + index,
                sha256=f"{index:064x}",
                idempotency_key=f"browser-restoration-{index:03d}",
                state=DocumentState.INGEST_FAILED,
                scan_state=ScanState.CLEAN,
                uploader_identity="browser-seed",
                collection_key="customer",
                collection_epoch=1,
            )
            session.add(document)
            session.flush()
            session.add(
                WorkOperation(
                    document=document,
                    operation_type=OperationType.INGEST,
                    state=OperationState.FAILED,
                    phase=OperationPhase.INGESTING,
                    attempt=1,
                    error="Seeded retryable publication failure.",
                    retryable=True,
                    started_at=utc_now(),
                    completed_at=utc_now(),
                )
            )
        session.commit()


@pytest.fixture
def live_server(app) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("Uvicorn did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.skipif(
    os.getenv("PDF_BRIDGE_RUN_BROWSER_TESTS") != "1",
    reason="set PDF_BRIDGE_RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_deployment_theme_system_override_persistence_and_accessibility(
    app, live_server: str
) -> None:
    app.state.settings.brand_primary_1 = "#123456"
    app.state.settings.brand_primary_2 = "#234567"
    app.state.settings.brand_secondary_1 = "#805500"
    app.state.settings.brand_secondary_2 = "#ffeeaa"
    app.state.settings.theme_default = "system"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(color_scheme="dark")
        page = context.new_page()
        page.goto(f"{live_server}/library")

        root = page.locator("html")
        toggle = page.locator("[data-theme-toggle]")
        expect(root).to_have_attribute("data-theme", "dark")
        expect(toggle).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_role("button", name="Dark mode")).to_be_visible()
        expect(toggle).to_have_attribute("title", "Switch to light mode")

        computed = page.evaluate(
            """() => {
                const rootStyles = getComputedStyle(document.documentElement);
                const primaryStyles = getComputedStyle(document.querySelector(".button--primary"));
                return {
                    action: rootStyles.getPropertyValue("--color-action").trim(),
                    actionHover: rootStyles.getPropertyValue("--color-action-hover").trim(),
                    focus: rootStyles.getPropertyValue("--color-focus").trim(),
                    accent: rootStyles.getPropertyValue("--color-accent").trim(),
                    primaryBackground: primaryStyles.backgroundColor
                };
            }"""
        )
        assert computed == {
            "action": "#123456",
            "actionHover": "#234567",
            "focus": "#805500",
            "accent": "#ffeeaa",
            "primaryBackground": "rgb(18, 52, 86)",
        }

        page.emulate_media(color_scheme="light")
        expect(root).to_have_attribute("data-theme", "light")
        expect(toggle).to_have_attribute("aria-pressed", "false")
        expect(page.get_by_role("button", name="Dark mode")).to_be_visible()
        expect(toggle).to_have_attribute("title", "Switch to dark mode")

        toggle.click()
        expect(root).to_have_attribute("data-theme", "dark")
        expect(toggle).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_role("button", name="Dark mode")).to_be_visible()
        expect(toggle).to_have_attribute("title", "Switch to light mode")
        assert page.evaluate("localStorage.getItem('pdf-bridge:theme')") == "dark"

        page.emulate_media(color_scheme="dark")
        page.emulate_media(color_scheme="light")
        expect(root).to_have_attribute("data-theme", "dark")
        page.reload()
        expect(root).to_have_attribute("data-theme", "dark")

        toggle.click()
        expect(root).to_have_attribute("data-theme", "light")
        assert page.evaluate("localStorage.getItem('pdf-bridge:theme')") == "light"
        page.emulate_media(color_scheme="dark")
        page.reload()
        expect(root).to_have_attribute("data-theme", "light")
        expect(page.get_by_role("button", name="Dark mode")).to_be_visible()
        expect(toggle).to_have_attribute("title", "Switch to dark mode")

        page.evaluate("localStorage.removeItem('pdf-bridge:theme')")
        app.state.settings.theme_default = "light"
        page.reload()
        expect(root).to_have_attribute("data-theme-default", "light")
        expect(root).to_have_attribute("data-theme", "light")
        page.emulate_media(color_scheme="dark")
        expect(root).to_have_attribute("data-theme", "light")

        app.state.settings.brand_primary_1 = "#ffffff"
        app.state.settings.brand_primary_2 = "#ffffff"
        page.goto(f"{live_server}/upload")
        hostile_palette_styles = page.evaluate(
            """() => {
                const rootStyles = getComputedStyle(document.documentElement);
                const primaryStyles = getComputedStyle(document.querySelector(".button--primary"));
                return {
                    action: rootStyles.getPropertyValue("--color-action").trim(),
                    primaryText: primaryStyles.color
                };
            }"""
        )
        assert hostile_palette_styles == {
            "action": "#ffffff",
            "primaryText": "rgb(0, 0, 0)",
        }

        page.get_by_role("button", name="Dark mode").click()
        expect(root).to_have_attribute("data-theme", "dark")
        page.emulate_media(media="print", color_scheme="dark")
        print_styles = page.evaluate(
            """() => {
                const bodyStyles = getComputedStyle(document.body);
                return {
                    background: bodyStyles.backgroundColor,
                    text: bodyStyles.color
                };
            }"""
        )
        assert print_styles == {
            "background": "rgb(255, 255, 255)",
            "text": "rgb(32, 36, 31)",
        }

        context.close()
        browser.close()


@pytest.mark.skipif(
    os.getenv("PDF_BRIDGE_RUN_BROWSER_TESTS") != "1",
    reason="set PDF_BRIDGE_RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_upload_queue_restoration_decisions_and_mobile_navigation(
    app, live_server: str
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        document_paths = _upload_files(
            page,
            live_server,
            [
                {
                    "name": "browser-flow-a.pdf",
                    "mimeType": "application/pdf",
                    "buffer": PDF_A,
                },
                {
                    "name": "browser-flow-b.pdf",
                    "mimeType": "application/pdf",
                    "buffer": PDF_B,
                },
            ],
        )
        first_id, second_id = [path.rsplit("/", 1)[-1] for path in document_paths]
        _seed_review(app, first_id, with_candidate=False, incomplete=True)
        _seed_review(app, second_id, with_candidate=False)

        page.goto(f"{live_server}/library")
        page.goto(f"{live_server}/upload#upload-{first_id}")
        expect(page.get_by_text("Restored 2 open uploads.")).to_be_visible()
        expect(page.get_by_text("browser-flow-a.pdf")).to_be_visible()
        expect(page.get_by_text("browser-flow-b.pdf")).to_be_visible()
        first_row = page.locator(f"#upload-{first_id}")
        first_heading = first_row.get_by_role("heading", name="Analysis evidence", exact=True)
        expect(first_heading).to_be_focused()
        expect(first_row.get_by_text("Analysis was incomplete", exact=True)).to_be_visible()
        expect(first_row.locator(".upload-item__status-line")).to_have_attribute(
            "role", "status"
        )
        first_row.locator('[data-decision-action][value="keep"]').check()
        first_row.get_by_role("button", name="Submit decision").click()
        expect(first_row.locator("[data-analysis-review]")).to_be_hidden()

        page.goto(f"{live_server}/upload#upload-{second_id}")
        second_row = page.locator(f"#upload-{second_id}")
        expect(
            second_row.get_by_role("heading", name="Analysis evidence", exact=True)
        ).to_be_focused()
        second_row.locator('[data-decision-action][value="cancel"]').check()
        page.once("dialog", lambda browser_dialog: browser_dialog.accept())
        second_row.get_by_role("button", name="Submit decision").click()
        expect(second_row.locator("[data-analysis-review]")).to_be_hidden()

        page.goto(f"{live_server}/queue")
        expect(page.get_by_text("browser-flow-a.pdf")).to_be_visible()
        expect(page.get_by_text("browser-flow-b.pdf")).to_be_visible()
        expect(page.locator('input[type="search"]')).to_have_count(0)

        page.set_viewport_size({"width": 390, "height": 760})
        page.goto(f"{live_server}/library")
        navigation = page.locator("#primary-navigation")
        page.get_by_role("button", name="Open navigation").click()
        expect(navigation).to_be_visible()
        page.keyboard.press("Escape")
        expect(navigation).not_to_be_visible()
        expect(page.get_by_role("button", name="Open navigation")).to_be_focused()

        page.goto(f"{live_server}/library")
        page.keyboard.press("Tab")
        expect(page.get_by_role("link", name="Skip to main content")).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#main-content")).to_be_focused()
        browser.close()


@pytest.mark.skipif(
    os.getenv("PDF_BRIDGE_RUN_BROWSER_TESTS") != "1",
    reason="set PDF_BRIDGE_RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_upload_request_pool_and_preflight_independence(
    live_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_preflight = api_controller.run_preflight
    phase = {"value": "pool"}
    pool_release = threading.Event()
    three_pool_requests_started = threading.Event()
    slow_release = threading.Event()
    slow_started = threading.Event()
    started_names: list[str] = []
    started_lock = threading.Lock()

    def controlled_preflight(*args, **kwargs):
        filename = str(kwargs["filename"])
        if phase["value"] == "pool":
            with started_lock:
                started_names.append(filename)
                if len(started_names) == 3:
                    three_pool_requests_started.set()
            if not pool_release.wait(timeout=5):
                raise RuntimeError("browser test did not release the request pool")
        elif filename == "slow-preflight.pdf":
            slow_started.set()
            if not slow_release.wait(timeout=5):
                raise RuntimeError("browser test did not release the delayed preflight")
        return original_preflight(*args, **kwargs)

    monkeypatch.setattr(api_controller, "run_preflight", controlled_preflight)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 760})
        try:
            page.goto(f"{live_server}/upload")
            _select_collection(page, "customer")
            page.locator("#pdf-files").set_input_files(
                [
                    {
                        "name": f"pool-{index}.pdf",
                        "mimeType": "application/pdf",
                        "buffer": PDF_A + str(index).encode(),
                    }
                    for index in range(4)
                ]
            )
            assert three_pool_requests_started.wait(timeout=3)
            time.sleep(0.2)
            with started_lock:
                assert len(started_names) == 3
            pool_release.set()
            expect(
                page.locator("[data-file-status]", has_text="Ready to upload and analyze")
            ).to_have_count(4)

            phase["value"] = "independence"
            page.reload()
            _select_collection(page, "customer")
            page.locator("#pdf-files").set_input_files(
                [
                    {
                        "name": "slow-preflight.pdf",
                        "mimeType": "application/pdf",
                        "buffer": PDF_A,
                    },
                    {
                        "name": "ready-while-slow.pdf",
                        "mimeType": "application/pdf",
                        "buffer": PDF_B,
                    },
                ]
            )
            assert slow_started.wait(timeout=3)
            fast_row = page.locator("[data-upload-item]").filter(
                has_text="ready-while-slow.pdf"
            )
            slow_row = page.locator("[data-upload-item]").filter(
                has_text="slow-preflight.pdf"
            )
            expect(
                fast_row.get_by_text("Ready to upload and analyze", exact=True)
            ).to_be_visible()
            expect(slow_row.get_by_text("Checking filename and size…", exact=True)).to_be_visible()
            start = page.get_by_role("button", name="Upload ready files")
            expect(start).to_be_enabled()
            start.click()
            expect(fast_row.get_by_role("link", name="Document details")).to_be_visible()
            expect(slow_row.get_by_text("Checking filename and size…", exact=True)).to_be_visible()
            slow_release.set()
            expect(
                slow_row.get_by_text("Ready to upload and analyze", exact=True)
            ).to_be_visible()
        finally:
            pool_release.set()
            slow_release.set()
            browser.close()


@pytest.mark.skipif(
    os.getenv("PDF_BRIDGE_RUN_BROWSER_TESTS") != "1",
    reason="set PDF_BRIDGE_RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_restores_every_paginated_open_upload(app, live_server: str) -> None:
    _seed_open_uploads(app, 101)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        page.goto(f"{live_server}/upload")
        expect(page.get_by_text("Restored 101 open uploads.")).to_be_visible()
        expect(page.locator("[data-upload-item]")).to_have_count(101)
        expect(page.get_by_text("restored-000.pdf", exact=True)).to_be_visible()
        expect(page.get_by_text("restored-100.pdf", exact=True)).to_be_visible()
        browser.close()


@pytest.mark.skipif(
    os.getenv("PDF_BRIDGE_RUN_BROWSER_TESTS") != "1",
    reason="set PDF_BRIDGE_RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_filename_advisory_exact_duplicate_and_queue_cancellation(live_server: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 760})
        _upload_files(
            page,
            live_server,
            [
                {
                    "name": "Customer Monthly Report May 2026.pdf",
                    "mimeType": "application/pdf",
                    "buffer": PDF_A,
                }
            ],
        )

        page.goto(f"{live_server}/upload")
        _select_collection(page, "customer")
        page.locator("#pdf-files").set_input_files(
            {
                "name": "Customer Monthly Report June 2026.pdf",
                "mimeType": "application/pdf",
                "buffer": PDF_B,
            }
        )
        june_row = page.locator("[data-upload-item]").filter(
            has_text="Customer Monthly Report June 2026.pdf"
        )
        expect(june_row.get_by_text("Filename advisory", exact=True)).to_be_visible()
        expect(page.locator("[data-duplicate-confirm]")).to_have_count(0)
        expect(
            page.get_by_text("Filename advisory found; ready to upload and analyze")
        ).to_be_visible()
        page.get_by_role("button", name="Upload ready files").click()
        expect(june_row.get_by_role("link", name="Document details")).to_be_visible()

        page.goto(f"{live_server}/upload")
        _select_collection(page, "customer")
        page.locator("#pdf-files").set_input_files(
            {"name": "renamed.pdf", "mimeType": "application/pdf", "buffer": PDF_A}
        )
        renamed_row = page.locator("[data-upload-item]").filter(has_text="renamed.pdf")
        expect(renamed_row.get_by_text("Ready to upload and analyze", exact=True)).to_be_visible()
        page.get_by_role("button", name="Upload ready files").click()
        expect(renamed_row.get_by_text("Exact duplicate blocked.", exact=True)).to_be_visible()
        expect(renamed_row.get_by_role("link", name="View existing document")).to_be_visible()

        page.goto(f"{live_server}/queue")
        page.get_by_role(
            "button", name="Cancel Customer Monthly Report May 2026.pdf"
        ).click()
        dialog = page.locator("#confirm-dialog")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Cancel upload").click()
        expect(page.get_by_text("Cleanup Pending", exact=True)).to_be_visible()
        browser.close()


@pytest.mark.skipif(
    os.getenv("PDF_BRIDGE_RUN_BROWSER_TESTS") != "1",
    reason="set PDF_BRIDGE_RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_semantic_evidence_and_confirmed_replacement(app, live_server: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 820})
        [document_path] = _upload_files(
            page,
            live_server,
            [
                {
                    "name": "Travel Policy 2026.pdf",
                    "mimeType": "application/pdf",
                    "buffer": PDF_A,
                }
            ],
            collection="internal",
        )
        document_id = document_path.rsplit("/", 1)[-1]
        candidate_id = _seed_review(
            app,
            document_id,
            with_candidate=True,
        )
        assert candidate_id is not None

        first_page_calls = 0
        first_candidate: dict | None = None

        def revision_changing_analysis(route, request) -> None:
            nonlocal first_candidate, first_page_calls
            response = route.fetch()
            payload = response.json()
            page_number = int(httpx.URL(request.url).params.get("page", "1"))
            if page_number == 1:
                first_page_calls += 1
                if first_page_calls == 1:
                    first_candidate = payload["candidates"][0]
                    payload["pages"] = 2
                    payload["total_candidates"] = 2
            elif page_number == 2 and first_page_calls == 1:
                assert first_candidate is not None
                wrong_revision = json.loads(json.dumps(first_candidate))
                wrong_revision["candidate_id"] = str(uuid.uuid4())
                wrong_revision["rank"] = 2
                wrong_revision["replacement_eligible"] = False
                wrong_revision["document"]["document_id"] = str(uuid.uuid4())
                wrong_revision["document"]["filename"] = "Wrong revision evidence.pdf"
                payload["analysis"]["id"] = str(uuid.uuid4())
                payload["analysis"]["revision"] = 2
                payload["candidates"] = [wrong_revision]
                payload["pages"] = 2
                payload["total_candidates"] = 2
            route.fulfill(
                status=response.status,
                content_type="application/json",
                body=json.dumps(payload),
            )

        page.route(
            "**/api/v1/uploads/*/analysis?*",
            revision_changing_analysis,
        )

        page.goto(f"{live_server}/upload#upload-{document_id}")
        row = page.locator(f"#upload-{document_id}")
        expect(row.get_by_role("heading", name="Analysis evidence", exact=True)).to_be_visible()
        assert first_page_calls == 2
        expect(row.get_by_text("Wrong revision evidence.pdf", exact=True)).to_have_count(0)
        expect(row.get_by_role("link", name="Travel Policy 2025.pdf", exact=True)).to_be_visible()
        expect(row.get_by_text("The permitted travel class differs.")).to_be_visible()
        row.get_by_text("Candidate excerpts (1)").click()
        hostile_text = row.get_by_text(
            "<script>alert(1)</script> The policy permits business flights."
        )
        expect(hostile_text).to_be_visible()
        assert "<script>alert(1)</script>" not in page.content()

        row.locator('[data-decision-action][value="replace"]').check()
        row.locator("[data-replacement-target]").select_option(candidate_id)
        row.get_by_label("I understand the selected document", exact=False).check()
        page.once("dialog", lambda browser_dialog: browser_dialog.accept())
        row.get_by_role("button", name="Submit decision").click()
        expect(row.locator("[data-analysis-review]")).to_be_hidden()
        page.goto(f"{live_server}/queue")
        expect(page.get_by_role("table").get_by_text("Replacing", exact=True)).to_be_visible()
        browser.close()


@pytest.mark.skipif(
    os.getenv("PDF_BRIDGE_RUN_BROWSER_TESTS") != "1",
    reason="set PDF_BRIDGE_RUN_BROWSER_TESTS=1 after installing Playwright Chromium",
)
def test_search_modes_and_confirmed_deletion(app, live_server: str) -> None:
    document_path: str
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        [document_path] = _upload_files(
            page,
            live_server,
            [
                {
                    "name": "searchable-handbook.pdf",
                    "mimeType": "application/pdf",
                    "buffer": PDF_A,
                }
            ],
        )
        document_id = document_path.rsplit("/", 1)[-1]
        _mark_ingested(app, document_id)

        def search_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            groups = []
            for collection in payload["collections"]:
                if not payload["include_hits"]:
                    groups.append(
                        {
                            "collection_key": collection,
                            "total": 1 if collection == "customer" else 0,
                            "hits": [],
                        }
                    )
                else:
                    groups.append(
                        {
                            "collection_key": collection,
                            "total": 1,
                            "hits": [
                                {
                                    "document_id": document_id,
                                    "score": 0.875,
                                    "snippet": "<script>alert(1)</script> retention policy",
                                }
                            ],
                        }
                    )
            return httpx.Response(
                200,
                json={
                    "query": payload["query"],
                    "mode": payload["mode"],
                    "groups": groups,
                },
            )

        search_client = httpx.Client(transport=httpx.MockTransport(search_handler))
        app.state.search_http_client = search_client
        for mode in ("keyword", "semantic", "hybrid"):
            page.goto(f"{live_server}/library")
            page.locator("#library-query").fill("retention")
            page.locator("#search-mode").select_option(mode)
            page.get_by_role("button", name="Search", exact=True).click()
            expect(
                page.locator(
                    ".collection-entry--customer .collection-entry__search-count strong"
                )
            ).to_have_text("1")
            customer_entry = page.locator("article").filter(has_text="Customer Product")
            customer_entry.get_by_role("link", name="View matches").click()
            expect(page.get_by_text("searchable-handbook.pdf")).to_be_visible()
            expect(page.get_by_text(f"using {mode} search.", exact=False)).to_be_visible()
            expect(page.get_by_text("<script>alert(1)</script> retention policy")).to_be_visible()
            assert "<script>alert(1)</script>" not in page.content()

        page.goto(f"{live_server}/library/customer")
        page.get_by_role("button", name="Delete").click()
        dialog = page.locator("#confirm-dialog")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Delete document").click()
        expect(page.get_by_text("Deleting", exact=True)).to_be_visible()
        browser.close()
    search_client.close()
