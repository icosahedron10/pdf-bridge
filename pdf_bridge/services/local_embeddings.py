"""Pinned process-local dense and sparse embedding providers.

Models are loaded once, remain private to the Bridge process, and never fall
back to a network provider.  All MPNet work passes through one semaphore so a
two-slot worker cannot overlap the service's largest memory operation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DENSE_MODEL_ID = "sentence-transformers/all-mpnet-base-v2"
DENSE_DIMENSION = 768
SPARSE_MODEL_ID = "Qdrant/bm25"
_PINNED_REVISION = re.compile(r"^[0-9A-Fa-f]{40,64}$")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_SPARSE_MANIFEST = "manifest.json"


class LocalModelError(RuntimeError):
    """A required pinned model is unavailable or returned invalid output."""


@dataclass(frozen=True, slots=True)
class SparseVector:
    """Validated Qdrant-compatible sparse coordinates."""

    indices: tuple[int, ...]
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class LocalModelConfig:
    """Immutable identities and local loading options for both model families."""

    dense_model_id: str
    dense_model_revision: str
    sparse_model_id: str
    sparse_model_revision: str
    cache_dir: Path
    local_files_only: bool = True
    device: str = "cpu"
    dense_batch_size: int = 16


class DenseTokenizer(Protocol):
    def encode(
        self,
        text: str,
        *,
        add_special_tokens: bool,
        truncation: bool,
    ) -> Sequence[int]: ...


class DenseModel(Protocol):
    tokenizer: DenseTokenizer

    def encode(self, sentences: list[str], **kwargs: Any) -> Any: ...


class SparseEmbeddingResult(Protocol):
    indices: Any
    values: Any


class SparseModel(Protocol):
    def embed(self, documents: list[str], **kwargs: Any) -> Iterable[SparseEmbeddingResult]: ...

    def query_embed(
        self, query: list[str] | str, **kwargs: Any
    ) -> Iterable[SparseEmbeddingResult]: ...


def _load_dense(config: LocalModelConfig) -> DenseModel:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - dependency packaging guard
        raise LocalModelError("sentence-transformers is not installed") from exc
    try:
        return SentenceTransformer(
            config.dense_model_id,
            device=config.device,
            cache_folder=str(config.cache_dir / "sentence-transformers"),
            trust_remote_code=False,
            revision=config.dense_model_revision,
            local_files_only=config.local_files_only,
        )
    except Exception as exc:
        raise LocalModelError("the pinned MPNet model could not be loaded locally") from exc


def _verify_sparse_assets(config: LocalModelConfig, revision_path: Path) -> None:
    """Attest every sparse asset against the hash-bound local manifest."""

    manifest_path = revision_path / _SPARSE_MANIFEST
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise LocalModelError("the pinned FastEmbed BM25 manifest is unavailable")
        if not 0 < manifest_path.stat().st_size <= 1024 * 1024:
            raise LocalModelError("the pinned FastEmbed BM25 manifest size is invalid")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except LocalModelError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise LocalModelError("the pinned FastEmbed BM25 manifest is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"version", "model", "files"}
        or manifest.get("version") != 1
        or manifest.get("model") != config.sparse_model_id
        or not isinstance(manifest.get("files"), dict)
        or not manifest["files"]
        or len(manifest["files"]) > 1_000
    ):
        raise LocalModelError("the pinned FastEmbed BM25 manifest schema is invalid")
    canonical = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != config.sparse_model_revision.casefold():
        raise LocalModelError("the pinned FastEmbed BM25 manifest identity does not match")

    declared: dict[str, str] = {}
    for raw_name, raw_digest in manifest["files"].items():
        if (
            not isinstance(raw_name, str)
            or not raw_name
            or "\\" in raw_name
            or raw_name.startswith("/")
            or any(part in {"", ".", ".."} for part in raw_name.split("/"))
            or not isinstance(raw_digest, str)
            or not _SHA256.fullmatch(raw_digest)
        ):
            raise LocalModelError("the pinned FastEmbed BM25 manifest entry is invalid")
        declared[raw_name] = raw_digest.casefold()

    actual: dict[str, Path] = {}
    try:
        for path in revision_path.rglob("*"):
            relative = path.relative_to(revision_path).as_posix()
            if relative == _SPARSE_MANIFEST:
                continue
            if path.is_symlink() or not path.is_file():
                raise LocalModelError(
                    "the pinned FastEmbed BM25 directory contains an unsafe asset"
                )
            actual[relative] = path
    except OSError as exc:
        raise LocalModelError("the pinned FastEmbed BM25 assets could not be inspected") from exc
    if set(actual) != set(declared):
        raise LocalModelError("the pinned FastEmbed BM25 asset inventory does not match")
    for relative, path in actual.items():
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise LocalModelError(
                "the pinned FastEmbed BM25 asset could not be read"
            ) from exc
        if digest.hexdigest() != declared[relative]:
            raise LocalModelError("the pinned FastEmbed BM25 asset hash does not match")

    english_stopwords = actual.get("english.txt")
    try:
        stopwords_ready = (
            english_stopwords is not None and english_stopwords.stat().st_size > 0
        )
    except OSError as exc:
        raise LocalModelError(
            "the pinned FastEmbed BM25 assets could not be inspected"
        ) from exc
    if not stopwords_ready:
        raise LocalModelError(
            "the pinned FastEmbed BM25 revision is missing English stopword assets"
        )


def _load_sparse(config: LocalModelConfig) -> SparseModel:
    revision_path = config.cache_dir / "fastembed" / config.sparse_model_revision
    if not revision_path.is_dir():
        raise LocalModelError(
            "the pinned FastEmbed BM25 revision directory is unavailable locally"
        )
    _verify_sparse_assets(config, revision_path)
    try:
        from fastembed import SparseTextEmbedding
    except ImportError as exc:  # pragma: no cover - dependency packaging guard
        raise LocalModelError("fastembed is not installed") from exc
    try:
        return SparseTextEmbedding(
            model_name=config.sparse_model_id,
            cache_dir=str(config.cache_dir / "fastembed"),
            local_files_only=True,
            specific_model_path=str(revision_path),
        )
    except Exception as exc:
        raise LocalModelError(
            "the pinned FastEmbed BM25 assets could not be loaded locally"
        ) from exc


def _coerce_dense(rows: Any, expected: int) -> list[tuple[float, ...]]:
    if hasattr(rows, "tolist"):
        rows = rows.tolist()
    if not isinstance(rows, list) or len(rows) != expected:
        raise LocalModelError("MPNet returned the wrong number of embeddings")
    vectors: list[tuple[float, ...]] = []
    for row in rows:
        if hasattr(row, "tolist"):
            row = row.tolist()
        if not isinstance(row, list) or len(row) != DENSE_DIMENSION:
            raise LocalModelError("MPNet must return exactly 768 dimensions")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in row
        ):
            raise LocalModelError("MPNet returned a non-finite vector")
        vector = tuple(float(value) for value in row)
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
            raise LocalModelError("MPNet returned a vector that is not normalized")
        vectors.append(vector)
    return vectors


def _coerce_sparse(result: SparseEmbeddingResult) -> SparseVector:
    raw_indices = result.indices.tolist() if hasattr(result.indices, "tolist") else result.indices
    raw_values = result.values.tolist() if hasattr(result.values, "tolist") else result.values
    try:
        indices = tuple(int(value) for value in raw_indices)
        values = tuple(float(value) for value in raw_values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LocalModelError("BM25 returned unreadable sparse coordinates") from exc
    if not indices or len(indices) != len(values):
        raise LocalModelError("BM25 sparse indices and values must be non-empty and correlated")
    if any(value < 0 for value in indices) or len(set(indices)) != len(indices):
        raise LocalModelError("BM25 sparse indices must be unique non-negative integers")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise LocalModelError("BM25 sparse values must be finite and non-negative")
    return SparseVector(indices=indices, values=values)


class LocalEmbeddingModels:
    """Lifecycle owner for one pinned MPNet model and one FastEmbed BM25 model."""

    def __init__(
        self,
        config: LocalModelConfig,
        *,
        dense_model: DenseModel | None = None,
        sparse_model: SparseModel | None = None,
    ) -> None:
        if config.dense_model_id != DENSE_MODEL_ID:
            raise LocalModelError(f"dense model must be {DENSE_MODEL_ID}")
        if config.sparse_model_id != SPARSE_MODEL_ID:
            raise LocalModelError(f"sparse model must be {SPARSE_MODEL_ID}")
        if not _PINNED_REVISION.fullmatch(
            config.dense_model_revision
        ) or not _PINNED_REVISION.fullmatch(config.sparse_model_revision):
            raise LocalModelError("local model revisions must be 40-64 character hashes")
        if not config.local_files_only:
            raise LocalModelError("local model network fallback is forbidden")
        if config.dense_batch_size <= 0:
            raise LocalModelError("dense batch size must be positive")
        if not config.cache_dir.is_absolute():
            raise LocalModelError("model cache directory must be absolute")
        self.config = config
        self._dense = dense_model or _load_dense(config)
        self._sparse = sparse_model or _load_sparse(config)
        self._dense_lane = threading.BoundedSemaphore(value=1)

    def count_tokens(self, text: str) -> int:
        """Count exact MPNet wordpieces including model special tokens."""

        try:
            tokens = self._dense.tokenizer.encode(
                text,
                add_special_tokens=True,
                truncation=False,
            )
        except Exception as exc:
            raise LocalModelError("the MPNet tokenizer failed") from exc
        count = len(tokens)
        if count <= 0:
            raise LocalModelError("the MPNet tokenizer returned no tokens")
        return count

    def embed_dense(self, texts: list[str]) -> list[tuple[float, ...]]:
        """Encode normalized 768-dimensional document vectors in one memory lane."""

        if not texts or any(not text.strip() for text in texts):
            raise LocalModelError("dense embedding input must contain non-empty text")
        try:
            with self._dense_lane:
                rows = self._dense.encode(
                    texts,
                    batch_size=self.config.dense_batch_size,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                )
        except LocalModelError:
            raise
        except Exception as exc:
            raise LocalModelError("MPNet document encoding failed") from exc
        return _coerce_dense(rows, len(texts))

    def embed_sparse_documents(self, texts: list[str]) -> list[SparseVector]:
        """Use FastEmbed's document encoding for stored chunks."""

        if not texts or any(not text.strip() for text in texts):
            raise LocalModelError("sparse embedding input must contain non-empty text")
        try:
            results = list(self._sparse.embed(texts))
        except Exception as exc:
            raise LocalModelError("BM25 document encoding failed") from exc
        if len(results) != len(texts):
            raise LocalModelError("BM25 returned the wrong number of document vectors")
        return [_coerce_sparse(result) for result in results]

    def embed_sparse_queries(self, texts: list[str]) -> list[SparseVector]:
        """Use the distinct FastEmbed query encoding for candidate searches."""

        if not texts or any(not text.strip() for text in texts):
            raise LocalModelError("sparse query input must contain non-empty text")
        try:
            results = list(self._sparse.query_embed(texts))
        except Exception as exc:
            raise LocalModelError("BM25 query encoding failed") from exc
        if len(results) != len(texts):
            raise LocalModelError("BM25 returned the wrong number of query vectors")
        return [_coerce_sparse(result) for result in results]

    def validate_ready(self) -> None:
        """Run bounded smoke checks used by startup/readiness."""

        probe = "PDF Bridge local model readiness probe."
        self.embed_dense([probe])
        self.embed_sparse_documents([probe])
        self.embed_sparse_queries([probe])
