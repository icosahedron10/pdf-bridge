"""Dense embedding generation via a private OpenAI-compatible endpoint.

The embedding provider is configured, never discovered: model ID and vector
dimension are deployment settings, and any response that does not correlate
exactly with its request — count, order, dimension, or finiteness — is an
error rather than a best-effort result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import httpx

MAX_EMBEDDING_BATCH = 64


class EmbeddingError(RuntimeError):
    """The embedding provider was unavailable or returned an invalid response."""


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """Deployment-pinned embedding endpoint, model, and dimension."""

    api_url: str
    model_id: str
    dimension: int
    api_token: str | None = None
    timeout: float = 30.0


def embed_texts(
    config: EmbeddingConfig,
    texts: list[str],
    *,
    client: httpx.Client,
) -> list[list[float]]:
    """Embed texts in order, validating correlation, dimension, and finiteness."""

    if not texts:
        return []
    vectors: list[list[float]] = []
    for start in range(0, len(texts), MAX_EMBEDDING_BATCH):
        batch = texts[start : start + MAX_EMBEDDING_BATCH]
        vectors.extend(_embed_batch(config, batch, client=client))
    return vectors


def _embed_batch(
    config: EmbeddingConfig,
    batch: list[str],
    *,
    client: httpx.Client,
) -> list[list[float]]:
    headers = {"Accept": "application/json"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    try:
        response = client.post(
            f"{config.api_url.rstrip('/')}/embeddings",
            json={"model": config.model_id, "input": batch},
            headers=headers,
            timeout=config.timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"embedding request failed: {exc}") from exc
    except ValueError as exc:
        raise EmbeddingError("embedding response was not valid JSON") from exc

    if not isinstance(payload, dict):
        raise EmbeddingError("embedding response was not a JSON object")
    if payload.get("model") not in {None, config.model_id}:
        raise EmbeddingError("embedding response reported a different model than configured")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != len(batch):
        raise EmbeddingError("embedding response did not contain exactly one vector per input")
    vectors: list[list[float]] = [[] for _ in batch]
    seen: set[int] = set()
    for item in data:
        if not isinstance(item, dict):
            raise EmbeddingError("embedding response contained a malformed item")
        index = item.get("index")
        vector = item.get("embedding")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index in seen
            or not 0 <= index < len(batch)
        ):
            raise EmbeddingError("embedding response indexes did not correlate to inputs")
        seen.add(index)
        if not isinstance(vector, list) or len(vector) != config.dimension:
            raise EmbeddingError(
                f"embedding vector did not match the configured {config.dimension} dimensions"
            )
        values: list[float] = []
        for component in vector:
            if (
                isinstance(component, bool)
                or not isinstance(component, (int, float))
                or not math.isfinite(component)
            ):
                raise EmbeddingError("embedding vector contained a non-finite component")
            values.append(float(component))
        vectors[index] = values
    return vectors
