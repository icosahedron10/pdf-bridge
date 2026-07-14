from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.db import build_engine, build_session_factory, create_schema


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = tmp_path / "bridge-data"
    cache = tmp_path / "model-cache"
    cache.mkdir()
    return Settings(
        _env_file=None,
        app_env="test",
        auth_mode="anonymous-poc",
        storage_root=root,
        database_url=f"sqlite+pysqlite:///{(root / 'catalog.sqlite3').as_posix()}",
        session_secret=SecretStr("worker-test-session-secret-32-characters"),
        allowed_hosts=["localhost"],
        formatter_api_url="https://formatter.test",
        formatter_api_token=SecretStr("formatter-test-token"),
        formatter_model_id="formatter-model",
        formatter_model_revision="formatter-commit-1",
        formatter_tokenizer_class="TestTokenizer",
        formatter_prompt_revision="formatter-prompt-v1",
        formatter_schema_revision="formatter-schema-v1",
        llm_api_url="https://advisory.test/v1",
        llm_api_token=SecretStr("advisory-test-token"),
        llm_classifier_model="classifier-model",
        llm_classifier_model_revision="classifier-commit-1",
        llm_classifier_prompt_revision="classifier-prompt-v1",
        llm_verifier_model="verifier-model",
        llm_verifier_model_revision="verifier-commit-1",
        llm_verifier_prompt_revision="verifier-prompt-v1",
        dense_model_revision="a" * 40,
        sparse_model_revision="b" * 64,
        model_cache_dir=cache,
        qdrant_url="https://qdrant.test",
        qdrant_api_key=SecretStr("qdrant-test-token"),
        qdrant_screening_collection_name="private-screening",
        worker_enabled=False,
        collections=[
            {
                "key": "customer",
                "display_name": "Customer",
                "description": "Customer documents.",
                "audience": "customer",
                "qdrant_collection_name": "customer-active",
            }
        ],
    )


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[sessionmaker[Session]]:
    engine = build_engine(settings.database_url)
    create_schema(engine)
    factory = build_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()
