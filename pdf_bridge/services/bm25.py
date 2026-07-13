"""Deterministic client-side BM25 sparse-vector encoding for Qdrant.

Qdrant applies the IDF component server-side when a sparse vector is
configured with the IDF modifier; the client contributes the saturated
term-frequency weights. Token hashing and weighting are deterministic so
identical text always produces identical sparse vectors.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass

from pdf_bridge.services.chunking import lexical_tokens

BM25_VERSION = "bm25/v1"
BM25_K1 = 1.2
BM25_B = 0.75
BM25_AVERAGE_CHUNK_TOKENS = 256.0


@dataclass(frozen=True, slots=True)
class SparseVectorData:
    """Sparse vector payload with parallel index and value arrays."""

    indices: tuple[int, ...]
    values: tuple[float, ...]


def token_index(token: str) -> int:
    """Map a token to a stable 31-bit sparse dimension index."""

    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def bm25_document_vector(text: str) -> SparseVectorData:
    """Encode chunk text as BM25 term-frequency weights for indexing."""

    tokens = lexical_tokens(text)
    if not tokens:
        return SparseVectorData(indices=(), values=())
    counts = Counter(tokens)
    length_norm = BM25_K1 * (1.0 - BM25_B + BM25_B * (len(tokens) / BM25_AVERAGE_CHUNK_TOKENS))
    weights: dict[int, float] = {}
    for token, frequency in counts.items():
        weight = frequency * (BM25_K1 + 1.0) / (frequency + length_norm)
        index = token_index(token)
        # Rare 31-bit hash collisions merge terms; keep the larger weight so
        # the encoding stays deterministic regardless of iteration order.
        if weight > weights.get(index, 0.0):
            weights[index] = weight
    ordered = sorted(weights.items())
    return SparseVectorData(
        indices=tuple(index for index, _ in ordered),
        values=tuple(round(value, 6) for _, value in ordered),
    )


def bm25_query_vector(text: str) -> SparseVectorData:
    """Encode query text with unit weights; Qdrant supplies the IDF part."""

    indices = sorted({token_index(token) for token in lexical_tokens(text)})
    return SparseVectorData(
        indices=tuple(indices),
        values=tuple(1.0 for _ in indices),
    )
