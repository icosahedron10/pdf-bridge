"""ASGI request tracing, trusted-host enforcement, and security headers."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from litestar.datastructures import Headers, MutableScopeHeaders
from litestar.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
DNS_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
DOCS_UI_PATHS = {"/api", "/api/", "/api/docs", "/api/oauth2-redirect.html"}


def ensure_request_id(scope: Scope) -> str:
    """Return the validated request ID for a scope, creating one when needed."""

    state = scope.setdefault("state", {})
    current = state.get("request_id")
    if isinstance(current, str) and REQUEST_ID_PATTERN.fullmatch(current):
        return current

    candidate = Headers.from_scope(scope).get("x-request-id", "")
    request_id = candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else str(uuid4())
    state["request_id"] = request_id
    return request_id


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = ensure_request_id(scope)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableScopeHeaders.from_message(message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["Referrer-Policy"] = "same-origin"
                response_headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=()"
                )
                response_headers["X-Frame-Options"] = "SAMEORIGIN"
                response_headers["Cross-Origin-Resource-Policy"] = "same-origin"
                if not scope.get("path", "").startswith("/static/"):
                    response_headers.setdefault("Cache-Control", "no-store")
                if "content-security-policy" not in response_headers:
                    if scope.get("path") in DOCS_UI_PATHS:
                        response_headers["Content-Security-Policy"] = (
                            "default-src 'none'; base-uri 'none'; object-src 'none'; "
                            "frame-ancestors 'self'; form-action 'self'; connect-src 'self'; "
                            "script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
                            "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
                            "img-src 'self' data: https://cdn.jsdelivr.net"
                        )
                    else:
                        response_headers["Content-Security-Policy"] = (
                            "default-src 'self'; base-uri 'none'; object-src 'none'; "
                            "frame-ancestors 'self'; form-action 'self'; script-src 'self'; "
                            "style-src 'self'; img-src 'self' data:; connect-src 'self'"
                        )
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _valid_hostname(value: str) -> bool:
    """Return whether a parsed hostname is a valid IP literal or DNS name."""

    if not value or len(value) > 253 or not value.isascii() or "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        return all(DNS_LABEL_PATTERN.fullmatch(label) for label in labels)
    return True


def _hostname_from_header(value: str) -> str | None:
    """Parse an HTTP Host value and discard only its optional port."""

    if (
        not value
        or value.endswith(":")
        or any(character.isspace() for character in value)
    ):
        return None
    try:
        parsed = urlsplit(f"//{value}", allow_fragments=False)
        port = parsed.port
    except ValueError:
        return None
    if (
        not parsed.hostname
        or not _valid_hostname(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    if port is not None and not 0 < port <= 65535:
        return None
    return parsed.hostname.casefold()


class PortAwareTrustedHostMiddleware:
    """Reject unexpected Host values while allowing arbitrary valid ports."""

    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str]) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")
        self.app = app
        self.allow_any = "*" in allowed_hosts
        self.exact_hosts: set[str] = set()
        self.wildcard_suffixes: tuple[str, ...] = ()

        suffixes: list[str] = []
        for allowed in allowed_hosts:
            normalized = allowed.casefold()
            if normalized == "*":
                continue
            if normalized.startswith("*.") and normalized.count("*") == 1:
                suffix = normalized[2:]
                if not _valid_hostname(suffix):
                    raise ValueError(f"invalid wildcard host suffix: {allowed!r}")
                suffixes.append(suffix)
                continue
            if "*" in normalized:
                raise ValueError(
                    "host wildcards must use the '*.example.com' form"
                )
            if normalized.startswith("[") and normalized.endswith("]"):
                normalized = normalized[1:-1]
            if not _valid_hostname(normalized):
                raise ValueError(f"invalid allowed host: {allowed!r}")
            self.exact_hosts.add(normalized)
        self.wildcard_suffixes = tuple(suffixes)

    def _is_allowed(self, hostname: str) -> bool:
        return self.allow_any or hostname in self.exact_hosts or any(
            hostname.endswith(f".{suffix}") for suffix in self.wildcard_suffixes
        )

    async def _reject(self, send: Send) -> None:
        body = json.dumps(
            {"status_code": 400, "detail": "Invalid host header"},
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers.from_scope(scope)
        host_values = headers.getall("host", [])
        hostname = _hostname_from_header(host_values[0]) if len(host_values) == 1 else None
        if hostname is None or not self._is_allowed(hostname):
            await self._reject(send)
            return
        await self.app(scope, receive, send)
