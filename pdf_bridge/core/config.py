"""Strict target configuration for PDF Bridge.

Deployment configuration owns collection topology and every provider identity.
The service never invents a Qdrant name, downloads an unpinned model in
production, or falls back to a legacy embedding or collection-admin boundary.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

APP_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_SESSION_SECRET = "development-only-change-me"

HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
COLLECTION_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
QDRANT_COLLECTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,254}$")
COMMIT_REVISION = re.compile(r"^[0-9A-Fa-f]{40,64}$")
SHA256_REVISION = re.compile(r"^[0-9A-Fa-f]{64}$")
JWT_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
FLOATING_REVISIONS = frozenset({"dev", "head", "latest", "main", "master", "stable", "trunk"})
_LAUNCHER_ONLY_DOTENV_KEYS = frozenset(
    {
        "pdf_bridge_bind_address",
        "pdf_bridge_port",
        "pdf_bridge_model_cache_host_path",
        "pdf_bridge_qdrant_admin_api_key",
        "pdf_bridge_streamlit_max_upload_files",
        "pdf_bridge_streamlit_bind_address",
        "pdf_bridge_streamlit_port",
        "pdf_bridge_url",
        "pdf_bridge_streamlit_identity_header",
    }
)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _normalized_optional(value: object) -> object:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _validate_http_url(field_name: str, value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL without secrets")
    return value.rstrip("/")


def _secret_value(field_name: str, value: SecretStr) -> str:
    secret = value.get_secret_value()
    if not secret or secret != secret.strip():
        raise ValueError(f"{field_name} must not be blank or padded with whitespace")
    if any(character.isspace() or ord(character) < 32 for character in secret):
        raise ValueError(f"{field_name} must not contain whitespace or control characters")
    if "change_me" in secret.casefold() or "placeholder" in secret.casefold():
        raise ValueError(f"{field_name} must not contain a placeholder credential")
    return secret


def _missing_group_fields(values: dict[str, object]) -> list[str]:
    return [name for name, value in values.items() if value is None]


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member is forbidden: {key}")
        result[key] = value
    return result


def _decode_jwt_segment(name: str, encoded: str, *, maximum_bytes: int) -> bytes:
    if not encoded or not JWT_SEGMENT.fullmatch(encoded):
        raise ValueError(f"qdrant_api_key has an invalid {name} base64url segment")
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"qdrant_api_key has an invalid {name} base64url segment") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != encoded:
        raise ValueError(f"qdrant_api_key has a non-canonical {name} base64url segment")
    if len(decoded) > maximum_bytes:
        raise ValueError(f"qdrant_api_key {name} segment exceeds its size limit")
    return decoded


def _decode_jwt_json(name: str, encoded: str) -> dict[str, Any]:
    raw = _decode_jwt_segment(name, encoded, maximum_bytes=8_192)
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"qdrant_api_key {name} must be a strict UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"qdrant_api_key {name} must be a JSON object")
    return value


def _validate_qdrant_access_token(
    token: str,
    *,
    allowed_collections: frozenset[str],
) -> None:
    """Validate the untrusted shape and least-privilege claims of a Qdrant JWT.

    Bridge intentionally does not receive Qdrant's HS256 signing key. Qdrant therefore remains
    the signature authority and readiness proves that the supplied token is authentic.
    """

    if len(token) > 16_384:
        raise ValueError("qdrant_api_key JWT exceeds the size limit")
    segments = token.split(".")
    if len(segments) != 3:
        raise ValueError("qdrant_api_key must be a three-segment compact JWT")
    header = _decode_jwt_json("header", segments[0])
    payload = _decode_jwt_json("payload", segments[1])
    signature = _decode_jwt_segment("signature", segments[2], maximum_bytes=64)

    if header != {"alg": "HS256", "typ": "JWT"}:
        raise ValueError("qdrant_api_key JWT header must contain only alg=HS256 and typ=JWT")
    if len(signature) != 32:
        raise ValueError("qdrant_api_key JWT must carry a 32-byte HS256 signature")
    if set(payload) != {"access", "exp"}:
        raise ValueError("qdrant_api_key JWT payload must contain only access and exp claims")

    expiration = payload["exp"]
    if type(expiration) is not int or expiration > 2**63 - 1:
        raise ValueError("qdrant_api_key JWT exp must be a bounded Unix timestamp integer")
    if expiration <= int(time.time()) + 30:
        raise ValueError("qdrant_api_key JWT must remain valid beyond Qdrant's clock-skew window")

    access = payload["access"]
    if not isinstance(access, list):
        raise ValueError("qdrant_api_key JWT access must be a collection-scoped list")
    granted: set[str] = set()
    for rule in access:
        if not isinstance(rule, dict) or set(rule) != {"collection", "access"}:
            raise ValueError(
                "qdrant_api_key JWT access entries must contain only collection and access"
            )
        collection = rule["collection"]
        permission = rule["access"]
        if not isinstance(collection, str) or not QDRANT_COLLECTION_NAME.fullmatch(collection):
            raise ValueError("qdrant_api_key JWT contains an invalid collection name")
        if permission != "rw":
            raise ValueError("qdrant_api_key JWT must grant exactly rw collection access")
        if collection in granted:
            raise ValueError("qdrant_api_key JWT contains a duplicate collection grant")
        granted.add(collection)
    if granted != allowed_collections:
        raise ValueError(
            "qdrant_api_key JWT collection grants must exactly match enabled active "
            "collections plus screening"
        )


class CollectionDefinition(BaseModel):
    """One deployment-owned logical-to-physical collection mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(max_length=63)
    display_name: str = Field(max_length=255)
    description: str = Field(max_length=2_000)
    audience: str = Field(max_length=63)
    qdrant_collection_name: str = Field(max_length=255)
    enabled: bool = True

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not COLLECTION_KEY.fullmatch(value):
            raise ValueError(
                "collection key must contain only lowercase letters, numbers, hyphens, "
                "and underscores, and must start with a letter or number"
            )
        return value

    @field_validator("display_name", "description", "audience")
    @classmethod
    def validate_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("collection display metadata cannot be blank")
        if any(ord(character) < 32 for character in stripped):
            raise ValueError("collection display metadata cannot contain control characters")
        return stripped

    @field_validator("qdrant_collection_name")
    @classmethod
    def validate_qdrant_name(cls, value: str) -> str:
        if not QDRANT_COLLECTION_NAME.fullmatch(value):
            raise ValueError("qdrant_collection_name must be a safe fixed physical name")
        return value


class Settings(BaseSettings):
    """Environment-backed target settings for the API and worker."""

    model_config = SettingsConfigDict(
        env_prefix="PDF_BRIDGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
        frozen=True,
        validate_default=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Keep dotenv strict while allowing documented launcher-only variables."""

        def launcher_aware_dotenv() -> dict[str, object]:
            values = dict(dotenv_settings())
            for key in _LAUNCHER_ONLY_DOTENV_KEYS:
                values.pop(key, None)
            return values

        return (
            init_settings,
            env_settings,
            launcher_aware_dotenv,
            file_secret_settings,
        )

    app_env: Literal["development", "test", "enterprise"] = "development"
    auth_mode: Literal["anonymous-poc", "trusted-header"] = "anonymous-poc"

    storage_root: Path
    database_url: str = ""
    collections: tuple[CollectionDefinition, ...] = Field(min_length=1, max_length=50)

    session_secret: SecretStr
    allowed_hosts: tuple[str, ...] = ()
    trusted_proxy_cidrs: tuple[str, ...] = ()
    trusted_identity_header: str = "X-Forwarded-User"

    max_upload_bytes: int = 50 * 1024 * 1024
    upload_chunk_bytes: int = 1024 * 1024

    clamd_host: str = "clamav"
    clamd_port: int = 3310
    clamd_timeout_seconds: float = 30.0
    clamd_stream_max_bytes: int = 64 * 1024 * 1024

    pypdf_extraction_mode: Literal["layout"] = "layout"
    parse_wall_clock_seconds: float = 120.0
    parse_cpu_seconds: int = 90
    parse_memory_bytes: int = 1024 * 1024 * 1024
    max_pages: int = 2_000
    max_extracted_characters: int = 5_000_000
    max_chunks: int = 10_000

    formatter_api_url: str | None = None
    formatter_api_token: SecretStr | None = None
    formatter_model_id: str | None = Field(default=None, max_length=255)
    formatter_model_revision: str | None = Field(default=None, max_length=255)
    formatter_tokenizer_class: str | None = Field(default=None, max_length=255)
    formatter_prompt_revision: str | None = Field(default=None, max_length=255)
    formatter_schema_revision: str | None = Field(default=None, max_length=255)
    formatter_timeout_seconds: float = 120.0
    formatter_max_input_tokens: int = 24_000
    formatter_max_output_tokens: int = 12_000
    formatter_token_safety_reserve: int = 512
    formatter_max_pages_per_request: int = 8
    formatter_max_attempts: int = 2

    llm_api_url: str | None = None
    llm_api_token: SecretStr | None = None
    llm_classifier_model: str | None = Field(default=None, max_length=255)
    llm_classifier_model_revision: str | None = Field(default=None, max_length=255)
    llm_classifier_prompt_revision: str | None = Field(default=None, max_length=255)
    llm_verifier_model: str | None = Field(default=None, max_length=255)
    llm_verifier_model_revision: str | None = Field(default=None, max_length=255)
    llm_verifier_prompt_revision: str | None = Field(default=None, max_length=255)
    llm_timeout_seconds: float = 60.0
    llm_max_input_tokens: int = 12_000
    llm_max_output_tokens: int = 2_048
    llm_max_attempts: int = 2

    dense_model_id: Literal["sentence-transformers/all-mpnet-base-v2"] = (
        "sentence-transformers/all-mpnet-base-v2"
    )
    dense_model_revision: str | None = None
    model_cache_dir: Path | None = None
    model_local_files_only: bool = True
    dense_device: str = Field(default="cpu", max_length=64)
    dense_batch_size: int = 16
    dense_dimension: Literal[768] = 768
    embedding_lanes: Literal[1] = 1

    sparse_model_id: Literal["Qdrant/bm25"] = "Qdrant/bm25"
    sparse_model_revision: str | None = None
    sparse_idf: Literal[True] = True

    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    qdrant_timeout_seconds: float = 30.0
    qdrant_screening_collection_name: str | None = Field(default=None, max_length=255)

    worker_enabled: bool = True
    worker_execution_slots: Literal[2] = 2
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 300
    worker_heartbeat_seconds: int = 30
    worker_max_operation_seconds: int = 3_600

    search_api_url: str | None = None
    search_api_token: SecretStr | None = None
    search_api_timeout_seconds: float = 10.0

    @field_validator(
        "formatter_api_url",
        "formatter_model_id",
        "formatter_model_revision",
        "formatter_tokenizer_class",
        "formatter_prompt_revision",
        "formatter_schema_revision",
        "llm_api_url",
        "llm_classifier_model",
        "llm_classifier_model_revision",
        "llm_classifier_prompt_revision",
        "llm_verifier_model",
        "llm_verifier_model_revision",
        "llm_verifier_prompt_revision",
        "dense_model_revision",
        "sparse_model_revision",
        "qdrant_url",
        "qdrant_screening_collection_name",
        "search_api_url",
        mode="before",
    )
    @classmethod
    def normalize_optional_strings(cls, value: object) -> object:
        return _normalized_optional(value)

    @field_validator(
        "formatter_api_token",
        "llm_api_token",
        "qdrant_api_key",
        "search_api_token",
        mode="before",
    )
    @classmethod
    def normalize_optional_secrets(cls, value: object) -> object:
        return _normalized_optional(value)

    @field_validator(
        "formatter_model_id",
        "formatter_model_revision",
        "formatter_tokenizer_class",
        "formatter_prompt_revision",
        "formatter_schema_revision",
        "llm_classifier_model",
        "llm_classifier_model_revision",
        "llm_classifier_prompt_revision",
        "llm_verifier_model",
        "llm_verifier_model_revision",
        "llm_verifier_prompt_revision",
        "sparse_model_revision",
    )
    @classmethod
    def validate_immutable_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not IDENTITY.fullmatch(value):
            raise ValueError("model, prompt, and schema identities must be bounded stable tokens")
        if value.casefold() in FLOATING_REVISIONS:
            raise ValueError("floating model, prompt, and schema revisions are forbidden")
        return value

    @field_validator("dense_model_revision")
    @classmethod
    def validate_dense_commit_revision(cls, value: str | None) -> str | None:
        if value is not None and not COMMIT_REVISION.fullmatch(value):
            raise ValueError(
                "dense model revision must be an immutable 40-64 character commit hash"
            )
        return value.casefold() if value is not None else None

    @field_validator("sparse_model_revision")
    @classmethod
    def validate_sparse_manifest_revision(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_REVISION.fullmatch(value):
            raise ValueError("sparse model revision must be an exact 64-character SHA-256")
        return value.casefold() if value is not None else None

    @field_validator("qdrant_screening_collection_name")
    @classmethod
    def validate_screening_name(cls, value: str | None) -> str | None:
        if value is not None and not QDRANT_COLLECTION_NAME.fullmatch(value):
            raise ValueError("qdrant_screening_collection_name must be a safe fixed physical name")
        return value

    @model_validator(mode="after")
    def validate_and_prepare(self) -> Settings:
        """Validate all cross-field invariants before creating storage paths."""

        self._validate_collections()
        self._validate_storage_and_database()
        self._validate_security()
        self._validate_limits()
        self._validate_provider_groups()
        self._prepare_storage()
        return self

    def _validate_collections(self) -> None:
        keys = [collection.key for collection in self.collections]
        if len(keys) != len(set(keys)):
            raise ValueError("collection keys must be unique")
        physical_names = [
            collection.qdrant_collection_name.casefold() for collection in self.collections
        ]
        if len(physical_names) != len(set(physical_names)):
            raise ValueError("qdrant_collection_name values must be unique")
        if not any(collection.enabled for collection in self.collections):
            raise ValueError("at least one configured collection must be enabled")
        screening = self.qdrant_screening_collection_name
        if screening and screening.casefold() in physical_names:
            raise ValueError(
                "the screening collection must be distinct from every active collection"
            )

    def _validate_storage_and_database(self) -> None:
        configured_root = self.storage_root.expanduser()
        if not configured_root.is_absolute():
            raise ValueError("storage_root must be an absolute path")
        root = configured_root.resolve(strict=False)
        app_root = APP_ROOT.resolve(strict=False)
        if _is_relative_to(root, app_root):
            raise ValueError("storage_root must be outside the application source tree")
        if any(part.casefold().startswith("onedrive") for part in root.parts):
            raise ValueError("storage_root must not be inside a OneDrive-synchronized path")
        object.__setattr__(self, "storage_root", root)

        database_url_value = self.database_url
        if not database_url_value:
            database_path = (root / "catalog.sqlite3").as_posix()
            database_url_value = f"sqlite+pysqlite:///{database_path}"
            object.__setattr__(self, "database_url", database_url_value)
        try:
            database_url = make_url(database_url_value)
        except ArgumentError as exc:
            raise ValueError("database_url is not a valid SQLAlchemy URL") from exc
        if database_url.get_backend_name() != "sqlite":
            raise ValueError("the target service supports only a SQLite catalog")
        database_name = database_url.database
        if database_name in {None, "", ":memory:"}:
            if self.app_env != "test":
                raise ValueError("in-memory SQLite is supported only during tests")
            return
        database_path = Path(database_name).expanduser()
        if not database_path.is_absolute():
            raise ValueError("SQLite database_url must use an absolute file path")
        if not _is_relative_to(database_path.resolve(strict=False), root):
            raise ValueError("the SQLite database file must be beneath storage_root")

    def _validate_security(self) -> None:
        session_secret = _secret_value("session_secret", self.session_secret)
        if len(session_secret) < 32:
            raise ValueError("session_secret must be at least 32 characters")
        if self.app_env != "test" and session_secret == DEVELOPMENT_SESSION_SECRET:
            raise ValueError("set a unique session_secret before starting PDF Bridge")

        hosts = self.allowed_hosts
        if not hosts and self.app_env == "development":
            hosts = ("localhost", "127.0.0.1")
            object.__setattr__(self, "allowed_hosts", hosts)
        if not hosts or any(not host.strip() or host != host.strip() for host in hosts):
            raise ValueError("allowed_hosts must contain at least one non-blank host")
        if self.app_env != "development" and "*" in hosts:
            raise ValueError("non-development deployments must not allow every Host header")

        if not HTTP_HEADER_NAME.fullmatch(self.trusted_identity_header):
            raise ValueError("trusted_identity_header must be a valid HTTP header name")
        for network in self.trusted_proxy_cidrs:
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"trusted_proxy_cidrs contains an invalid network: {network}"
                ) from exc
        if self.auth_mode == "trusted-header" and not self.trusted_proxy_cidrs:
            raise ValueError("trusted-header authentication requires trusted_proxy_cidrs")
        if self.app_env == "enterprise" and self.auth_mode != "trusted-header":
            raise ValueError("enterprise deployments must use trusted-header authentication")

    def _validate_limits(self) -> None:
        if not self.clamd_host.strip():
            raise ValueError("clamd_host cannot be blank")
        if not 1 <= self.clamd_port <= 65_535:
            raise ValueError("clamd_port must be a valid TCP port")
        if self.max_upload_bytes <= 0 or self.upload_chunk_bytes <= 0:
            raise ValueError("upload byte limits must be positive")
        if self.clamd_stream_max_bytes <= 0:
            raise ValueError("clamd_stream_max_bytes must be positive")
        if self.max_upload_bytes > self.clamd_stream_max_bytes:
            raise ValueError("max_upload_bytes must not exceed clamd_stream_max_bytes")

        positive_timeouts = {
            "clamd_timeout_seconds": self.clamd_timeout_seconds,
            "parse_wall_clock_seconds": self.parse_wall_clock_seconds,
            "parse_cpu_seconds": self.parse_cpu_seconds,
            "formatter_timeout_seconds": self.formatter_timeout_seconds,
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "qdrant_timeout_seconds": self.qdrant_timeout_seconds,
            "search_api_timeout_seconds": self.search_api_timeout_seconds,
            "worker_poll_seconds": self.worker_poll_seconds,
        }
        if any(value <= 0 for value in positive_timeouts.values()):
            raise ValueError("service timeouts must be positive")
        if self.parse_memory_bytes < 64 * 1024 * 1024:
            raise ValueError("parse_memory_bytes must allow at least 64 MiB")
        if min(self.max_pages, self.max_extracted_characters, self.max_chunks) <= 0:
            raise ValueError("extraction and chunk safety caps must be positive")

        formatter_limits = (
            self.formatter_max_input_tokens,
            self.formatter_max_output_tokens,
            self.formatter_token_safety_reserve,
            self.formatter_max_pages_per_request,
            self.formatter_max_attempts,
        )
        llm_limits = (
            self.llm_max_input_tokens,
            self.llm_max_output_tokens,
            self.llm_max_attempts,
        )
        if min(*formatter_limits, *llm_limits) <= 0:
            raise ValueError("formatter and advisory request limits must be positive")
        if self.formatter_max_pages_per_request > self.max_pages:
            raise ValueError("formatter_max_pages_per_request cannot exceed max_pages")
        if not 1 <= self.formatter_max_attempts <= 5 or not 1 <= self.llm_max_attempts <= 5:
            raise ValueError("model attempt limits must be between 1 and 5")
        if not 1 <= self.dense_batch_size <= 128:
            raise ValueError("dense_batch_size must be between 1 and 128")
        if not self.model_local_files_only:
            raise ValueError("local model network fallback is forbidden")
        if not IDENTITY.fullmatch(self.dense_device) or self.dense_device.casefold() == "auto":
            raise ValueError("dense_device must name one explicit local device")

        if not 10 <= self.worker_lease_seconds <= 3_600:
            raise ValueError("worker_lease_seconds must be between 10 and 3600")
        if not 1 <= self.worker_heartbeat_seconds < self.worker_lease_seconds:
            raise ValueError("worker_heartbeat_seconds must be shorter than the lease")
        if self.worker_max_operation_seconds <= self.worker_lease_seconds:
            raise ValueError("worker_max_operation_seconds must exceed worker_lease_seconds")

    def _validate_provider_groups(self) -> None:
        formatter = {
            "formatter_api_url": self.formatter_api_url,
            "formatter_api_token": self.formatter_api_token,
            "formatter_model_id": self.formatter_model_id,
            "formatter_model_revision": self.formatter_model_revision,
            "formatter_tokenizer_class": self.formatter_tokenizer_class,
            "formatter_prompt_revision": self.formatter_prompt_revision,
            "formatter_schema_revision": self.formatter_schema_revision,
        }
        advisory = {
            "llm_api_url": self.llm_api_url,
            "llm_api_token": self.llm_api_token,
            "llm_classifier_model": self.llm_classifier_model,
            "llm_classifier_model_revision": self.llm_classifier_model_revision,
            "llm_classifier_prompt_revision": self.llm_classifier_prompt_revision,
            "llm_verifier_model": self.llm_verifier_model,
            "llm_verifier_model_revision": self.llm_verifier_model_revision,
            "llm_verifier_prompt_revision": self.llm_verifier_prompt_revision,
        }
        local_models = {
            "dense_model_revision": self.dense_model_revision,
            "sparse_model_revision": self.sparse_model_revision,
            "model_cache_dir": self.model_cache_dir,
        }
        qdrant = {
            "qdrant_url": self.qdrant_url,
            "qdrant_api_key": self.qdrant_api_key,
            "qdrant_screening_collection_name": self.qdrant_screening_collection_name,
        }

        for label, values in (
            ("formatter", formatter),
            ("advisory", advisory),
            ("local model", local_models),
            ("Qdrant", qdrant),
        ):
            missing = _missing_group_fields(values)
            supplied = len(missing) != len(values)
            if supplied and missing:
                raise ValueError(
                    f"{label} configuration is incomplete; missing: {', '.join(missing)}"
                )
            if self.app_env != "test" and missing:
                raise ValueError(f"{label} configuration is required outside test mode")

        for field_name in ("formatter_api_url", "llm_api_url", "qdrant_url", "search_api_url"):
            value = getattr(self, field_name)
            if value is None:
                continue
            normalized = _validate_http_url(field_name, value)
            if self.app_env == "enterprise" and urlsplit(normalized).scheme != "https":
                raise ValueError(f"enterprise {field_name} must use HTTPS")
            object.__setattr__(self, field_name, normalized)

        if self.formatter_api_url is not None and urlsplit(
            self.formatter_api_url
        ).path.rstrip("/").endswith("/v1"):
            raise ValueError(
                "formatter_api_url must be the vLLM server root, without a trailing /v1"
            )
        if self.llm_api_url is not None and not urlsplit(
            self.llm_api_url
        ).path.rstrip("/").endswith("/v1"):
            raise ValueError("llm_api_url must be the OpenAI-compatible /v1 API root")

        configured_secrets: dict[str, str] = {
            "session_secret": _secret_value("session_secret", self.session_secret)
        }
        for field_name in (
            "formatter_api_token",
            "llm_api_token",
            "qdrant_api_key",
            "search_api_token",
        ):
            value = getattr(self, field_name)
            if value is not None:
                configured_secrets[field_name] = _secret_value(field_name, value)
        if len(configured_secrets.values()) != len(set(configured_secrets.values())):
            raise ValueError("session and provider credentials must all be distinct")

        if self.app_env != "test" and self.qdrant_api_key is not None:
            screening = self.qdrant_screening_collection_name
            if screening is None:
                raise ValueError("Qdrant screening collection is required outside test mode")
            allowed_collections = frozenset(
                [
                    *(
                        collection.qdrant_collection_name
                        for collection in self.collections
                        if collection.enabled
                    ),
                    screening,
                ]
            )
            _validate_qdrant_access_token(
                configured_secrets["qdrant_api_key"],
                allowed_collections=allowed_collections,
            )

        if self.search_api_url is None and self.search_api_token is not None:
            raise ValueError("search_api_token cannot be set without search_api_url")
        if self.search_api_url is not None and self.search_api_token is None:
            raise ValueError("search_api_token is required when search_api_url is configured")

        cache = self.model_cache_dir
        if cache is not None:
            expanded = cache.expanduser()
            if not expanded.is_absolute():
                raise ValueError("model_cache_dir must be an absolute path")
            resolved = expanded.resolve(strict=False)
            if not resolved.is_dir():
                raise ValueError("model_cache_dir must be an existing directory")
            object.__setattr__(self, "model_cache_dir", resolved)

    def _prepare_storage(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        for directory in ("objects", "artifacts", "temporary", "quarantine"):
            (self.storage_root / directory).mkdir(mode=0o700, exist_ok=True)

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the validated process-wide target settings."""

    return Settings()


def clear_settings_cache() -> None:
    """Clear cached settings for isolated tests."""

    get_settings.cache_clear()
