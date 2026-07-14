from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

STREAMLIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STREAMLIT_ROOT))

from bridge_client import BridgeClient, BridgeProblem, BridgeUnreachable  # noqa: E402


def _json_response(
    request: httpx.Request,
    payload: dict[str, object],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers, request=request)


def test_upload_bootstraps_cookie_session_and_csrf_from_authenticated_get() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            assert request.url.path == "/api/v2/collections"
            return _json_response(
                request,
                {"items": [], "limit": 1, "next_cursor": None, "has_more": False},
                headers={
                    "X-CSRF-Token": "csrf-from-get",
                    "Set-Cookie": "bridge_session=signed; Path=/; HttpOnly",
                },
            )
        assert request.method == "POST"
        assert request.url.path == "/api/v2/collections/customer/documents"
        assert request.headers["X-CSRF-Token"] == "csrf-from-get"
        assert request.headers["Idempotency-Key"] == "upload-key-123"
        assert "bridge_session=signed" in request.headers["Cookie"]
        assert b'name="file"' in request.content
        assert b'name="collection_key"' not in request.content
        return _json_response(request, {"document": {"id": "document"}, "operation": {}})

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))
    result = client.upload(
        "customer",
        filename="guide.pdf",
        content=b"%PDF-1.7",
        idempotency_key="upload-key-123",
    )

    assert result["document"] == {"id": "document"}
    assert client.csrf_token == "csrf-from-get"
    assert len(requests) == 2


def test_async_mutations_require_idempotency_and_use_document_resources() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v2/collections":
            return _json_response(
                request,
                {"items": [], "limit": 1, "next_cursor": None, "has_more": False},
                headers={"X-CSRF-Token": "csrf"},
            )
        return _json_response(request, {"document": {}, "operation": {}})

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="idempotency_key"):
        client.retry("doc-id", idempotency_key="")

    client.decide(
        "doc-id",
        prepared_revision_id="revision-id",
        action="REPLACE",
        target_document_id="old-id",
        idempotency_key="decision-key",
    )
    client.retry("doc-id", idempotency_key="retry-key")
    client.delete("doc-id", idempotency_key="delete-key")

    mutation_requests = requests[1:]
    assert [(request.method, request.url.path) for request in mutation_requests] == [
        ("POST", "/api/v2/documents/doc-id/decision"),
        ("POST", "/api/v2/documents/doc-id/retry"),
        ("DELETE", "/api/v2/documents/doc-id"),
    ]
    assert [request.headers["Idempotency-Key"] for request in mutation_requests] == [
        "decision-key",
        "retry-key",
        "delete-key",
    ]
    assert json.loads(mutation_requests[0].content) == {
        "prepared_revision_id": "revision-id",
        "action": "REPLACE",
        "target_document_id": "old-id",
    }


def test_cursor_and_inspection_endpoints_use_v2_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(request, {"items": [], "has_more": False})

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))
    client.collections(cursor="opaque", limit=7)
    client.collection("customer")
    client.documents("customer", state="READY", cursor="next", limit=9)
    client.document("doc")
    client.markdown("doc")
    client.chunks("doc", cursor="chunks", limit=11)
    client.preflight("doc", cursor="evidence", limit=13)
    client.events("doc", cursor="events", limit=15)
    client.operation("op")
    client.history(
        collection_key="customer",
        disposition="DELETED",
        cursor="history",
        limit=17,
    )

    assert [request.url.path for request in requests] == [
        "/api/v2/collections",
        "/api/v2/collections/customer",
        "/api/v2/collections/customer/documents",
        "/api/v2/documents/doc",
        "/api/v2/documents/doc/markdown",
        "/api/v2/documents/doc/chunks",
        "/api/v2/documents/doc/preflight",
        "/api/v2/documents/doc/events",
        "/api/v2/operations/op",
        "/api/v2/history",
    ]
    assert dict(requests[2].url.params) == {
        "limit": "9",
        "cursor": "next",
        "state": "READY",
    }
    assert dict(requests[-1].url.params) == {
        "limit": "17",
        "cursor": "history",
        "collection_key": "customer",
        "disposition": "DELETED",
    }


def test_operation_metrics_reads_content_free_v2_queue_aggregates() -> None:
    expected = {
        "generated_at": "2026-07-13T15:00:00Z",
        "total": 1,
        "queued": 1,
        "running": 0,
        "failed": 0,
        "oldest_queued_age_seconds": 90.0,
        "buckets": [
            {
                "operation_type": "PREFLIGHT",
                "state": "QUEUED",
                "phase": "CHECKING_ELIGIBILITY",
                "count": 1,
                "oldest_operation_age_seconds": 90.0,
                "oldest_phase_age_seconds": 30.0,
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v2/operations/metrics"
        return _json_response(request, expected)

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))

    assert client.operation_metrics() == expected


def test_nested_error_contract_is_sanitized_and_typed() -> None:
    request_id = "2fd5c8b8-2ed2-4d50-bec4-160bc0b26d89"

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            request,
            {
                "error": {
                    "code": "artifact_not_ready",
                    "message": "Markdown is not ready.",
                    "request_id": request_id,
                    "retryable": True,
                }
            },
            status=409,
        )

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))
    with pytest.raises(BridgeProblem) as raised:
        client.markdown("doc")

    assert raised.value.status == 409
    assert raised.value.code == "artifact_not_ready"
    assert raised.value.message == "Markdown is not ready."
    assert raised.value.request_id == request_id
    assert raised.value.retryable is True


def test_filename_advisory_and_search_are_csrf_protected_without_async_keys() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return _json_response(
                request,
                {"items": [], "limit": 1, "next_cursor": None, "has_more": False},
                headers={"X-CSRF-Token": "csrf"},
            )
        return _json_response(request, {"matches": [], "results": []})

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))
    client.name_check("customer", filename="guide.pdf")
    client.search(
        collection_key="customer",
        query="installation",
        mode="hybrid",
        limit=12,
    )

    advisory, search = requests[1:]
    assert advisory.url.path == "/api/v2/collections/customer/name-check"
    assert search.url.path == "/api/v2/operator/search"
    assert advisory.headers["X-CSRF-Token"] == "csrf"
    assert search.headers["X-CSRF-Token"] == "csrf"
    assert "Idempotency-Key" not in advisory.headers
    assert "Idempotency-Key" not in search.headers


def test_source_and_degraded_readiness_preserve_non_json_transport_semantics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/health/ready":
            return _json_response(
                request,
                {
                    "status": "NOT_READY",
                    "checks": [
                        {
                            "component": "qdrant",
                            "status": "NOT_READY",
                            "failure_code": "qdrant_unavailable",
                        }
                    ],
                },
                status=503,
            )
        assert request.url.path == "/api/v2/documents/doc/source"
        return httpx.Response(
            200,
            content=b"%PDF-1.7 source",
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": "inline; filename*=UTF-8''operator%20guide.pdf",
            },
            request=request,
        )

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))

    assert client.health("ready")["status"] == "NOT_READY"
    content, filename, content_type = client.source("doc")
    assert content == b"%PDF-1.7 source"
    assert filename == "operator guide.pdf"
    assert content_type == "application/pdf"


def test_source_download_parses_plain_quoted_ascii_filenames() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/documents/doc/source"
        return httpx.Response(
            200,
            content=b"%PDF-1.7 source",
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": 'inline; filename="quarterly-report.pdf"',
            },
            request=request,
        )

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))

    _content, filename, _content_type = client.source("doc")
    assert filename == "quarterly-report.pdf"


def test_source_download_falls_back_to_document_id_without_a_parseable_filename() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.7 source",
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": "inline",
            },
            request=request,
        )

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))

    _content, filename, _content_type = client.source("doc")
    assert filename == "doc.pdf"


def test_expired_csrf_session_refreshes_once_with_the_same_idempotency_key() -> None:
    requests: list[httpx.Request] = []
    bootstrap_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal bootstrap_count
        requests.append(request)
        if request.method == "GET":
            bootstrap_count += 1
            return _json_response(
                request,
                {"items": [], "limit": 1, "next_cursor": None, "has_more": False},
                headers={"X-CSRF-Token": f"csrf-{bootstrap_count}"},
            )
        if len([item for item in requests if item.method == "POST"]) == 1:
            return _json_response(
                request,
                {
                    "error": {
                        "code": "csrf-check-failed",
                        "message": "Refresh the session.",
                        "request_id": "2fd5c8b8-2ed2-4d50-bec4-160bc0b26d89",
                        "retryable": True,
                    }
                },
                status=403,
            )
        return _json_response(request, {"document": {}, "operation": {}})

    client = BridgeClient("https://bridge.test", transport=httpx.MockTransport(handler))
    client.retry("doc", idempotency_key="retry-stable-key")

    posts = [request for request in requests if request.method == "POST"]
    assert bootstrap_count == 2
    assert [request.headers["X-CSRF-Token"] for request in posts] == [
        "csrf-1",
        "csrf-2",
    ]
    assert [request.headers["Idempotency-Key"] for request in posts] == [
        "retry-stable-key",
        "retry-stable-key",
    ]


def test_proxy_identity_is_forwarded_on_the_fixed_bridge_session() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Forwarded-User"] == "operator@example.test"
        return _json_response(
            request,
            {"items": [], "limit": 1, "next_cursor": None, "has_more": False},
            headers={"X-CSRF-Token": "csrf"},
        )

    client = BridgeClient(
        "https://bridge.test",
        identity_header_name="X-Forwarded-User",
        identity="operator@example.test",
        transport=httpx.MockTransport(handler),
    )

    client.collections(limit=1)


@pytest.mark.parametrize(
    "base_url",
    [
        "bridge.test",
        "file:///etc/passwd",
        "https://user:secret@bridge.test",
        "https://bridge.test/internal",
        "https://bridge.test?next=internal",
    ],
)
def test_bridge_base_url_must_be_a_deployment_owned_server_root(base_url: str) -> None:
    with pytest.raises(ValueError, match=r"HTTP\(S\) server root"):
        BridgeClient(base_url)


def test_redirects_are_refused_without_following_the_location() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://attacker.example/collect"},
            request=request,
        )

    client = BridgeClient(
        "https://bridge.test",
        identity_header_name="X-Forwarded-User",
        identity="operator@example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BridgeUnreachable, match="redirects are forbidden"):
        client.collections(limit=1)
    assert [request.url.host for request in requests] == ["bridge.test"]
