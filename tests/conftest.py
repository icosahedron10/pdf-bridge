from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

# pdf_bridge.app intentionally validates required external storage at import time.
_IMPORT_STORAGE = tempfile.mkdtemp(prefix="pdf-bridge-import-")
os.environ.setdefault("PDF_BRIDGE_STORAGE_ROOT", _IMPORT_STORAGE)
os.environ.setdefault("PDF_BRIDGE_SESSION_SECRET", "test-import-session-secret-32-characters")
os.environ.setdefault("PDF_BRIDGE_JOB_TOKEN", "test-import-job-secret-32-characters")
os.environ.setdefault(
    "PDF_BRIDGE_COLLECTIONS",
    '[{"key":"customer","display_name":"Customer Product",'
    '"description":"Approved customer-facing product content.","audience":"customer"},'
    '{"key":"internal","display_name":"HR & Internal",'
    '"description":"Employee-only policies and operations.","audience":"internal"}]',
)

from pdf_bridge.app import create_app  # noqa: E402
from pdf_bridge.config import Settings  # noqa: E402
from pdf_bridge.db import build_engine, build_session_factory, create_schema, get_db  # noqa: E402
from pdf_bridge.models import ScanState, utc_now  # noqa: E402
from pdf_bridge.scanner import ScanResult  # noqa: E402

PDF_A = b"%PDF-1.4\n% bridge test\n1 0 obj\n<< /Type /Catalog /Value (A) >>\nendobj\n%%EOF\n"
PDF_B = b"%PDF-1.4\n% bridge test\n1 0 obj\n<< /Type /Catalog /Value (B) >>\nendobj\n%%EOF\n"


def clean_scanner(_path: Path) -> ScanResult:
    return ScanResult(state=ScanState.CLEAN, engine="test-clamd", scanned_at=utc_now())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        auth_mode="anonymous-poc",
        storage_root=tmp_path / "bridge-data",
        database_url="sqlite+pysqlite:///:memory:",
        session_secret=SecretStr("test-session-secret-not-for-production"),
        job_token=SecretStr("test-job-token-not-for-production"),
        search_api_token=SecretStr("test-search-token-not-for-production"),
        allowed_hosts=["testserver", "localhost", "127.0.0.1"],
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
    application = create_app(settings, scanner=clean_scanner)

    def database_override() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    application.dependency_overrides[get_db] = database_override
    application.state.test_session_factory = session_factory
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
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
        confirm: bool = False,
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
                "possible_duplicate_confirmed": str(confirm).lower(),
                "collection_key": collection,
            },
        )

    return perform


@pytest.fixture
def job_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.job_token.get_secret_value()}"}
