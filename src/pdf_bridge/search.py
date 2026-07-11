"""Typed, deliberately narrow integration with the external retrieval API."""

from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from pdf_bridge.config import Settings
from pdf_bridge.problems import ProblemError
from pdf_bridge.schemas import SearchRequest, SearchResponse

MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024


async def search_retrieval(
    settings: Settings,
    request: SearchRequest,
    *,
    client: httpx.AsyncClient | None = None,
) -> SearchResponse:
    if not settings.search_api_url:
        raise ProblemError(
            status=503,
            code="search-not-configured",
            title="Search is not configured",
            detail=(
                "Browsing remains available, but the retrieval search endpoint is not configured."
            ),
        )

    headers = {"Accept": "application/json"}
    if settings.search_api_token:
        headers["Authorization"] = f"Bearer {settings.search_api_token.get_secret_value()}"

    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=settings.search_api_timeout)
    try:
        async with active_client.stream(
            "POST",
            f"{settings.search_api_url.rstrip('/')}/search",
            json=request.model_dump(mode="json"),
            headers=headers,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_SEARCH_RESPONSE_BYTES:
                    raise ValueError("retrieval response exceeded the configured limit")
        result = SearchResponse.model_validate(json.loads(body))
        if (
            result.query != request.query
            or result.mode != request.mode
            or len(result.hits) > request.limit
        ):
            raise ValueError("retrieval response did not correlate to its request")
        return result
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ProblemError(
            status=503,
            code="search-unavailable",
            title="Search is temporarily unavailable",
            detail="The retrieval service could not be reached. No fallback search was used.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ProblemError(
            status=502,
            code="search-upstream-error",
            title="Search service returned an error",
            detail="The retrieval service rejected the request. No fallback search was used.",
        ) from exc
    except (ValueError, ValidationError) as exc:
        raise ProblemError(
            status=502,
            code="search-invalid-response",
            title="Search response was invalid",
            detail=(
                "The retrieval service returned data that did not match the configured contract."
            ),
        ) from exc
    finally:
        if owns_client:
            await active_client.aclose()
