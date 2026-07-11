from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from litestar.testing import TestClient


def _request(query: str, mode: str = "hybrid", page_size: int = 10) -> dict:
    return {
        "query": query,
        "mode": mode,
        "collections": ["customer"],
        "include_hits": True,
        "page_size": page_size,
    }


def _response(query: str, mode: str, hits: list[dict], *, total: int | None = None) -> dict:
    return {
        "query": query,
        "mode": mode,
        "groups": [
            {
                "collection_key": "customer",
                "total": len(hits) if total is None else total,
                "hits": hits,
            }
        ],
    }


def test_unknown_search_document_fails_visibly(
    app, client: TestClient, csrf_headers: dict[str, str]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_response(
                "missing",
                "hybrid",
                [
                    {
                        "document_id": "00000000-0000-0000-0000-000000000001",
                        "score": 1.0,
                        "snippet": "Unknown record",
                    }
                ],
            ),
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.post(
            "/api/v1/search",
            headers=csrf_headers,
            json=_request("missing"),
        )
        assert response.status_code == 502
        assert response.json()["code"] == "search-catalog-mismatch"
    finally:
        asyncio.run(search_client.aclose())


@pytest.mark.parametrize(
    "failure_type",
    [httpx.ConnectError, httpx.RemoteProtocolError, httpx.DecodingError],
    ids=["connect", "remote-protocol", "decoding"],
)
def test_retrieval_outage_never_falls_back(
    app,
    client: TestClient,
    csrf_headers: dict[str, str],
    failure_type: type[httpx.RequestError],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise failure_type("offline")

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.search_http_client = search_client
    try:
        response = client.post(
            "/api/v1/search",
            headers=csrf_headers,
            json=_request("anything", "semantic"),
        )
        assert response.status_code == 503
        assert response.json()["code"] == "search-unavailable"
        page = client.get("/library?q=anything&mode=semantic")
        assert page.status_code == 503
        assert "No fallback search was used" in page.text
    finally:
        asyncio.run(search_client.aclose())


@pytest.mark.parametrize(
    ("response_payload", "request_payload"),
    [
        (_response("different", "hybrid", []), _request("requested")),
        (_response("requested", "keyword", []), _request("requested", "semantic")),
        (
            _response(
                "requested",
                "hybrid",
                [
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
            ),
            _request("requested"),
        ),
        (
            _response(
                "requested",
                "hybrid",
                [
                    {
                        "document_id": "00000000-0000-0000-0000-000000000001",
                        "score": float("nan"),
                        "snippet": "not finite",
                    }
                ],
            ),
            _request("requested"),
        ),
        (
            _response(
                "requested",
                "hybrid",
                [
                    {
                        "document_id": str(uuid.UUID(int=index + 1)),
                        "score": 1.0,
                        "snippet": "too many",
                    }
                    for index in range(2)
                ],
            ),
            _request("requested", page_size=1),
        ),
        (
            _response("requested", "hybrid", [], total=1),
            _request("requested"),
        ),
        (
            {
                "query": "requested",
                "mode": "hybrid",
                "groups": [
                    {"collection_key": "customer", "total": "0", "hits": []}
                ],
            },
            _request("requested"),
        ),
        (
            {
                "query": "requested",
                "mode": "hybrid",
                "groups": [
                    {"collection_key": "customer ", "total": 0, "hits": []}
                ],
            },
            _request("requested"),
        ),
        (
            _response(
                "requested",
                "hybrid",
                [
                    {
                        "document_id": "00000000-0000-0000-0000-000000000001",
                        "score": 1.0,
                        "snippet": "impossible second page",
                    }
                ],
                total=1,
            ),
            {**_request("requested"), "page": 2},
        ),
    ],
    ids=[
        "query-mismatch",
        "mode-mismatch",
        "duplicates",
        "nonfinite",
        "over-limit",
        "missing-page-hits",
        "coerced-total",
        "trimmed-collection",
        "impossible-page",
    ],
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
