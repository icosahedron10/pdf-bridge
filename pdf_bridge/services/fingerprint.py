"""Versioned analysis-pipeline fingerprint.

Every analysis records the fingerprint of the exact parser, chunker,
encoder, thresholds, and model configuration that produced it. Decisions are
validated against the analysis revision they reviewed, and evaluation runs
record this fingerprint alongside their dataset hash.
"""

from __future__ import annotations

import hashlib
import json

from pdf_bridge.services import bm25, candidates, chunking, classification, extraction, filenames

PIPELINE_SCHEMA_VERSION = 1


def pipeline_fingerprint(
    *,
    embedding_model_id: str | None,
    embedding_dimension: int | None,
    classifier_model: str | None,
    verifier_model: str | None,
) -> str:
    """Hash the complete analysis configuration into a stable identifier."""

    material = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "extraction": extraction.EXTRACTION_VERSION,
        "chunking": {
            "version": chunking.CHUNKING_VERSION,
            "target_tokens": chunking.TARGET_CHUNK_TOKENS,
            "overlap_tokens": chunking.OVERLAP_TOKENS,
            "hard_cap_chars": chunking.CHUNK_HARD_CAP_CHARS,
            "max_pages": chunking.MAX_PAGES,
            "max_chars": chunking.MAX_NORMALIZED_CHARS,
            "max_chunks": chunking.MAX_CHUNKS,
            "min_document_alnum": chunking.MIN_DOCUMENT_ALNUM_CHARS,
        },
        "bm25": {
            "version": bm25.BM25_VERSION,
            "k1": bm25.BM25_K1,
            "b": bm25.BM25_B,
            "average_tokens": bm25.BM25_AVERAGE_CHUNK_TOKENS,
        },
        "filenames": {
            "version": filenames.FILENAME_ANALYSIS_VERSION,
            "token_set": filenames.TOKEN_SET_SIMILARITY_THRESHOLD,
            "jaro_winkler": filenames.JARO_WINKLER_THRESHOLD,
            "family_tokens": filenames.MIN_FAMILY_SUBSTANTIVE_TOKENS,
        },
        "candidates": {
            "version": candidates.CANDIDATES_VERSION,
            "dense_top_k": candidates.DENSE_TOP_K,
            "bm25_top_k": candidates.BM25_TOP_K,
            "cosine_strong": candidates.COSINE_STRONG_THRESHOLD,
            "cosine_multi": candidates.COSINE_MULTI_THRESHOLD,
            "cosine_multi_chunks": candidates.COSINE_MULTI_MIN_CHUNKS,
            "bm25_rank": candidates.BM25_STRONG_PLACEMENT_RANK,
            "bm25_chunks": candidates.BM25_STRONG_MIN_CHUNKS,
            "rrf_k": candidates.RRF_K,
            "classified": candidates.MAX_CLASSIFIED_CANDIDATES,
        },
        "classification": {
            "version": classification.CLASSIFICATION_VERSION,
            "classifier_model": classifier_model,
            "verifier_model": verifier_model,
        },
        "embedding": {
            "model": embedding_model_id,
            "dimension": embedding_dimension,
        },
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "pl1-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
