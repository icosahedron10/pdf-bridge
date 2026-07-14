"""Typed, cookie-backed client for the canonical PDF Bridge API v2 surface."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Literal
from urllib.parse import quote, urlsplit

import httpx

API_PREFIX = "/api/v2"
CSRF_RESPONSE_HEADER = "X-CSRF-Token"
DEFAULT_TIMEOUT_SECONDS = 30.0
UPLOAD_READ_TIMEOUT_SECONDS = 600.0
HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

DecisionAction = Literal["KEEP", "REPLACE", "CANCEL"]
SearchMode = Literal["keyword", "semantic", "hybrid"]
HealthProbe = Literal["live", "ready"]


def new_idempotency_key() -> str:
    """Return a fresh key satisfying the API idempotency contract."""

    return uuid.uuid4().hex


@dataclass(slots=True)
class BridgeProblem(Exception):
    """Canonical sanitized API error raised as a typed exception."""

    status: int
    code: str
    message: str
    request_id: str | None = None
    retryable: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.message} ({self.code})"


class BridgeUnreachable(Exception):
    """PDF Bridge could not be reached or returned an invalid protocol response."""


def _problem_from_response(response: httpx.Response) -> BridgeProblem:
    """Build the v2 error type while tolerating an upstream non-JSON failure."""

    payload: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            nested = parsed.get("error")
            payload = nested if isinstance(nested, dict) else parsed
    except ValueError:
        pass

    known = {"code", "message", "request_id", "retryable"}
    message = payload.get("message")
    if not isinstance(message, str) or not message:
        message = response.text[:500] or "PDF Bridge returned an error without details."
    return BridgeProblem(
        status=response.status_code,
        code=str(payload.get("code", f"http_{response.status_code}")),
        message=message,
        request_id=(
            str(payload["request_id"])
            if payload.get("request_id") is not None
            else response.headers.get("X-Request-ID")
        ),
        retryable=payload.get("retryable") is True,
        extra={key: value for key, value in payload.items() if key not in known},
    )


def _require_idempotency_key(value: str | None) -> str:
    if (
        value is None
        or not 8 <= len(value) <= 128
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("idempotency_key must contain 8 to 128 visible ASCII characters")
    return value


def _cursor_params(*, cursor: str | None, limit: int) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    params: dict[str, Any] = {"limit": limit}
    if cursor is not None:
        if not cursor:
            raise ValueError("cursor cannot be blank")
        params["cursor"] = cursor
    return params


def _resource_segment(value: str) -> str:
    if not value:
        raise ValueError("resource identifiers cannot be blank")
    return quote(value, safe="")


def _download_filename(response: httpx.Response, fallback: str) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    utf8_match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if utf8_match:
        from urllib.parse import unquote

        return unquote(utf8_match.group(1))
    quoted_match = re.search(r'filename="([^"\\r\\n]+)"', disposition)
    return quoted_match.group(1) if quoted_match else fallback


class BridgeClient:
    """One browser-session client covering every canonical API v2 endpoint."""

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        transport: httpx.BaseTransport | None = None,
        identity_header_name: str | None = None,
        identity: str | None = None,
    ) -> None:
        candidate = base_url.strip()
        parsed = urlsplit(candidate)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "base_url must be an HTTP(S) server root without credentials, path, query, "
                "or fragment"
            )
        if (identity_header_name is None) != (identity is None):
            raise ValueError("identity header name and value must be configured together")
        default_headers: dict[str, str] = {}
        if identity_header_name is not None and identity is not None:
            if not HTTP_HEADER_NAME.fullmatch(identity_header_name):
                raise ValueError("identity header name is invalid")
            if not 1 <= len(identity) <= 200 or any(
                ord(character) < 32 or ord(character) > 126 for character in identity
            ):
                raise ValueError("identity must contain 1 to 200 printable ASCII characters")
            default_headers[identity_header_name] = identity
        self.base_url = candidate.rstrip("/")
        self.identity_header_name = identity_header_name
        self.identity = identity
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
            follow_redirects=False,
            headers=default_headers,
            transport=transport,
        )
        self._csrf_token: str | None = None

    @property
    def csrf_token(self) -> str | None:
        """Expose session readiness without exposing the cookie jar."""

        return self._csrf_token

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""

        self._http.close()

    def _capture_csrf(self, response: httpx.Response) -> None:
        token = response.headers.get(CSRF_RESPONSE_HEADER)
        if token:
            self._csrf_token = token

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BridgeUnreachable(
                f"Could not reach PDF Bridge at {self.base_url}: {exc}"
            ) from exc
        if 300 <= response.status_code < 400:
            raise BridgeUnreachable("PDF Bridge redirects are forbidden for operator requests.")
        return response

    def ensure_session(self, *, force: bool = False) -> str:
        """Authenticate a GET, retain its cookie, and capture its CSRF header."""

        if self._csrf_token is not None and not force:
            return self._csrf_token
        if force:
            self._csrf_token = None
        response = self._send(
            "GET",
            f"{API_PREFIX}/collections",
            params={"limit": 1},
        )
        if response.status_code >= 400:
            raise _problem_from_response(response)
        self._capture_csrf(response)
        if self._csrf_token is None:
            raise BridgeUnreachable(
                "PDF Bridge authenticated the session but did not return the "
                f"required {CSRF_RESPONSE_HEADER} response header."
            )
        return self._csrf_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        csrf_protected: bool = False,
        idempotency_key: str | None = None,
        require_idempotency: bool = False,
        retry_on_csrf_failure: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        headers: dict[str, str] = dict(kwargs.pop("headers", {}) or {})
        if csrf_protected:
            headers["X-CSRF-Token"] = self.ensure_session()
        if require_idempotency:
            headers["Idempotency-Key"] = _require_idempotency_key(idempotency_key)
        elif idempotency_key is not None:
            headers["Idempotency-Key"] = _require_idempotency_key(idempotency_key)

        response = self._send(method, path, headers=headers, **kwargs)
        if response.status_code < 400:
            self._capture_csrf(response)
            return response

        problem = _problem_from_response(response)
        if csrf_protected and retry_on_csrf_failure and problem.code == "csrf-check-failed":
            self.ensure_session(force=True)
            headers.pop("X-CSRF-Token", None)
            return self._request(
                method,
                path,
                csrf_protected=True,
                idempotency_key=idempotency_key,
                require_idempotency=require_idempotency,
                retry_on_csrf_failure=False,
                headers=headers,
                **kwargs,
            )
        raise problem

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BridgeUnreachable("PDF Bridge returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise BridgeUnreachable("PDF Bridge returned a non-object JSON response.")
        return payload

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._json_object(self._request("GET", path, params=params))

    # Health and collections -------------------------------------------------

    def health(self, probe: HealthProbe) -> dict[str, Any]:
        """Read liveness/readiness, preserving a valid 503 readiness payload."""

        response = self._send("GET", f"{API_PREFIX}/health/{probe}")
        if response.status_code not in {200, 503}:
            raise _problem_from_response(response)
        return self._json_object(response)

    def collections(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        return self._get_json(
            f"{API_PREFIX}/collections",
            params=_cursor_params(cursor=cursor, limit=limit),
        )

    def collection(self, collection_key: str) -> dict[str, Any]:
        key = _resource_segment(collection_key)
        return self._get_json(f"{API_PREFIX}/collections/{key}")

    def documents(
        self,
        collection_key: str,
        *,
        state: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        key = _resource_segment(collection_key)
        params = _cursor_params(cursor=cursor, limit=limit)
        if state is not None:
            params["state"] = state
        return self._get_json(
            f"{API_PREFIX}/collections/{key}/documents",
            params=params,
        )

    def name_check(self, collection_key: str, *, filename: str) -> dict[str, Any]:
        key = _resource_segment(collection_key)
        return self._json_object(
            self._request(
                "POST",
                f"{API_PREFIX}/collections/{key}/name-check",
                csrf_protected=True,
                json={"filename": filename},
            )
        )

    # Intake and document inspection ---------------------------------------

    def upload(
        self,
        collection_key: str,
        *,
        filename: str,
        content: bytes | BinaryIO,
        idempotency_key: str,
    ) -> dict[str, Any]:
        key = _resource_segment(collection_key)
        return self._json_object(
            self._request(
                "POST",
                f"{API_PREFIX}/collections/{key}/documents",
                csrf_protected=True,
                require_idempotency=True,
                idempotency_key=idempotency_key,
                files={"file": (filename, content, "application/pdf")},
                timeout=httpx.Timeout(UPLOAD_READ_TIMEOUT_SECONDS, connect=5.0),
            )
        )

    def document(self, document_id: str) -> dict[str, Any]:
        identifier = _resource_segment(document_id)
        return self._get_json(f"{API_PREFIX}/documents/{identifier}")

    def source(self, document_id: str) -> tuple[bytes, str, str]:
        identifier = _resource_segment(document_id)
        response = self._request("GET", f"{API_PREFIX}/documents/{identifier}/source")
        filename = _download_filename(response, f"{document_id}.pdf")
        content_type = response.headers.get("Content-Type", "application/pdf").split(";", 1)[0]
        return response.content, filename, content_type

    def markdown(self, document_id: str) -> dict[str, Any]:
        identifier = _resource_segment(document_id)
        return self._get_json(f"{API_PREFIX}/documents/{identifier}/markdown")

    def chunks(
        self,
        document_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        identifier = _resource_segment(document_id)
        return self._get_json(
            f"{API_PREFIX}/documents/{identifier}/chunks",
            params=_cursor_params(cursor=cursor, limit=limit),
        )

    def preflight(
        self,
        document_id: str,
        *,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        identifier = _resource_segment(document_id)
        return self._get_json(
            f"{API_PREFIX}/documents/{identifier}/preflight",
            params=_cursor_params(cursor=cursor, limit=limit),
        )

    # Lifecycle mutations and polling --------------------------------------

    def decide(
        self,
        document_id: str,
        *,
        prepared_revision_id: str,
        action: DecisionAction,
        target_document_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if action not in {"KEEP", "REPLACE", "CANCEL"}:
            raise ValueError("action must be KEEP, REPLACE, or CANCEL")
        if (action == "REPLACE") != (target_document_id is not None):
            raise ValueError("REPLACE alone requires target_document_id")
        identifier = _resource_segment(document_id)
        return self._json_object(
            self._request(
                "POST",
                f"{API_PREFIX}/documents/{identifier}/decision",
                csrf_protected=True,
                require_idempotency=True,
                idempotency_key=idempotency_key,
                json={
                    "prepared_revision_id": prepared_revision_id,
                    "action": action,
                    "target_document_id": target_document_id,
                },
            )
        )

    def retry(self, document_id: str, *, idempotency_key: str) -> dict[str, Any]:
        identifier = _resource_segment(document_id)
        return self._json_object(
            self._request(
                "POST",
                f"{API_PREFIX}/documents/{identifier}/retry",
                csrf_protected=True,
                require_idempotency=True,
                idempotency_key=idempotency_key,
                json={},
            )
        )

    def delete(self, document_id: str, *, idempotency_key: str) -> dict[str, Any]:
        identifier = _resource_segment(document_id)
        return self._json_object(
            self._request(
                "DELETE",
                f"{API_PREFIX}/documents/{identifier}",
                csrf_protected=True,
                require_idempotency=True,
                idempotency_key=idempotency_key,
            )
        )

    def events(
        self,
        document_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        identifier = _resource_segment(document_id)
        return self._get_json(
            f"{API_PREFIX}/documents/{identifier}/events",
            params=_cursor_params(cursor=cursor, limit=limit),
        )

    def operation(self, operation_id: str) -> dict[str, Any]:
        identifier = _resource_segment(operation_id)
        return self._get_json(f"{API_PREFIX}/operations/{identifier}")

    def operation_metrics(self) -> dict[str, Any]:
        """Read content-free durable queue, failure, and phase aggregates."""

        return self._get_json(f"{API_PREFIX}/operations/metrics")

    def history(
        self,
        *,
        collection_key: str | None = None,
        disposition: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        params = _cursor_params(cursor=cursor, limit=limit)
        if collection_key is not None:
            params["collection_key"] = collection_key
        if disposition is not None:
            params["disposition"] = disposition
        return self._get_json(f"{API_PREFIX}/history", params=params)

    # Optional operator search ---------------------------------------------

    def search(
        self,
        *,
        collection_key: str,
        query: str,
        mode: SearchMode,
        limit: int = 20,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return self._json_object(
            self._request(
                "POST",
                f"{API_PREFIX}/operator/search",
                csrf_protected=True,
                json={
                    "collection_key": collection_key,
                    "query": query,
                    "mode": mode,
                    "limit": limit,
                },
            )
        )
