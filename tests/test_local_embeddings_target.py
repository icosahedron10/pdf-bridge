from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType

import pytest

from pdf_bridge.services.local_embeddings import (
    DENSE_MODEL_ID,
    SPARSE_MODEL_ID,
    LocalEmbeddingModels,
    LocalModelConfig,
    LocalModelError,
    _load_sparse,
)


class FakeTokenizer:
    def encode(
        self, text: str, *, add_special_tokens: bool, truncation: bool
    ) -> list[int]:
        assert add_special_tokens is True
        assert truncation is False
        return [0, *range(1, len(text.split()) + 1), 2]


class FakeDense:
    tokenizer = FakeTokenizer()

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def encode(self, sentences: list[str], **kwargs: object) -> list[list[float]]:
        assert kwargs["normalize_embeddings"] is True
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)
        with self.lock:
            self.active -= 1
        return [[1.0, *([0.0] * 767)] for _ in sentences]


@dataclass
class SparseResult:
    indices: list[int]
    values: list[float]


class FakeSparse:
    def __init__(self) -> None:
        self.document_calls = 0
        self.query_calls = 0

    def embed(self, documents: list[str], **kwargs: object):
        self.document_calls += 1
        return iter(SparseResult([10, 20], [0.5, 1.5]) for _ in documents)

    def query_embed(self, query: list[str] | str, **kwargs: object):
        self.query_calls += 1
        texts = [query] if isinstance(query, str) else query
        return iter(SparseResult([10, 20], [1.0, 1.0]) for _ in texts)


def config(cache: Path) -> LocalModelConfig:
    return LocalModelConfig(
        dense_model_id=DENSE_MODEL_ID,
        dense_model_revision="a" * 40,
        sparse_model_id=SPARSE_MODEL_ID,
        sparse_model_revision="b" * 64,
        cache_dir=cache,
    )


def test_models_validate_dense_and_keep_sparse_document_and_query_paths_distinct(
    tmp_path: Path,
) -> None:
    dense = FakeDense()
    sparse = FakeSparse()
    models = LocalEmbeddingModels(
        config(tmp_path.resolve()), dense_model=dense, sparse_model=sparse
    )

    assert models.count_tokens("one two three") == 5
    assert models.embed_dense(["first", "second"])[0] == (1.0, *([0.0] * 767))
    assert models.embed_sparse_documents(["document"])[0].values == (0.5, 1.5)
    assert models.embed_sparse_queries(["query"])[0].values == (1.0, 1.0)
    assert sparse.document_calls == 1
    assert sparse.query_calls == 1


def test_dense_calls_are_serialized_through_one_lane(tmp_path: Path) -> None:
    dense = FakeDense()
    models = LocalEmbeddingModels(
        config(tmp_path.resolve()), dense_model=dense, sparse_model=FakeSparse()
    )
    threads = [threading.Thread(target=models.embed_dense, args=([f"text {i}"],)) for i in range(4)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert dense.max_active == 1


def test_invalid_dense_and_sparse_outputs_fail_hard(tmp_path: Path) -> None:
    class BadDense(FakeDense):
        def encode(self, sentences: list[str], **kwargs: object) -> list[list[float]]:
            return [[0.0] * 3 for _ in sentences]

    class BadSparse(FakeSparse):
        def embed(self, documents: list[str], **kwargs: object):
            return iter([SparseResult([], [])])

    bad_dense = LocalEmbeddingModels(
        config(tmp_path.resolve()), dense_model=BadDense(), sparse_model=FakeSparse()
    )
    with pytest.raises(LocalModelError, match="768"):
        bad_dense.embed_dense(["content"])

    bad_sparse = LocalEmbeddingModels(
        config(tmp_path.resolve()), dense_model=FakeDense(), sparse_model=BadSparse()
    )
    with pytest.raises(LocalModelError, match="non-empty and correlated"):
        bad_sparse.embed_sparse_documents(["content"])


def test_wrong_model_identity_and_relative_cache_are_rejected(tmp_path: Path) -> None:
    base = config(tmp_path.resolve())
    with pytest.raises(LocalModelError, match="dense model"):
        LocalEmbeddingModels(
            LocalModelConfig(
                dense_model_id="other/model",
                dense_model_revision=base.dense_model_revision,
                sparse_model_id=base.sparse_model_id,
                sparse_model_revision=base.sparse_model_revision,
                cache_dir=base.cache_dir,
            ),
            dense_model=FakeDense(),
            sparse_model=FakeSparse(),
        )

    with pytest.raises(LocalModelError, match="network fallback is forbidden"):
        LocalEmbeddingModels(
            LocalModelConfig(
                dense_model_id=base.dense_model_id,
                dense_model_revision=base.dense_model_revision,
                sparse_model_id=base.sparse_model_id,
                sparse_model_revision=base.sparse_model_revision,
                cache_dir=base.cache_dir,
                local_files_only=False,
            ),
            dense_model=FakeDense(),
            sparse_model=FakeSparse(),
        )
    with pytest.raises(LocalModelError, match="absolute"):
        LocalEmbeddingModels(
            LocalModelConfig(
                dense_model_id=base.dense_model_id,
                dense_model_revision=base.dense_model_revision,
                sparse_model_id=base.sparse_model_id,
                sparse_model_revision=base.sparse_model_revision,
                cache_dir=Path("relative"),
            ),
            dense_model=FakeDense(),
            sparse_model=FakeSparse(),
        )
    with pytest.raises(LocalModelError, match="40-64 character hashes"):
        LocalEmbeddingModels(
            LocalModelConfig(
                dense_model_id=base.dense_model_id,
                dense_model_revision=base.dense_model_revision,
                sparse_model_id=base.sparse_model_id,
                sparse_model_revision="../untrusted-cache-path",
                cache_dir=base.cache_dir,
            ),
            dense_model=FakeDense(),
            sparse_model=FakeSparse(),
        )


def test_sparse_loader_selects_the_exact_preseeded_revision_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path.resolve()
    assets = {"english.txt": b"a\nthe\n", "tokenizer.json": b"{}\n"}
    manifest = {
        "version": 1,
        "model": SPARSE_MODEL_ID,
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in assets.items()
        },
    }
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    revision = hashlib.sha256(manifest_bytes).hexdigest()
    model_config = replace(config(cache), sparse_model_revision=revision)
    revision_path = (
        model_config.cache_dir / "fastembed" / model_config.sparse_model_revision
    )
    revision_path.mkdir(parents=True)
    (revision_path / "manifest.json").write_bytes(manifest_bytes)
    for name, content in assets.items():
        (revision_path / name).write_bytes(content)
    captured: dict[str, object] = {}

    fastembed = ModuleType("fastembed")

    def sparse_text_embedding(**kwargs: object) -> FakeSparse:
        captured.update(kwargs)
        return FakeSparse()

    fastembed.SparseTextEmbedding = sparse_text_embedding  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "fastembed", fastembed)

    loaded = _load_sparse(model_config)

    assert isinstance(loaded, FakeSparse)
    assert captured["local_files_only"] is True
    assert captured["specific_model_path"] == str(revision_path)


def test_sparse_loader_fails_when_the_pinned_revision_is_absent(tmp_path: Path) -> None:
    with pytest.raises(LocalModelError, match="revision directory is unavailable"):
        _load_sparse(config(tmp_path.resolve()))

    model_config = config(tmp_path.resolve())
    (model_config.cache_dir / "fastembed" / model_config.sparse_model_revision).mkdir(
        parents=True
    )
    with pytest.raises(LocalModelError, match="manifest is unavailable"):
        _load_sparse(model_config)


def test_sparse_loader_rejects_manifest_and_asset_tampering(
    tmp_path: Path,
) -> None:
    cache = tmp_path.resolve()
    content = b"a\nthe\n"
    manifest = {
        "version": 1,
        "model": SPARSE_MODEL_ID,
        "files": {"english.txt": hashlib.sha256(content).hexdigest()},
    }
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    revision = hashlib.sha256(manifest_bytes).hexdigest()
    model_config = replace(config(cache), sparse_model_revision=revision)
    revision_path = cache / "fastembed" / revision
    revision_path.mkdir(parents=True)
    (revision_path / "manifest.json").write_bytes(manifest_bytes)
    (revision_path / "english.txt").write_bytes(b"tampered")

    with pytest.raises(LocalModelError, match="asset hash does not match"):
        _load_sparse(model_config)

    (revision_path / "english.txt").write_bytes(content)
    (revision_path / "unexpected.bin").write_bytes(b"not declared")
    with pytest.raises(LocalModelError, match="inventory does not match"):
        _load_sparse(model_config)
