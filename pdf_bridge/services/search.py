"""Typed, deliberately narrow integration with the external retrieval API."""

from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from pdf_bridge.contracts.schemas import SearchRequest, SearchResponse
from pdf_bridge.core.config import Settings
from pdf_bridge.services.errors import ServiceError

MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024


async def search_retrieval(
    settings: Settings,
    request: SearchRequest,
    *,
    client: httpx.AsyncClient | None = None,
) -> SearchResponse:
    configured = {collection.key for collection in settings.collections}
    unknown = [key for key in request.collections if key not in configured]
    if unknown:
        raise ServiceError(
            "Search may use only collections configured for this deployment.",
            status=422,
            code="collection-not-configured",
            title="Search collection was rejected",
        )
    if not settings.search_api_url:
        raise ServiceError(
            "Browsing remains available, but the retrieval search endpoint is not configured.",
            status=503,
            code="search-not-configured",
            title="Search is not configured",
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
        expected_groups = set(request.collections)
        actual_groups = {group.collection_key for group in result.groups}
        if request.include_hits:
            offset = (request.page - 1) * request.page_size
            invalid_hits = any(
                len(group.hits) != min(request.page_size, max(group.total - offset, 0))
                for group in result.groups
            )
        else:
            invalid_hits = any(group.hits for group in result.groups)
        if (
            result.query != request.query
            or result.mode != request.mode
            or result.language != request.language
            or actual_groups != expected_groups
            or len(result.groups) != len(request.collections)
            or invalid_hits
        ):
            raise ValueError("retrieval response did not correlate to its request")
        return result
    except httpx.RequestError as exc:
        raise ServiceError(
            "The retrieval service could not be reached. No fallback search was used.",
            status=503,
            code="search-unavailable",
            title="Search is temporarily unavailable",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ServiceError(
            "The retrieval service rejected the request. No fallback search was used.",
            status=502,
            code="search-upstream-error",
            title="Search service returned an error",
        ) from exc
    except (ValueError, ValidationError) as exc:
        raise ServiceError(
            "The retrieval service returned data that did not match the configured contract.",
            status=502,
            code="search-invalid-response",
            title="Search response was invalid",
        ) from exc
    finally:
        if owns_client:
            await active_client.aclose()
