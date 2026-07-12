from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from litestar.testing import RequestFactory, TestClient
from pydantic import SecretStr, ValidationError

from pdf_bridge.app import create_app
from pdf_bridge.core.config import Settings
from pdf_bridge.http.problems import ProblemError
from pdf_bridge.http.security import get_actor
from pdf_bridge.persistence.db import build_engine, create_schema
from pdf_bridge.persistence.models import Document, DocumentState
from pdf_bridge.services.lifecycle import LifecycleError, import_historical_manifest
from pdf_bridge.services.storage import StorageLayout, UnsafePathError, validate_source_path
from tests.conftest import PDF_A, clean_scanner


def test_enterprise_mode_rejects_anonymous_access(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="trusted-header"):
        Settings(
            app_env="enterprise",
            auth_mode="anonymous-poc",
            storage_root=tmp_path / "external-data",
            session_secret=SecretStr("unique-enterprise-session-secret"),
            job_token=SecretStr("unique-enterprise-job-token"),
        )


def test_storage_root_cannot_be_in_source_or_onedrive() -> None:
    with pytest.raises(ValidationError, match="outside the application source tree"):
        Settings(storage_root=Path.cwd() / "runtime-data")


def test_sqlite_catalog_must_be_an_absolute_path_beneath_storage_root(
    tmp_path: Path,
) -> None:
    common = {
        "app_env": "test",
        "session_secret": SecretStr("session-secret-value-at-least-32-characters"),
        "job_token": SecretStr("job-token-value-that-is-at-least-32-characters"),
    }
    relative_root = tmp_path / "relative-database-root"
    with pytest.raises(ValidationError, match="absolute file path"):
        Settings(
            **common,
            storage_root=relative_root,
            database_url="sqlite+pysqlite:///catalog.sqlite3",
        )
    assert not relative_root.exists()

    external_root = tmp_path / "external-database-root"
    outside_database = tmp_path / "outside" / "catalog.sqlite3"
    with pytest.raises(ValidationError, match="beneath storage_root"):
        Settings(
            **common,
            storage_root=external_root,
            database_url=f"sqlite+pysqlite:///{outside_database.as_posix()}",
        )
    assert not external_root.exists()

    valid_root = tmp_path / "valid-database-root"
    valid_database = valid_root / "catalog.sqlite3"
    settings = Settings(
        **common,
        storage_root=valid_root,
        database_url=f"sqlite+pysqlite:///{valid_database.as_posix()}",
    )
    assert settings.storage_root == valid_root.resolve()


def test_in_memory_sqlite_is_test_only(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="only during tests"):
        Settings(
            app_env="development",
            storage_root=tmp_path / "memory-database-root",
            database_url="sqlite+pysqlite:///:memory:",
            session_secret=SecretStr("session-secret-value-at-least-32-characters"),
            job_token=SecretStr("job-token-value-that-is-at-least-32-characters"),
        )


def test_upload_limit_cannot_exceed_clamd_stream_limit(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        Settings(
            storage_root=tmp_path / "external-data",
            max_upload_bytes=65 * 1024 * 1024,
            clamd_stream_max_bytes=64 * 1024 * 1024,
        )


def test_runtime_secrets_are_required_and_distinct(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing-job-token"
    with pytest.raises(ValidationError, match="job_token is required"):
        Settings(
            storage_root=missing_root,
            session_secret=SecretStr("unique-session-secret"),
            job_token=None,
        )
    assert not missing_root.exists()
    with pytest.raises(ValidationError, match="must be different"):
        Settings(
            storage_root=tmp_path / "matching-secrets",
            session_secret=SecretStr("same-secret-value"),
            job_token=SecretStr("same-secret-value"),
        )
    with pytest.raises(ValidationError, match="at least 32"):
        Settings(
            storage_root=tmp_path / "weak-secrets",
            session_secret=SecretStr("short-session"),
            job_token=SecretStr("a-different-but-still-short-job-token"),
        )
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(
            storage_root=tmp_path / "placeholder-secrets",
            session_secret=SecretStr("CHANGE_ME_generate_a_long_random_session_secret"),
            job_token=SecretStr("separate-job-token-at-least-32-characters"),
        )


def test_retrieval_configuration_requires_a_separate_credential(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="search_api_token is required"):
        Settings(
            app_env="test",
            storage_root=tmp_path / "missing-search-token",
            session_secret=SecretStr("session-secret-value-at-least-32-characters"),
            job_token=SecretStr("job-token-value-that-is-at-least-32-characters"),
            search_api_url="https://retrieval.example.test",
            search_api_token=None,
        )


def test_trusted_header_identity_requires_configured_proxy(app) -> None:
    app.state.settings.auth_mode = "trusted-header"
    app.state.settings.trusted_proxy_cidrs = ["127.0.0.0/8"]
    trusted_request = RequestFactory(
        app=app,
        server="127.0.0.1",
        port=50000,
    ).get(
        "/upload",
        headers={"X-Forwarded-User": "data.scientist@example.test"},
        session={},
    )
    actor = get_actor(trusted_request)
    assert actor.identifier == "data.scientist@example.test"
    assert actor.kind == "trusted-header"

    missing_request = RequestFactory(
        app=app,
        server="127.0.0.1",
        port=50000,
    ).get("/upload", session={})
    with pytest.raises(ProblemError) as missing:
        get_actor(missing_request)
    assert missing.value.code == "identity-required"

    untrusted_request = RequestFactory(
        app=app,
        server="192.0.2.5",
        port=50000,
    ).get(
        "/upload",
        headers={"X-Forwarded-User": "forged@example.test"},
        session={},
    )
    with pytest.raises(ProblemError) as rejected:
        get_actor(untrusted_request)
    assert rejected.value.code == "untrusted-identity-source"


def test_html_responses_have_a_restrictive_content_security_policy(
    client: TestClient,
) -> None:
    response = client.get("/library")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "form-action 'self'" in policy

    for path in ("/api", "/api/", "/api/docs", "/api/oauth2-redirect.html"):
        docs = client.get(path)
        assert docs.status_code == 200, path
        docs_policy = docs.headers["content-security-policy"]
        assert "https://cdn.jsdelivr.net" in docs_policy
        assert "connect-src 'self'" in docs_policy


def test_browser_session_cookie_is_encrypted_and_persists(client: TestClient) -> None:
    first = client.get("/upload")
    assert first.status_code == 200
    first_token = re.search(
        r'<meta name="csrf-token" content="([^"]+)"', first.text
    )
    assert first_token

    encoded_cookie = client.cookies.get("pdf_bridge_session")
    assert encoded_cookie
    assert first_token.group(1) not in encoded_cookie
    set_cookie = first.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Max-Age=28800" in set_cookie
    assert "Path=/" in set_cookie

    second = client.get("/upload")
    second_token = re.search(
        r'<meta name="csrf-token" content="([^"]+)"', second.text
    )
    assert second_token
    assert second_token.group(1) == first_token.group(1)


def test_enterprise_mode_disables_all_openapi_routes(tmp_path: Path) -> None:
    storage_root = tmp_path / "enterprise-docs"
    database_path = storage_root / "catalog.sqlite3"
    settings = Settings(
        app_env="enterprise",
        auth_mode="trusted-header",
        storage_root=storage_root,
        database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
        session_secret=SecretStr("enterprise-docs-session-secret-32-characters"),
        job_token=SecretStr("enterprise-docs-job-token-32-characters-long"),
        allowed_hosts=["testserver"],
        trusted_proxy_cidrs=["127.0.0.0/8"],
        collections=[
            {
                "key": "customer",
                "display_name": "Customer Product",
                "description": "Approved customer-facing product content.",
                "audience": "customer",
            }
        ],
    )
    engine = build_engine(settings.database_url)
    create_schema(engine)
    engine.dispose()
    application = create_app(settings, scanner=clean_scanner)

    with TestClient(
        application,
        base_url="http://testserver",
        raise_server_exceptions=True,
    ) as test_client:
        for path in (
            "/api",
            "/api/",
            "/api/docs",
            "/api/openapi.json",
            "/api/oauth2-redirect.html",
        ):
            assert test_client.get(path).status_code == 404, path


def test_historical_import_dry_run_rejects_duplicate_manifest_contents(
    tmp_path: Path, session_factory
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "one.pdf").write_bytes(PDF_A)
    (source_root / "two.pdf").write_bytes(PDF_A)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "documents": [
                    {
                        "path": "one.pdf",
                        "filename": "one.pdf",
                        "collection_key": "internal",
                        "language": "en",
                    },
                    {
                        "path": "two.pdf",
                        "filename": "two.pdf",
                        "collection_key": "internal",
                        "language": "en",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    layout = StorageLayout.from_root(tmp_path / "bridge-storage")
    with (
        session_factory() as session,
        pytest.raises(LifecycleError, match="duplicate PDF contents"),
    ):
        import_historical_manifest(
            session,
            manifest_path=manifest,
            source_root=source_root,
            layout=layout,
            scanner=clean_scanner,
            max_bytes=1024 * 1024,
            dry_run=True,
            actor_id="import-test",
            configured_collections={"customer", "internal"},
        )


def test_historical_source_path_cannot_escape_root(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF_A)
    with pytest.raises(UnsafePathError, match="escapes"):
        validate_source_path(source_root, "../outside.pdf")


def test_historical_import_applies_scanned_canonical_record(
    tmp_path: Path, session_factory
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "existing.pdf").write_bytes(PDF_A)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "documents": [
                    {
                        "path": "existing.pdf",
                        "filename": "Existing handbook.pdf",
                        "chunk_count": 12,
                        "pipeline_run_id": "historical-run-1",
                        "collection_key": "internal",
                        "language": "en",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    layout = StorageLayout.from_root(tmp_path / "bridge-storage")
    with session_factory() as session:
        response = import_historical_manifest(
            session,
            manifest_path=manifest,
            source_root=source_root,
            layout=layout,
            scanner=clean_scanner,
            max_bytes=1024 * 1024,
            dry_run=False,
            actor_id="import-test",
            configured_collections={"customer", "internal"},
        )
        session.commit()
        document_id = response.items[0].document_id
        assert response.imported == 1
        assert document_id is not None
        document = session.get(Document, document_id)
        assert document is not None
        assert document.state == DocumentState.INGESTED
        assert document.chunk_count == 12
        assert document.storage_key is not None
        assert (layout.root / document.storage_key).read_bytes() == PDF_A
