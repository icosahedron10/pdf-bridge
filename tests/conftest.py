from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from litestar.testing import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

# pdf_bridge.app intentionally validates required external storage at import time.
_IMPORT_STORAGE = tempfile.mkdtemp(prefix="pdf-bridge-import-")
os.environ.setdefault("PDF_BRIDGE_STORAGE_ROOT", _IMPORT_STORAGE)
os.environ.setdefault("PDF_BRIDGE_SESSION_SECRET", "test-import-session-secret-32-characters")
os.environ.setdefault(
    "PDF_BRIDGE_COLLECTIONS",
    '[{"key":"customer","display_name":"Customer Product",'
    '"description":"Approved customer-facing product content.","audience":"customer"},'
    '{"key":"internal","display_name":"HR & Internal",'
    '"description":"Employee-only policies and operations.","audience":"internal"}]',
)

from pdf_bridge.app import create_app  # noqa: E402
from pdf_bridge.core.config import Settings  # noqa: E402
from pdf_bridge.persistence.db import (  # noqa: E402
    build_engine,
    build_session_factory,
    create_schema,
)
from pdf_bridge.persistence.models import ScanState, utc_now  # noqa: E402
from pdf_bridge.services.scanner import ScanResult  # noqa: E402

PDF_A = b"%PDF-1.4\n% bridge test\n1 0 obj\n<< /Type /Catalog /Value (A) >>\nendobj\n%%EOF\n"
PDF_B = b"%PDF-1.4\n% bridge test\n1 0 obj\n<< /Type /Catalog /Value (B) >>\nendobj\n%%EOF\n"


def clean_scanner(_path: Path) -> ScanResult:
    return ScanResult(state=ScanState.CLEAN, engine="test-clamd", scanned_at=utc_now())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    storage_root = tmp_path / "bridge-data"
    return Settings(
        app_env="test",
        auth_mode="anonymous-poc",
        storage_root=storage_root,
        database_url=f"sqlite+pysqlite:///{(storage_root / 'catalog.sqlite3').as_posix()}",
        session_secret=SecretStr("test-session-secret-not-for-production"),
        search_api_token=SecretStr("test-search-token-not-for-production"),
        allowed_hosts=["testserver.local", "localhost", "127.0.0.1"],
        clamd_host="127.0.0.1",
        clamd_port=3310,
        clamd_timeout=0.05,
        search_api_url="https://retrieval.test",
        collections=[
            {
                "key": "customer",
                "display_name": "Customer Product",
                "description": "Approved customer-facing product content.",
                "audience": "customer",
            },
            {
                "key": "internal",
                "display_name": "HR & Internal",
                "description": "Employee-only policies and operations.",
                "audience": "internal",
            },
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


@pytest.fixture
def app(settings: Settings, session_factory: sessionmaker[Session]):
    def database_override() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    application = create_app(
        settings,
        scanner=clean_scanner,
        db_provider=database_override,
    )
    application.state.test_session_factory = session_factory
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(
        app,
        base_url="http://testserver.local",
        raise_server_exceptions=True,
    ) as test_client:
        yield test_client


@pytest.fixture
def csrf_headers(client: TestClient) -> dict[str, str]:
    response = client.get("/upload")
    assert response.status_code == 200
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', response.text)
    assert match
    return {"X-CSRF-Token": match.group(1)}


@pytest.fixture
def upload_pdf(client: TestClient, csrf_headers: dict[str, str]) -> Callable[..., object]:
    def perform(
        *,
        filename: str = "example.pdf",
        contents: bytes = PDF_A,
        key: str = "upload-key-0001",
        content_type: str = "application/pdf",
        collection: str = "customer",
    ):
        headers = {**csrf_headers, "Idempotency-Key": key}
        return client.post(
            "/api/v1/uploads",
            headers=headers,
            files={"file": (filename, contents, content_type)},
            data={
                "idempotency_key": key,
                "collection_key": collection,
            },
        )

    return perform
