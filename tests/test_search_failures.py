from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient


def test_unknown_search_document_fails_visibly(
    app, client: TestClient, csrf_headers: dict[str, str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": "missing",
                "mode": "hybrid",
                "hits": [
                    {
                        "document_id": "00000000-0000-0000-0000-000000000001",
                        "score": 1.0,
                        "snippet": "Unknown record",
                    }
                ],
            },
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.post(
            "/api/v1/search",
            headers=csrf_headers,
            json={"query": "missing", "mode": "hybrid", "limit": 10},
        )
        assert response.status_code == 502
        assert response.json()["code"] == "search-catalog-mismatch"
    finally:
        asyncio.run(search_client.aclose())


def test_retrieval_outage_never_falls_back(
    app, client: TestClient, csrf_headers: dict[str, str]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.post(
            "/api/v1/search",
            headers=csrf_headers,
            json={"query": "anything", "mode": "semantic", "limit": 10},
        )
        assert response.status_code == 503
        assert response.json()["code"] == "search-unavailable"
        page = client.get("/library?q=anything&mode=semantic")
        assert page.status_code == 200
        assert "No fallback search was used" in page.text
    finally:
        asyncio.run(search_client.aclose())


@pytest.mark.parametrize(
    ("response_payload", "request_payload"),
    [
        (
            {"query": "different", "mode": "hybrid", "hits": []},
            {"query": "requested", "mode": "hybrid", "limit": 10},
        ),
        (
            {"query": "requested", "mode": "keyword", "hits": []},
            {"query": "requested", "mode": "semantic", "limit": 10},
        ),
        (
            {
                "query": "requested",
                "mode": "hybrid",
                "hits": [
                    {
                        "document_id": "00000000-0000-0000-0000-000000000001",
                        "score": 1.0,
                        "snippet": "one",
                    },
                    {
                        "document_id": "00000000-0000-0000-0000-000000000001",
                        "score": 0.9,
                        "snippet": "duplicate",
                    },
                ],
            },
            {"query": "requested", "mode": "hybrid", "limit": 10},
        ),
        (
            {
                "query": "requested",
                "mode": "hybrid",
                "hits": [
                    {
                        "document_id": "00000000-0000-0000-0000-000000000001",
                        "score": float("nan"),
                        "snippet": "not finite",
                    }
                ],
            },
            {"query": "requested", "mode": "hybrid", "limit": 10},
        ),
        (
            {
                "query": "requested",
                "mode": "hybrid",
                "hits": [
                    {
                        "document_id": str(uuid.UUID(int=index + 1)),
                        "score": 1.0,
                        "snippet": "too many",
                    }
                    for index in range(2)
                ],
            },
            {"query": "requested", "mode": "hybrid", "limit": 1},
        ),
    ],
    ids=["query-mismatch", "mode-mismatch", "duplicates", "nonfinite", "over-limit"],
)
def test_retrieval_response_must_correlate_strictly(
    app,
    client: TestClient,
    csrf_headers: dict[str, str],
    response_payload: dict,
    request_payload: dict,
) -> None:
    search_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response_payload))
    )
    app.state.search_http_client = search_client
    try:
        response = client.post("/api/v1/search", headers=csrf_headers, json=request_payload)
        assert response.status_code == 502
        assert response.json()["code"] == "search-invalid-response"
    finally:
        asyncio.run(search_client.aclose())
