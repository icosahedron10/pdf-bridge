from __future__ import annotations

import re
from collections.abc import Iterator

from litestar.testing import TestClient
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge.app import UPLOAD_REQUEST_OVERHEAD_BYTES, create_app
from tests.conftest import clean_scanner


def test_chunked_upload_is_stopped_at_litestar_request_limit(
    settings,
    session_factory: sessionmaker[Session],
) -> None:
    max_upload_bytes = 5
    limited_settings = settings.model_copy(
        update={"max_upload_bytes": max_upload_bytes}
    )

    def db_provider() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    application = create_app(
        limited_settings,
        scanner=clean_scanner,
        db_provider=db_provider,
    )
    envelope_limit = max_upload_bytes + UPLOAD_REQUEST_OVERHEAD_BYTES

    def body_chunks() -> Iterator[bytes]:
        chunk_size = envelope_limit // 2 + 1
        yield b"x" * chunk_size
        yield b"y" * chunk_size

    with TestClient(
        application,
        base_url="http://testserver.local",
        raise_server_exceptions=True,
    ) as client:
        page = client.get("/upload")
        token_match = re.search(
            r'<meta name="csrf-token" content="([^"]+)"', page.text
        )
        assert token_match
        response = client.post(
            "/api/v1/uploads",
            headers={
                "Content-Type": "multipart/form-data; boundary=chunked-limit-test",
                "X-CSRF-Token": token_match.group(1),
                "Idempotency-Key": "chunked-limit-key",
            },
            content=body_chunks(),
        )

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["status_code"] == 413
    assert response.json()["detail"]
    assert "code" not in response.json()
    assert response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
