from __future__ import annotations

import asyncio
import json

from pdf_bridge.middleware import UploadSizeLimitMiddleware


def test_chunked_upload_is_stopped_at_request_limit() -> None:
    completed = False
    sent: list[dict] = []
    incoming = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )

    async def downstream(_scope, receive, send) -> None:
        nonlocal completed
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        completed = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> dict:
        return next(incoming)

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = UploadSizeLimitMiddleware(downstream, max_upload_bytes=5, overhead_bytes=0)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/uploads",
        "headers": [],
        "state": {"request_id": "chunked-limit-test"},
    }
    asyncio.run(middleware(scope, receive, send))

    assert completed is False
    assert sent[0]["status"] == 413
    body = json.loads(sent[1]["body"])
    assert body["code"] == "upload-too-large"
    assert body["request_id"] == "chunked-limit-test"
