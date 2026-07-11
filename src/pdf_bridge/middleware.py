"""Small ASGI middleware for request tracing, security headers, and upload limits."""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        candidate = headers.get("x-request-id", "")
        request_id = candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["Referrer-Policy"] = "same-origin"
                response_headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                response_headers["X-Frame-Options"] = "SAMEORIGIN"
                response_headers["Cross-Origin-Resource-Policy"] = "same-origin"
                if not scope.get("path", "").startswith("/static/"):
                    response_headers.setdefault("Cache-Control", "no-store")
                if "content-security-policy" not in response_headers:
                    if scope.get("path") == "/api/docs":
                        # FastAPI's development-only Swagger page contains its
                        # bootstrap script and loads the pinned-major UI bundle
                        # from jsDelivr. Enterprise mode disables this route.
                        response_headers["Content-Security-Policy"] = (
                            "default-src 'none'; base-uri 'none'; object-src 'none'; "
                            "frame-ancestors 'self'; form-action 'self'; connect-src 'self'; "
                            "script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
                            "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
                            "img-src data: https://fastapi.tiangolo.com"
                        )
                    else:
                        response_headers["Content-Security-Policy"] = (
                            "default-src 'self'; base-uri 'none'; object-src 'none'; "
                            "frame-ancestors 'self'; form-action 'self'; script-src 'self'; "
                            "style-src 'self'; img-src 'self' data:; connect-src 'self'"
                        )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class UploadSizeLimitMiddleware:
    """Bound upload request bodies even when transfer encoding is chunked."""

    def __init__(self, app: ASGIApp, max_upload_bytes: int, overhead_bytes: int = 1_048_576):
        self.app = app
        self.max_request_bytes = max_upload_bytes + overhead_bytes

    async def _reject(self, scope: Scope, send: Send) -> None:
        body = json.dumps(
            {
                "type": "https://pdf-bridge.invalid/problems/upload-too-large",
                "title": "Upload too large",
                "status": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "detail": "The request exceeds the configured upload limit.",
                "code": "upload-too-large",
                "instance": scope.get("path"),
                "request_id": scope.get("state", {}).get("request_id"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/api/v1/uploads":
            await self.app(scope, receive, send)
            return

        raw_length = Headers(scope=scope).get("content-length")
        if raw_length:
            try:
                too_large = int(raw_length) > self.max_request_bytes
            except ValueError:
                too_large = True
            if too_large:
                await self._reject(scope, send)
                return

        received = 0

        async def receive_limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_request_bytes:
                    raise _UploadRequestTooLarge
            return message

        try:
            await self.app(scope, receive_limited, send)
        except _UploadRequestTooLarge:
            await self._reject(scope, send)


class _UploadRequestTooLarge(Exception):
    """Internal control flow used to stop multipart parsing at the byte limit."""
