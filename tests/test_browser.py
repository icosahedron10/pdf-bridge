from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from tests.conftest import PDF_A, PDF_B

pytestmark = pytest.mark.browser


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _upload_files(page: Page, base_url: str, files: list[dict]) -> list[str]:
    page.goto(f"{base_url}/upload")
    page.locator("#pdf-files").set_input_files(files)
    ready = page.locator("[data-file-status]", has_text="Ready to upload")
    expect(ready).to_have_count(len(files))
    page.get_by_role("button", name="Upload ready files").click()
    queued = page.locator("[data-file-status]", has_text="Queued successfully")
    expect(queued).to_have_count(len(files))
    return [
        page.locator("[data-upload-item]")
        .filter(has_text=file["name"])
        .get_by_role("link", name="View document")
        .get_attribute("href")
        for file in files
    ]


def _run_job_batch(client: httpx.Client, request_id: str) -> dict:
    claim = client.post(
        "/api/v1/jobs/batches/claim",
        json={"request_id": request_id, "limit": 100},
    )
    assert claim.status_code == 200, claim.text
    batch = claim.json()
    manifest_response = client.get(f"/api/v1/jobs/batches/{batch['batch_id']}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest = manifest_response.json()
    operation_ids = [operation["operation_id"] for operation in manifest["operations"]]
    staged = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/staged",
        json={"operation_ids": operation_ids},
    )
    assert staged.status_code == 200, staged.text
    results = []
    for operation in manifest["operations"]:
        result = {
            "operation_id": operation["operation_id"],
            "success": True,
            "components": {
                "pdf_source": "succeeded",
                "markdown": "succeeded",
                "bm25": "succeeded",
                "dense": "succeeded",
            },
        }
        if operation["operation_type"] == "INGEST":
            result["chunk_count"] = 4
        results.append(result)
    reported = client.post(
        f"/api/v1/jobs/batches/{batch['batch_id']}/results",
        json={"pipeline_run_id": f"run-{request_id}", "results": results},
    )
    assert reported.status_code == 200, reported.text
    return manifest


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
def test_upload_queue_and_mobile_navigation(live_server: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        _upload_files(
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
def test_duplicate_confirmation_exact_duplicate_and_queue_removal(live_server: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 760})
        _upload_files(
            page,
            live_server,
            [{"name": "revision.pdf", "mimeType": "application/pdf", "buffer": PDF_A}],
        )

        page.goto(f"{live_server}/upload")
        page.locator("#pdf-files").set_input_files(
            {"name": "revision.pdf", "mimeType": "application/pdf", "buffer": PDF_B}
        )
        expect(page.get_by_text("Possible duplicate", exact=True)).to_be_visible()
        page.get_by_label("Upload this file anyway").check()
        expect(page.get_by_text("Possible duplicate confirmed; ready to upload")).to_be_visible()
        page.get_by_role("button", name="Upload ready files").click()
        expect(page.get_by_text("Queued successfully", exact=False)).to_be_visible()

        page.goto(f"{live_server}/upload")
        page.locator("#pdf-files").set_input_files(
            {"name": "renamed.pdf", "mimeType": "application/pdf", "buffer": PDF_A}
        )
        expect(page.get_by_text("Ready to upload")).to_be_visible()
        page.get_by_role("button", name="Upload ready files").click()
        expect(page.get_by_text("Exact duplicate blocked.")).to_be_visible()
        expect(page.get_by_role("link", name="View existing document")).to_be_visible()

        page.goto(f"{live_server}/queue")
        expect(page.get_by_text("revision.pdf")).to_have_count(2)
        page.get_by_role("button", name="Remove").first.click()
        dialog = page.locator("#confirm-dialog")
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Remove from queue").click()
        expect(page.get_by_text("revision.pdf")).to_have_count(1)
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
        job_headers = {"Authorization": f"Bearer {app.state.settings.job_token.get_secret_value()}"}
        with httpx.Client(base_url=live_server, headers=job_headers) as job_client:
            ingest_manifest = _run_job_batch(job_client, "browser-ingest-job")
            assert ingest_manifest["operations"][0]["document_id"] == document_id

            def search_handler(request: httpx.Request) -> httpx.Response:
                payload = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={
                        "query": payload["query"],
                        "mode": payload["mode"],
                        "hits": [
                            {
                                "document_id": document_id,
                                "score": 0.875,
                                "snippet": "<script>alert(1)</script> retention policy",
                            }
                        ],
                    },
                )

            search_client = httpx.AsyncClient(transport=httpx.MockTransport(search_handler))
            app.state.search_http_client = search_client
            for mode in ("keyword", "semantic", "hybrid"):
                page.goto(f"{live_server}/library")
                page.locator("#library-query").fill("retention")
                page.locator("#search-mode").select_option(mode)
                page.get_by_role("button", name="Search").click()
                expect(page.get_by_text("searchable-handbook.pdf")).to_be_visible()
                expect(page.get_by_text(f"using {mode} search.", exact=False)).to_be_visible()
                expect(
                    page.get_by_text("<script>alert(1)</script> retention policy")
                ).to_be_visible()
                assert "<script>alert(1)</script>" not in page.content()

            page.goto(f"{live_server}/library")
            page.get_by_role("button", name="Delete").click()
            dialog = page.locator("#confirm-dialog")
            expect(dialog).to_be_visible()
            dialog.get_by_role("button", name="Request deletion").click()
            expect(page.get_by_text("Delete Queued", exact=True)).to_be_visible()

            deletion_manifest = _run_job_batch(job_client, "browser-delete-job")
            assert deletion_manifest["operations"][0]["operation_type"] == "DELETE"
            page.goto(f"{live_server}{document_path}")
            expect(page.get_by_text("Deleted", exact=True).first).to_be_visible()
        browser.close()
    asyncio.run(search_client.aclose())
