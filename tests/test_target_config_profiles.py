from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from pdf_bridge.core.config import Settings
from pdf_bridge.services.profiles import (
    build_index_profile,
    build_pipeline_profiles,
    canonical_profile_json,
)

SESSION_SECRET = SecretStr("target-test-session-secret-with-32-characters")
DENSE_REVISION = "a" * 40
SPARSE_REVISION = "b" * 64


def _jwt_segment(value: object) -> str:
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _qdrant_jwt(
    collections: list[str],
    *,
    expiration: int | None = None,
    header: object | None = None,
    payload: object | None = None,
    signature: bytes = b"s" * 32,
) -> str:
    resolved_payload = (
        payload
        if payload is not None
        else {
            "access": [
                {"collection": collection, "access": "rw"} for collection in collections
            ],
            "exp": expiration or int(time.time()) + 3_600,
        }
    )
    return ".".join(
        (
            _jwt_segment(header or {"alg": "HS256", "typ": "JWT"}),
            _jwt_segment(resolved_payload),
            _jwt_segment(signature),
        )
    )


def _collection(
    key: str = "customer",
    physical_name: str = "customer-product-pdfs",
    *,
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "key": key,
        "display_name": key.title(),
        "description": f"Documents for {key}.",
        "audience": key,
        "qdrant_collection_name": physical_name,
        "enabled": enabled,
    }


def _test_settings(tmp_path: Path, **updates: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "storage_root": tmp_path / "storage",
        "database_url": "sqlite+pysqlite:///:memory:",
        "session_secret": SESSION_SECRET,
        "allowed_hosts": ["testserver.local"],
        "collections": [_collection()],
    }
    values.update(updates)
    return Settings(**values)


def _complete_development_settings(tmp_path: Path, **updates: object) -> Settings:
    cache = tmp_path / "model-cache"
    cache.mkdir(exist_ok=True)
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "development",
        "storage_root": tmp_path / "storage",
        "session_secret": SecretStr("development-session-secret-with-32-characters"),
        "collections": [_collection()],
        "formatter_api_url": "http://formatter.internal:8000",
        "formatter_api_token": SecretStr("formatter-token-that-is-not-shared"),
        "formatter_model_id": "formatter-model",
        "formatter_model_revision": "formatter-model-2026-07-14",
        "formatter_tokenizer_class": "LlamaTokenizerFast",
        "formatter_prompt_revision": "formatter-prompt-v1",
        "formatter_schema_revision": "formatter-schema-v1",
        "llm_api_url": "http://advisory.internal:8000/v1",
        "llm_api_token": SecretStr("advisory-token-that-is-not-shared"),
        "llm_classifier_model": "duplicate-classifier",
        "llm_classifier_model_revision": "classifier-model-2026-07-14",
        "llm_classifier_prompt_revision": "classifier-prompt-v1",
        "llm_verifier_model": "duplicate-verifier",
        "llm_verifier_model_revision": "verifier-model-2026-07-14",
        "llm_verifier_prompt_revision": "verifier-prompt-v1",
        "dense_model_revision": DENSE_REVISION,
        "sparse_model_revision": SPARSE_REVISION,
        "model_cache_dir": cache,
        "qdrant_url": "http://qdrant.internal:6333",
        "qdrant_screening_collection_name": "pdf-bridge-screening",
    }
    values.update(updates)
    if "qdrant_api_key" not in updates:
        collections = values["collections"]
        assert isinstance(collections, list)
        enabled_names = [
            str(collection["qdrant_collection_name"])
            for collection in collections
            if bool(collection["enabled"])
        ]
        screening = values["qdrant_screening_collection_name"]
        assert isinstance(screening, str)
        values["qdrant_api_key"] = SecretStr(
            _qdrant_jwt([*enabled_names, screening])
        )
    return Settings(**values)


def _profile_inputs() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    content = {
        "pypdf": {"version": "6.14.2", "mode": "layout", "normalization": "NFC"},
        "formatter": {
            "model_revision": "formatter-model-2026-07-14",
            "prompt_revision": "formatter-prompt-v1",
            "schema_revision": "formatter-schema-v1",
        },
        "chunker": {"revision": "markdown-v1", "target": 320, "overlap": 48, "max": 384},
    }
    index = {
        "dense": {"model_revision": DENSE_REVISION, "dimension": 768, "normalized": True},
        "sparse": {"model_revision": SPARSE_REVISION, "idf": True},
        "vectors": ["dense", "bm25"],
        "point_schema": 2,
    }
    policy = {
        "candidate_limits": {"dense": 20, "sparse": 20},
        "thresholds": {"cosine": 0.91, "filename": 0.86},
        "classifier_prompt_revision": "classifier-prompt-v1",
        "verifier_prompt_revision": "verifier-prompt-v1",
    }
    return content, index, policy


def test_test_mode_explicitly_allows_injected_provider_omissions(tmp_path: Path) -> None:
    settings = _test_settings(tmp_path)

    assert settings.collections[0].qdrant_collection_name == "customer-product-pdfs"
    assert settings.collections[0].enabled is True
    assert settings.formatter_api_url is None
    assert settings.llm_api_url is None
    assert settings.model_cache_dir is None
    assert settings.qdrant_url is None
    assert settings.pypdf_extraction_mode == "layout"
    assert settings.worker_execution_slots == 2
    assert settings.embedding_lanes == 1
    assert settings.dense_dimension == 768
    assert settings.dense_model_id == "sentence-transformers/all-mpnet-base-v2"
    assert settings.sparse_model_id == "Qdrant/bm25"
    assert settings.sparse_idf is True


@pytest.mark.parametrize(
    "legacy_field",
    [
        "embedding_api_url",
        "embedding_api_token",
        "embedding_model_id",
        "embedding_dimension",
        "brand_primary_1",
        "theme_default",
        "collection_epoch",
        "qdrant_admin_key",
        "qdrant_admin_api_key",
        "max_upload_files",
    ],
)
def test_removed_legacy_configuration_is_rejected(
    tmp_path: Path, legacy_field: str
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _test_settings(tmp_path, **{legacy_field: "legacy-value"})


def test_collection_keys_and_physical_names_are_unique_and_screening_is_private(
    tmp_path: Path,
) -> None:
    duplicate_key = [_collection(), _collection(physical_name="other-active")]
    with pytest.raises(ValidationError, match="collection keys must be unique"):
        _test_settings(tmp_path / "keys", collections=duplicate_key)

    duplicate_physical = [_collection(), _collection("internal", "CUSTOMER-PRODUCT-PDFS")]
    with pytest.raises(ValidationError, match="qdrant_collection_name values must be unique"):
        _test_settings(tmp_path / "names", collections=duplicate_physical)

    with pytest.raises(ValidationError, match="screening collection must be distinct"):
        _test_settings(
            tmp_path / "screening",
            qdrant_screening_collection_name="customer-product-pdfs",
        )

    with pytest.raises(ValidationError, match="at least one configured collection must be enabled"):
        _test_settings(tmp_path / "disabled", collections=[_collection(enabled=False)])


def test_partial_injected_provider_configuration_still_fails(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="formatter configuration is incomplete"):
        _test_settings(tmp_path, formatter_api_url="https://formatter.test")


def test_non_test_modes_require_every_target_provider_group(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="formatter configuration is required"):
        Settings(
            _env_file=None,
            app_env="development",
            storage_root=tmp_path / "storage",
            session_secret=SecretStr("development-session-secret-with-32-characters"),
            collections=[_collection()],
        )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("formatter_api_url", "formatter configuration is incomplete"),
        ("formatter_tokenizer_class", "formatter configuration is incomplete"),
        ("llm_api_url", "advisory configuration is incomplete"),
        ("dense_model_revision", "local model configuration is incomplete"),
        ("qdrant_url", "Qdrant configuration is incomplete"),
    ],
)
def test_non_test_provider_groups_cannot_be_partially_removed(
    tmp_path: Path, field: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _complete_development_settings(tmp_path, **{field: None})


def test_complete_target_configuration_uses_documented_fields(tmp_path: Path) -> None:
    settings = _complete_development_settings(
        tmp_path,
        clamd_timeout_seconds=31,
        formatter_timeout_seconds=121,
        llm_timeout_seconds=61,
        qdrant_timeout_seconds=32,
        search_api_timeout_seconds=11,
        max_pages=1_999,
        max_extracted_characters=4_999_999,
        max_chunks=9_999,
    )

    assert settings.clamd_timeout_seconds == 31
    assert settings.formatter_timeout_seconds == 121
    assert settings.formatter_tokenizer_class == "LlamaTokenizerFast"
    assert settings.llm_timeout_seconds == 61
    assert settings.qdrant_timeout_seconds == 32
    assert settings.search_api_timeout_seconds == 11
    assert settings.max_pages == 1_999
    assert settings.max_extracted_characters == 4_999_999
    assert settings.max_chunks == 9_999
    assert settings.model_cache_dir == (tmp_path / "model-cache").resolve()
    assert (settings.storage_root / "artifacts").is_dir()


def test_non_test_qdrant_jwt_scope_is_exactly_enabled_collections_and_screening(
    tmp_path: Path,
) -> None:
    settings = _complete_development_settings(
        tmp_path,
        collections=[
            _collection(),
            _collection("internal", "internal-pdfs", enabled=False),
        ],
    )

    assert settings.qdrant_api_key is not None


def test_test_mode_allows_opaque_injected_qdrant_test_credentials(tmp_path: Path) -> None:
    settings = _test_settings(
        tmp_path,
        qdrant_url="https://qdrant.test",
        qdrant_api_key=SecretStr("opaque-test-double-credential"),
        qdrant_screening_collection_name="pdf-bridge-screening",
    )

    assert settings.qdrant_api_key is not None


@pytest.mark.parametrize(
    ("token", "message"),
    [
        ("not-a-jwt", "three-segment compact JWT"),
        (
            _qdrant_jwt(
                ["customer-product-pdfs", "pdf-bridge-screening"],
                header={"alg": "none", "typ": "JWT"},
            ),
            "alg=HS256",
        ),
        (
            _qdrant_jwt(
                [],
                payload={"access": "m", "exp": int(time.time()) + 3_600},
            ),
            "collection-scoped list",
        ),
        (
            _qdrant_jwt(["customer-product-pdfs"]),
            "exactly match enabled active collections plus screening",
        ),
        (
            _qdrant_jwt(
                [
                    "customer-product-pdfs",
                    "pdf-bridge-screening",
                    "unrelated-collection",
                ]
            ),
            "exactly match enabled active collections plus screening",
        ),
        (
            _qdrant_jwt(
                [],
                payload={
                    "access": [
                        {"collection": "customer-product-pdfs", "access": "r"},
                        {"collection": "pdf-bridge-screening", "access": "rw"},
                    ],
                    "exp": int(time.time()) + 3_600,
                },
            ),
            "exactly rw",
        ),
        (
            _qdrant_jwt(
                [
                    "customer-product-pdfs",
                    "customer-product-pdfs",
                    "pdf-bridge-screening",
                ]
            ),
            "duplicate collection grant",
        ),
        (
            _qdrant_jwt(
                ["customer-product-pdfs", "pdf-bridge-screening"],
                expiration=int(time.time()) + 30,
            ),
            "clock-skew window",
        ),
        (
            _qdrant_jwt(
                [],
                payload={
                    "access": [
                        {"collection": "customer-product-pdfs", "access": "rw"},
                        {"collection": "pdf-bridge-screening", "access": "rw"},
                    ],
                    "exp": int(time.time()) + 3_600,
                    "sub": "unexpected",
                },
            ),
            "only access and exp claims",
        ),
        (
            _qdrant_jwt(
                ["customer-product-pdfs", "pdf-bridge-screening"],
                signature=b"short",
            ),
            "32-byte HS256 signature",
        ),
    ],
)
def test_non_test_qdrant_jwt_rejects_broader_or_invalid_tokens(
    tmp_path: Path,
    token: str,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _complete_development_settings(
            tmp_path,
            qdrant_api_key=SecretStr(token),
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"formatter_api_url": "http://formatter.internal:8000/v1"}, "server root"),
        ({"llm_api_url": "http://advisory.internal:8000"}, "/v1 API root"),
    ],
)
def test_provider_urls_use_the_endpoint_roots_expected_by_the_clients(
    tmp_path: Path, updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _complete_development_settings(tmp_path, **updates)


def test_documented_environment_names_populate_target_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PDF_BRIDGE_CLAMD_TIMEOUT_SECONDS", "33")
    monkeypatch.setenv("PDF_BRIDGE_SEARCH_API_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("PDF_BRIDGE_MAX_PAGES", "123")
    monkeypatch.setenv("PDF_BRIDGE_MAX_EXTRACTED_CHARACTERS", "4567")
    monkeypatch.setenv("PDF_BRIDGE_MAX_CHUNKS", "89")

    settings = _test_settings(tmp_path)

    assert settings.clamd_timeout_seconds == 33
    assert settings.search_api_timeout_seconds == 12
    assert settings.max_pages == 123
    assert settings.max_extracted_characters == 4_567
    assert settings.max_chunks == 89


def test_dotenv_accepts_only_documented_launcher_extras(
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / "launcher.env"
    dotenv.write_text(
        "\n".join(
            (
                "PDF_BRIDGE_BIND_ADDRESS=127.0.0.1",
                "PDF_BRIDGE_PORT=8000",
                f"PDF_BRIDGE_MODEL_CACHE_HOST_PATH={tmp_path.as_posix()}",
                "PDF_BRIDGE_QDRANT_ADMIN_API_KEY=launcher-only-admin-secret",
                "PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES=5",
                "PDF_BRIDGE_STREAMLIT_BIND_ADDRESS=127.0.0.1",
                "PDF_BRIDGE_STREAMLIT_PORT=8501",
                "PDF_BRIDGE_URL=http://127.0.0.1:8000",
                "PDF_BRIDGE_STREAMLIT_IDENTITY_HEADER=X-Forwarded-User",
            )
        ),
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=dotenv,
        app_env="test",
        storage_root=tmp_path / "storage",
        database_url="sqlite+pysqlite:///:memory:",
        session_secret=SESSION_SECRET,
        allowed_hosts=["testserver.local"],
        collections=[_collection()],
    )

    assert settings.app_env == "test"


def test_dotenv_still_rejects_unknown_service_prefixed_values(tmp_path: Path) -> None:
    dotenv = tmp_path / "unknown.env"
    dotenv.write_text("PDF_BRIDGE_OPTIONAL_TYPO=true\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="pdf_bridge_optional_typo"):
        Settings(
            _env_file=dotenv,
            app_env="test",
            storage_root=tmp_path / "storage",
            database_url="sqlite+pysqlite:///:memory:",
            session_secret=SESSION_SECRET,
            allowed_hosts=["testserver.local"],
            collections=[_collection()],
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("worker_execution_slots", 3, "Input should be 2"),
        ("embedding_lanes", 2, "Input should be 1"),
        ("dense_dimension", 384, "Input should be 768"),
        ("dense_model_id", "other/model", "all-mpnet-base-v2"),
        ("sparse_model_id", "other/bm25", "Qdrant/bm25"),
        ("sparse_idf", False, "Input should be True"),
        ("model_local_files_only", False, "network fallback is forbidden"),
        ("dense_model_revision", "main", "immutable 40-64"),
        ("sparse_model_revision", "latest", "floating model"),
        ("sparse_model_revision", "b" * 63, "exact 64-character SHA-256"),
    ],
)
def test_fixed_processing_and_concurrency_contracts_cannot_drift(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _complete_development_settings(tmp_path, **{field: value})


def test_profile_hashes_are_order_independent_and_cryptographically_chained() -> None:
    content, index, policy = _profile_inputs()
    profiles = build_pipeline_profiles(
        content_inputs=content,
        index_inputs=index,
        preflight_policy_inputs=policy,
        active_qdrant_collection="customer-product-pdfs",
    )
    reordered = build_pipeline_profiles(
        content_inputs=dict(reversed(list(content.items()))),
        index_inputs=dict(reversed(list(index.items()))),
        preflight_policy_inputs=dict(reversed(list(policy.items()))),
        active_qdrant_collection="customer-product-pdfs",
    )

    assert profiles == reordered
    assert profiles.content.profile_id.startswith("sha256:")
    assert len(profiles.content.profile_id) == len("sha256:") + 64
    assert json.loads(profiles.index.canonical_json)["active_qdrant_collection"] == (
        "customer-product-pdfs"
    )
    assert json.loads(profiles.index.canonical_json)["content_profile_id"] == (
        profiles.content.profile_id
    )
    assert json.loads(profiles.preflight_policy.canonical_json)["index_profile_id"] == (
        profiles.index.profile_id
    )


def test_policy_only_change_does_not_change_content_or_index_profile() -> None:
    content, index, policy = _profile_inputs()
    initial = build_pipeline_profiles(
        content_inputs=content,
        index_inputs=index,
        preflight_policy_inputs=policy,
        active_qdrant_collection="customer-product-pdfs",
    )
    changed = build_pipeline_profiles(
        content_inputs=content,
        index_inputs=index,
        preflight_policy_inputs={**policy, "thresholds": {"cosine": 0.93, "filename": 0.86}},
        active_qdrant_collection="customer-product-pdfs",
    )

    assert changed.content.profile_id == initial.content.profile_id
    assert changed.index.profile_id == initial.index.profile_id
    assert changed.preflight_policy.profile_id != initial.preflight_policy.profile_id


def test_resolved_active_collection_changes_index_and_policy_not_content() -> None:
    content, index, policy = _profile_inputs()
    first = build_pipeline_profiles(
        content_inputs=content,
        index_inputs=index,
        preflight_policy_inputs=policy,
        active_qdrant_collection="customer-product-pdfs",
    )
    second = build_pipeline_profiles(
        content_inputs=content,
        index_inputs=index,
        preflight_policy_inputs=policy,
        active_qdrant_collection="customer-product-pdfs-v2",
    )

    assert second.content.profile_id == first.content.profile_id
    assert second.index.profile_id != first.index.profile_id
    assert second.preflight_policy.profile_id != first.preflight_policy.profile_id


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_profile_canonicalization_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError, match="non-finite float"):
        canonical_profile_json({"threshold": value})


@pytest.mark.parametrize("value", [{1, 2}, object(), Path("model")])
def test_profile_canonicalization_rejects_unknown_value_types(value: object) -> None:
    with pytest.raises(TypeError, match="unsupported profile value type"):
        canonical_profile_json({"value": value})


def test_index_profile_requires_a_valid_parent_and_resolved_collection() -> None:
    with pytest.raises(ValueError, match="content_profile_id"):
        build_index_profile(
            {"point_schema": 2},
            content_profile_id="content-v1",
            active_qdrant_collection="customer-product-pdfs",
        )
    with pytest.raises(ValueError, match="resolved physical name"):
        build_index_profile(
            {"point_schema": 2},
            content_profile_id="sha256:" + "a" * 64,
            active_qdrant_collection="../customer",
        )
