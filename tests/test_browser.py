from __future__ import annotations

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


def _run_job_batch(
    client: httpx.Client,
    request_id: str,
) -> dict:
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
        result: dict[str, object] = {
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
        _select_collection(page, "customer")
        page.locator("#pdf-files").set_input_files(
            {"name": "revision.pdf", "mimeType": "application/pdf", "buffer": PDF_B}
        )
        expect(page.get_by_text("Possible duplicate", exact=True)).to_be_visible()
        page.get_by_label("Upload this file anyway").check()
        expect(page.get_by_text("Possible duplicate confirmed; ready to upload")).to_be_visible()
        page.get_by_role("button", name="Upload ready files").click()
        expect(page.get_by_text("Queued successfully", exact=False)).to_be_visible()

        page.goto(f"{live_server}/upload")
        _select_collection(page, "customer")
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
def test_language_and_review_controls_are_absent(app, live_server: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 820})
        [document_path] = _upload_files(
            page,
            live_server,
            [{"name": "quebec-policy.pdf", "mimeType": "application/pdf", "buffer": PDF_A}],
            collection="internal",
        )
        page.goto(f"{live_server}/queue")
        expect(page.get_by_role("link", name="Needs review")).to_have_count(0)
        expect(page.get_by_label("Language")).to_have_count(0)
        expect(page.get_by_role("columnheader", name="Language")).to_have_count(0)

        job_headers = {"Authorization": f"Bearer {app.state.settings.job_token.get_secret_value()}"}
        with httpx.Client(base_url=live_server, headers=job_headers) as job_client:
            _run_job_batch(job_client, "browser-collection-only-job")

        page.goto(f"{live_server}{document_path}")
        expect(page.get_by_text("Classification", exact=False)).to_have_count(0)
        review_response = page.goto(f"{live_server}/review")
        assert review_response is not None and review_response.status == 404
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
            assert ingest_manifest["version"] == 2
            assert ingest_manifest["operations"][0]["document_id"] == document_id
            assert ingest_manifest["operations"][0]["collection_key"] == "customer"
            assert ingest_manifest["operations"][0]["relative_path"].startswith(
                "pdfs/customer/"
            )

            def search_handler(request: httpx.Request) -> httpx.Response:
                payload = json.loads(request.content)
                assert "language" not in payload
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
                                        "snippet": (
                                            "<script>alert(1)</script> retention policy"
                                        ),
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
                expect(
                    page.locator(
                        ".collection-entry--internal .collection-entry__search-count strong"
                    )
                ).to_have_text("0")
                customer_entry = page.locator("article").filter(has_text="Customer Product")
                customer_entry.get_by_role("link", name="View matches").click()
                expect(page).to_have_url(
                    f"{live_server}/library/customer?q=retention&mode={mode}"
                )
                expect(page.get_by_text("searchable-handbook.pdf")).to_be_visible()
                expect(page.get_by_text(f"using {mode} search.", exact=False)).to_be_visible()
                expect(
                    page.get_by_text("<script>alert(1)</script> retention policy")
                ).to_be_visible()
                assert "<script>alert(1)</script>" not in page.content()

            forged_response = page.goto(f"{live_server}/library/internal?q=retention")
            assert forged_response is not None and forged_response.status == 502
            boundary_error = page.get_by_text(
                "No partial results or fallback search were shown"
            )
            expect(boundary_error).to_be_visible()
            expect(page.get_by_text("searchable-handbook.pdf")).to_have_count(0)

            page.goto(f"{live_server}/library/customer")
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
    search_client.close()
