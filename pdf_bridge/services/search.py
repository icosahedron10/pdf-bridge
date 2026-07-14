"""Strict operator-only proxy for the separately owned retrieval service."""

from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from pdf_bridge.contracts.schemas import OperatorSearchRequest, OperatorSearchResponse
from pdf_bridge.core.config import Settings
from pdf_bridge.services.errors import ServiceError

MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024


def search_retrieval(
    settings: Settings,
    request: OperatorSearchRequest,
    *,
    client: httpx.Client,
) -> OperatorSearchResponse:
    """Forward one bounded collection-scoped query and validate its wire result."""

    definition = next(
        (
            collection
            for collection in settings.collections
            if collection.key == request.collection_key and collection.enabled
        ),
        None,
    )
    if definition is None:
        raise ServiceError(
            "The requested collection is not available.",
            status=404,
            code="collection_not_found",
            title="Collection not found",
        )
    if settings.search_api_url is None or settings.search_api_token is None:
        raise ServiceError(
            "The operator retrieval integration is not configured.",
            status=503,
            code="search_not_configured",
            title="Search is unavailable",
        )

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {settings.search_api_token.get_secret_value()}",
    }
    try:
        with client.stream(
            "POST",
            f"{settings.search_api_url.rstrip('/')}/search",
            json=request.model_dump(mode="json"),
            headers=headers,
            timeout=settings.search_api_timeout_seconds,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_SEARCH_RESPONSE_BYTES:
                    raise ValueError("retrieval response exceeded the safety limit")
        result = OperatorSearchResponse.model_validate(json.loads(body))
        if (
            result.collection_key != request.collection_key
            or result.query != request.query
            or result.mode is not request.mode
            or len(result.results) > request.limit
        ):
            raise ValueError("retrieval response did not correlate to its request")
        return result
    except httpx.RequestError as exc:
        raise ServiceError(
            "The retrieval service could not be reached; no fallback was used.",
            status=503,
            code="search_unavailable",
            title="Search is unavailable",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ServiceError(
            "The retrieval service rejected the query; no fallback was used.",
            status=502,
            code="search_upstream_error",
            title="Search upstream failed",
        ) from exc
    except (ValueError, ValidationError, UnicodeDecodeError) as exc:
        raise ServiceError(
            "The retrieval service returned an invalid response.",
            status=502,
            code="search_invalid_response",
            title="Search response was invalid",
        ) from exc
