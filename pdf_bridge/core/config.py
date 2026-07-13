"""Application configuration.

The storage root is deliberately required.  PDF Bridge must never fall back to
putting uploaded documents beside the source tree (or in a synchronized
OneDrive folder) just because an environment variable was missed.
"""

from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

APP_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_SESSION_SECRET = "development-only-change-me"
HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
COLLECTION_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
BRAND_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


class CollectionDefinition(BaseModel):
    """Deployment-owned metadata for one isolated retrieval collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(max_length=63)
    display_name: str = Field(max_length=255)
    description: str = Field(max_length=2_000)
    audience: Literal["customer", "internal"]

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        """Require a stable lowercase collection identifier."""

        if not COLLECTION_KEY.fullmatch(value):
            raise ValueError(
                "collection key must contain only lowercase letters, numbers, hyphens, "
                "and underscores, and must start with a letter or number"
            )
        return value

    @field_validator("display_name", "description")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """Trim collection copy and reject blank display text."""

        stripped = value.strip()
        if not stripped:
            raise ValueError("collection display name and description cannot be blank")
        return stripped


class Settings(BaseSettings):
    """Environment-backed settings for the API and command-line tools."""

    model_config = SettingsConfigDict(
        env_prefix="PDF_BRIDGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "enterprise"] = "development"
    auth_mode: Literal["anonymous-poc", "trusted-header"] = "anonymous-poc"

    brand_primary_1: str = "#173f34"
    brand_primary_2: str = "#0f3028"
    brand_secondary_1: str = "#d5a846"
    brand_secondary_2: str = "#d9c78f"
    theme_default: Literal["system", "light", "dark"] = "system"

    storage_root: Path
    database_url: str = ""
    collections: list[CollectionDefinition] = Field(min_length=1, max_length=50)

    session_secret: SecretStr = SecretStr(DEVELOPMENT_SESSION_SECRET)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    trusted_identity_header: str = "X-Forwarded-User"

    max_upload_bytes: int = 50 * 1024 * 1024
    max_upload_files: int = 20
    upload_chunk_bytes: int = 1024 * 1024

    clamd_host: str = "clamav"
    clamd_port: int = 3310
    clamd_timeout: float = 30.0
    clamd_stream_max_bytes: int = 64 * 1024 * 1024

    search_api_url: str | None = None
    search_api_token: SecretStr | None = None
    search_api_timeout: float = 10.0

    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 300
    worker_heartbeat_seconds: int = 30

    parse_wall_clock_seconds: float = 120.0
    parse_cpu_seconds: int = 90
    parse_memory_bytes: int = 1024 * 1024 * 1024
    analysis_max_pages: int = 2_000
    analysis_max_characters: int = 5_000_000
    analysis_max_chunks: int = 10_000

    embedding_api_url: str | None = None
    embedding_api_token: SecretStr | None = None
    embedding_model_id: str | None = None
    embedding_dimension: int | None = None
    embedding_timeout: float = 30.0

    llm_api_url: str | None = None
    llm_api_token: SecretStr | None = None
    llm_classifier_model: str | None = None
    llm_verifier_model: str | None = None
    llm_timeout: float = 60.0

    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    qdrant_timeout: float = 30.0

    @field_validator(
        "brand_primary_1",
        "brand_primary_2",
        "brand_secondary_1",
        "brand_secondary_2",
        mode="before",
    )
    @classmethod
    def validate_brand_color(cls, value: object) -> str:
        """Require brand colors in six-digit hexadecimal notation."""

        if not isinstance(value, str) or not BRAND_COLOR.fullmatch(value):
            raise ValueError("brand colors must use strict six-digit #RRGGBB hexadecimal values")
        return value

    @model_validator(mode="after")
    def validate_and_prepare(self) -> Settings:
        """Validate cross-field security constraints and prepare storage directories."""

        collection_keys = [collection.key for collection in self.collections]
        if len(collection_keys) != len(set(collection_keys)):
            raise ValueError("collection keys must be unique")

        root = self.storage_root.expanduser().resolve(strict=False)
        app_root = APP_ROOT.resolve(strict=False)

        if _is_relative_to(root, app_root):
            raise ValueError("storage_root must be outside the application source tree")
        if any(part.casefold().startswith("onedrive") for part in root.parts):
            raise ValueError("storage_root must not be inside a OneDrive-synchronized path")

        self.storage_root = root

        if not self.database_url:
            database_path = (root / "catalog.sqlite3").as_posix()
            self.database_url = f"sqlite+pysqlite:///{database_path}"
        try:
            database_url = make_url(self.database_url)
        except ArgumentError as exc:
            raise ValueError("database_url is not a valid SQLAlchemy URL") from exc
        if database_url.get_backend_name() == "sqlite":
            database_name = database_url.database
            if database_name in {None, "", ":memory:"}:
                if self.app_env != "test":
                    raise ValueError("in-memory SQLite is supported only during tests")
            else:
                database_path = Path(database_name).expanduser()
                if not database_path.is_absolute():
                    raise ValueError("SQLite database_url must use an absolute file path")
                resolved_database = database_path.resolve(strict=False)
                if not _is_relative_to(resolved_database, root):
                    raise ValueError("the SQLite database file must be beneath storage_root")

        if self.app_env == "enterprise":
            if self.auth_mode == "anonymous-poc":
                raise ValueError("enterprise deployments must use trusted-header authentication")
            if self.session_secret.get_secret_value() == DEVELOPMENT_SESSION_SECRET:
                raise ValueError("enterprise deployments require a unique session_secret")
            if not self.trusted_proxy_cidrs:
                raise ValueError("enterprise trusted-header mode requires trusted_proxy_cidrs")
            if "*" in self.allowed_hosts:
                raise ValueError("enterprise deployments must not allow every Host header")

        if (
            self.app_env != "test"
            and self.session_secret.get_secret_value() == DEVELOPMENT_SESSION_SECRET
        ):
            raise ValueError("set a unique session_secret before starting PDF Bridge")
        session_secret = self.session_secret.get_secret_value()
        if "CHANGE_ME" in session_secret:
            raise ValueError("replace placeholder secrets before starting PDF Bridge")
        if len(session_secret) < 32:
            raise ValueError("session_secret must be at least 32 characters")
        if self.search_api_url:
            if not self.search_api_token or not self.search_api_token.get_secret_value().strip():
                raise ValueError("search_api_token is required when search_api_url is configured")
            search_token = self.search_api_token.get_secret_value()
            if "CHANGE_ME" in search_token:
                raise ValueError("replace the retrieval credential placeholder")
            if len(search_token) < 32:
                raise ValueError("search_api_token must be at least 32 characters")
            if search_token == session_secret:
                raise ValueError("search_api_token must be distinct from bridge secrets")
            if any(character.isspace() or ord(character) < 32 for character in search_token):
                raise ValueError(
                    "search_api_token must not contain whitespace or control characters"
                )
            parsed_search_url = urlsplit(self.search_api_url)
            if (
                parsed_search_url.scheme not in {"http", "https"}
                or not parsed_search_url.hostname
                or parsed_search_url.username
                or parsed_search_url.password
                or parsed_search_url.query
                or parsed_search_url.fragment
            ):
                raise ValueError("search_api_url must be an absolute HTTP(S) URL without secrets")
            if self.app_env == "enterprise" and parsed_search_url.scheme != "https":
                raise ValueError("enterprise retrieval access must use HTTPS")
            self.search_api_url = self.search_api_url.rstrip("/")

        if self.qdrant_url:
            if not self.qdrant_api_key or not self.qdrant_api_key.get_secret_value().strip():
                raise ValueError("qdrant_api_key is required when qdrant_url is configured")
            qdrant_key = self.qdrant_api_key.get_secret_value()
            if "CHANGE_ME" in qdrant_key or len(qdrant_key) < 32:
                raise ValueError("qdrant_api_key must be a non-placeholder 32+ character secret")
            if qdrant_key == session_secret or (
                self.search_api_token
                and qdrant_key == self.search_api_token.get_secret_value()
            ):
                raise ValueError("qdrant_api_key must be distinct from bridge credentials")
            if any(character.isspace() or ord(character) < 32 for character in qdrant_key):
                raise ValueError("qdrant_api_key must not contain whitespace or control characters")

        if not HTTP_HEADER_NAME.fullmatch(self.trusted_identity_header):
            raise ValueError("trusted_identity_header must be a valid HTTP header name")
        if not self.allowed_hosts or any(
            not host.strip() or host != host.strip() for host in self.allowed_hosts
        ):
            raise ValueError("allowed_hosts must contain at least one non-blank host")
        for network in self.trusted_proxy_cidrs:
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"trusted_proxy_cidrs contains an invalid network: {network}"
                ) from exc
        if not self.clamd_host.strip():
            raise ValueError("clamd_host cannot be blank")
        if self.max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if not 1 <= self.max_upload_files <= 100:
            raise ValueError("max_upload_files must be between 1 and 100")
        if self.upload_chunk_bytes <= 0:
            raise ValueError("upload_chunk_bytes must be positive")
        if not 1 <= self.clamd_port <= 65535:
            raise ValueError("clamd_port must be a valid TCP port")
        if self.clamd_timeout <= 0 or self.search_api_timeout <= 0:
            raise ValueError("service timeouts must be positive")
        if self.clamd_stream_max_bytes <= 0:
            raise ValueError("clamd_stream_max_bytes must be positive")
        if self.max_upload_bytes > self.clamd_stream_max_bytes:
            raise ValueError("max_upload_bytes must not exceed clamd_stream_max_bytes")

        if self.worker_poll_seconds <= 0:
            raise ValueError("worker_poll_seconds must be positive")
        if not 10 <= self.worker_lease_seconds <= 3600:
            raise ValueError("worker_lease_seconds must be between 10 and 3600")
        if not 1 <= self.worker_heartbeat_seconds < self.worker_lease_seconds:
            raise ValueError("worker_heartbeat_seconds must be shorter than the lease")
        if self.parse_wall_clock_seconds <= 0 or self.parse_cpu_seconds <= 0:
            raise ValueError("parser time limits must be positive")
        if self.parse_memory_bytes < 64 * 1024 * 1024:
            raise ValueError("parse_memory_bytes must allow at least 64 MiB")
        if min(
            self.analysis_max_pages,
            self.analysis_max_characters,
            self.analysis_max_chunks,
        ) <= 0:
            raise ValueError("analysis safety caps must be positive")
        if self.embedding_timeout <= 0 or self.llm_timeout <= 0 or self.qdrant_timeout <= 0:
            raise ValueError("provider timeouts must be positive")

        for url_field, url_value in (
            ("embedding_api_url", self.embedding_api_url),
            ("llm_api_url", self.llm_api_url),
            ("qdrant_url", self.qdrant_url),
        ):
            if url_value is None:
                continue
            parsed = urlsplit(url_value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    f"{url_field} must be an absolute HTTP(S) URL without secrets"
                )
            setattr(self, url_field, url_value.rstrip("/"))
        if self.embedding_api_url:
            if not self.embedding_model_id or not self.embedding_model_id.strip():
                raise ValueError("embedding_model_id is required with embedding_api_url")
            if not self.embedding_dimension or not 1 <= self.embedding_dimension <= 65_536:
                raise ValueError("embedding_dimension is required with embedding_api_url")
        if self.llm_api_url and not (self.llm_classifier_model and self.llm_verifier_model):
            raise ValueError(
                "llm_classifier_model and llm_verifier_model are required with llm_api_url"
            )

        # Do not mutate the filesystem until every configuration check passes.
        root.mkdir(parents=True, exist_ok=True)
        for directory in ("objects", "temporary", "quarantine"):
            (root / directory).mkdir(mode=0o700, exist_ok=True)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable-in-practice settings instance."""

    return Settings()


def clear_settings_cache() -> None:
    """Clear cached settings (primarily for isolated tests)."""

    get_settings.cache_clear()
