from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from litestar.testing import TestClient
from pydantic import SecretStr

import pdf_bridge.app as app_module
import pdf_bridge.persistence.db as db_module
from pdf_bridge.app import create_app
from pdf_bridge.core.config import Settings
from pdf_bridge.persistence.db import build_engine, build_session_factory, create_schema
from pdf_bridge.persistence.models import Document, DocumentState, ScanState, utc_now
from tests.conftest import clean_scanner

BASE_URL = "http://testserver.local"


def _prepare_catalog(settings: Settings, *, with_marker: bool = False) -> uuid.UUID | None:
    """Create the schema for a settings-owned database, optionally with one row."""

    engine = build_engine(settings.database_url)
    create_schema(engine)
    marker_id: uuid.UUID | None = None
    if with_marker:
        marker_id = uuid.uuid4()
        factory = build_session_factory(engine)
        with factory() as session:
            session.add(
                Document(
                    id=marker_id,
                    original_filename="settings-database-marker.pdf",
                    normalized_filename="settings-database-marker.pdf",
                    storage_key=f"objects/{marker_id}.pdf",
                    size_bytes=10,
                    sha256="a" * 64,
                    idempotency_key="settings-database-marker",
                    state=DocumentState.INGESTED,
                    scan_state=ScanState.CLEAN,
                    scan_engine="test-clamd",
                    scanned_at=utc_now(),
                    uploader_identity="lifecycle-test",
                    collection_key="customer",
                )
            )
            session.commit()
    engine.dispose()
    return marker_id


def test_custom_db_provider_is_rejected_outside_test_mode(tmp_path: Path) -> None:
    development_settings = Settings(
        app_env="development",
        storage_root=tmp_path / "provider-guard-data",
        session_secret=SecretStr("development-guard-session-secret-32-characters"),
        job_token=SecretStr("development-guard-job-token-32-characters-x"),
        collections=[
            {
                "key": "customer",
                "display_name": "Customer Product",
                "description": "Approved customer-facing product content.",
                "audience": "customer",
            }
        ],
    )

    def provider():
        raise AssertionError("the provider must never be called")
        yield

    with pytest.raises(RuntimeError, match="test mode"):
        create_app(development_settings, scanner=clean_scanner, db_provider=provider)


def test_create_app_validates_and_serves_the_settings_database(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker_id = _prepare_catalog(settings, with_marker=True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the process-wide cached database must not be used")

    # The application must run entirely on the settings-owned database.
    monkeypatch.setattr(db_module, "get_engine", forbidden)
    monkeypatch.setattr(db_module, "get_session_factory", forbidden)

    application = create_app(settings, scanner=clean_scanner)
    with TestClient(application, base_url=BASE_URL, raise_server_exceptions=True) as client:
        response = client.get(f"/api/v1/documents/{marker_id}")

    assert response.status_code == 200
    assert response.json()["original_filename"] == "settings-database-marker.pdf"


def _track_engines(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Count engine builds and per-engine dispose calls inside create_app."""

    engines: list[object] = []
    real_build_engine = app_module.build_engine

    def counting_build_engine(database_url: str, **kwargs):
        engine = real_build_engine(database_url, **kwargs)
        real_dispose = engine.dispose
        engine.dispose_calls = 0

        def counting_dispose(*args, **dispose_kwargs):
            engine.dispose_calls += 1
            return real_dispose(*args, **dispose_kwargs)

        engine.dispose = counting_dispose
        engines.append(engine)
        return engine

    monkeypatch.setattr(app_module, "build_engine", counting_build_engine)
    return engines


def _track_clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Client]:
    """Count httpx.Client constructions and close calls inside create_app."""

    clients: list[httpx.Client] = []
    real_client = httpx.Client

    class TrackingClient(real_client):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            clients.append(self)

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    monkeypatch.setattr(httpx, "Client", TrackingClient)
    return clients


def test_lifespan_reuses_and_closes_owned_resources_exactly_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_catalog(settings)
    engines = _track_engines(monkeypatch)
    clients = _track_clients(monkeypatch)

    application = create_app(settings, scanner=clean_scanner)
    with TestClient(application, base_url=BASE_URL, raise_server_exceptions=True) as client:
        # Multiple database-backed requests must reuse the one owned engine.
        assert client.get("/api/v1/collections").status_code == 200
        assert client.get("/api/v1/collections").status_code == 200

    assert len(engines) == 1
    assert engines[0].dispose_calls == 1
    assert len(clients) == 1
    assert clients[0].close_calls == 1


def _leaf_messages(error: BaseException) -> list[str]:
    if isinstance(error, BaseExceptionGroup):
        return [message for nested in error.exceptions for message in _leaf_messages(nested)]
    return [str(error)]


def test_lifespan_releases_owned_engine_when_startup_validation_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No schema exists, so startup collection validation must fail.
    engines = _track_engines(monkeypatch)
    clients = _track_clients(monkeypatch)

    application = create_app(settings, scanner=clean_scanner)
    with pytest.raises(BaseExceptionGroup) as startup_failure:
        with TestClient(application, base_url=BASE_URL, raise_server_exceptions=True):
            pytest.fail("startup must not succeed without a catalog schema")

    assert any("no such table" in message for message in _leaf_messages(startup_failure.value))
    assert len(engines) == 1
    assert engines[0].dispose_calls == 1
    assert clients == []


def test_injected_search_client_is_caller_owned(
    settings: Settings, session_factory
) -> None:
    injected = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    )

    def database_override():
        with session_factory() as session:
            yield session

    application = create_app(
        settings,
        scanner=clean_scanner,
        search_http_client=injected,
        db_provider=database_override,
    )
    with TestClient(application, base_url=BASE_URL, raise_server_exceptions=True) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.app.state.search_http_client is injected

    assert not injected.is_closed
    injected.close()
