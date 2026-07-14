"""Typed HTTP client for the PDF Bridge browser API.

The Streamlit app is a separate process, so it talks to PDF Bridge exactly the
way the built-in browser interface does: it holds a cookie-backed anonymous
session, reads the CSRF token from a rendered page, and sends every mutation
with ``x-csrf-token`` and an ``Idempotency-Key``. It never touches the SQLite
catalog directly because the supported topology is a single application
process.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Literal

import httpx

CSRF_META_PATTERN = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
SESSION_BOOTSTRAP_PATH = "/library"
UPLOAD_READ_TIMEOUT_SECONDS = 600.0
DEFAULT_TIMEOUT_SECONDS = 30.0

DecisionAction = Literal["keep", "replace", "cancel"]
SearchMode = Literal["keyword", "semantic", "hybrid"]
DocumentScope = Literal["library", "queue", "all"]


def new_idempotency_key() -> str:
    """Return a fresh key satisfying the API's idempotency-key contract."""

    return uuid.uuid4().hex


@dataclass(slots=True)
class BridgeProblem(Exception):
    """RFC 9457 problem response raised as a typed exception."""

    status: int
    code: str
    title: str
    detail: str
    request_id: str | None = None
    duplicate: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.title} ({self.code}): {self.detail}"


class BridgeUnreachable(Exception):
    """PDF Bridge could not be reached at the configured base URL."""


def _problem_from_response(response: httpx.Response) -> BridgeProblem:
    """Build a typed problem from an error response, tolerating non-JSON bodies."""

    payload: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            payload = parsed
    except ValueError:
        payload = {}
    known = {"status", "code", "title", "detail", "request_id", "duplicate", "type", "instance"}
    return BridgeProblem(
        status=response.status_code,
        code=str(payload.get("code", f"http-{response.status_code}")),
        title=str(payload.get("title", "PDF Bridge returned an error")),
        detail=str(payload.get("detail", response.text[:500] or "No details were provided.")),
        request_id=payload.get("request_id"),
        duplicate=payload.get("duplicate"),
        extra={key: value for key, value in payload.items() if key not in known},
    )


class BridgeClient:
    """Session-holding client covering the full ``/api/v1`` intake surface."""

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
            follow_redirects=True,
        )
        self._csrf_token: str | None = None

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""

        self._http.close()

    # ------------------------------------------------------------------
    # Session and request plumbing
    # ------------------------------------------------------------------

    def ensure_session(self, *, force: bool = False) -> str:
        """Hold a browser session and return its CSRF token.

        PDF Bridge issues the anonymous session cookie and CSRF token when a
        web page renders, so the client fetches one lightweight page and reads
        the ``csrf-token`` meta tag exactly as the built-in UI would.
        """

        if self._csrf_token is not None and not force:
            return self._csrf_token
        try:
            response = self._http.get(SESSION_BOOTSTRAP_PATH)
        except httpx.HTTPError as exc:
            raise BridgeUnreachable(
                f"Could not reach PDF Bridge at {self.base_url}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise _problem_from_response(response)
        match = CSRF_META_PATTERN.search(response.text)
        if match is None:
            raise BridgeUnreachable(
                f"{self.base_url} responded, but not with a PDF Bridge page; "
                "check that the base URL points at the bridge, not another service."
            )
        self._csrf_token = match.group(1)
        return self._csrf_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        mutation: bool = False,
        idempotency_key: str | None = None,
        retry_on_csrf_failure: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        headers: dict[str, str] = dict(kwargs.pop("headers", {}) or {})
        if mutation:
            headers["x-csrf-token"] = self.ensure_session()
            if idempotency_key is not None:
                headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise BridgeUnreachable(
                f"Could not reach PDF Bridge at {self.base_url}: {exc}"
            ) from exc
        if response.status_code < 400:
            return response
        problem = _problem_from_response(response)
        # An expired 8-hour session invalidates the cached token; one refresh
        # is transparent and safe because mutations carry idempotency keys.
        if mutation and retry_on_csrf_failure and problem.code == "csrf-check-failed":
            self.ensure_session(force=True)
            return self._request(
                method,
                path,
                mutation=True,
                idempotency_key=idempotency_key,
                retry_on_csrf_failure=False,
                headers=headers,
                **kwargs,
            )
        raise problem

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        cleaned = {key: value for key, value in (params or {}).items() if value is not None}
        return self._request("GET", path, params=cleaned).json()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self, probe: Literal["live", "ready", "dependencies"]) -> dict[str, Any]:
        """Return a health payload, treating a degraded 503 as data, not failure."""

        try:
            response = self._http.get(f"/api/v1/health/{probe}")
        except httpx.HTTPError as exc:
            raise BridgeUnreachable(
                f"Could not reach PDF Bridge at {self.base_url}: {exc}"
            ) from exc
        if response.status_code in (200, 503):
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if isinstance(payload, dict) and "status" in payload:
                return payload
        raise _problem_from_response(response)

    # ------------------------------------------------------------------
    # Collections and catalog
    # ------------------------------------------------------------------

    def collections(self) -> dict[str, Any]:
        """List configured collections with live catalog counts."""

        return self._get_json("/api/v1/collections")

    def documents(
        self,
        *,
        scope: DocumentScope = "all",
        state: str | None = None,
        collection_key: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        """List catalog documents filtered by lifecycle scope and collection."""

        return self._get_json(
            "/api/v1/documents",
            params={
                "scope": scope,
                "state": state,
                "collection_key": collection_key,
                "page": page,
                "page_size": page_size,
            },
        )

    def document(self, document_id: str) -> dict[str, Any]:
        """Return one document with its analysis, decisions, and audit ledger."""

        return self._get_json(f"/api/v1/documents/{document_id}")

    def document_content(self, document_id: str) -> tuple[bytes, str]:
        """Download a clean, available PDF and return its bytes and filename."""

        response = self._request("GET", f"/api/v1/documents/{document_id}/content")
        disposition = response.headers.get("content-disposition", "")
        match = re.search(r'filename="([^"]+)"', disposition)
        filename = match.group(1) if match else f"{document_id}.pdf"
        return response.content, filename

    def request_deletion(self, document_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Queue verified deletion of an eligible catalog document."""

        body = {"reason": reason} if reason else None
        return self._request(
            "POST",
            f"/api/v1/documents/{document_id}/deletion",
            mutation=True,
            json=body,
        ).json()

    # ------------------------------------------------------------------
    # Upload lifecycle
    # ------------------------------------------------------------------

    def preflight(self, *, filename: str, size_bytes: int, collection_key: str) -> dict[str, Any]:
        """Validate upload metadata and return advisory filename warnings."""

        return self._request(
            "POST",
            "/api/v1/uploads/preflight",
            mutation=True,
            json={
                "filename": filename,
                "size_bytes": size_bytes,
                "collection_key": collection_key,
            },
        ).json()

    def upload(
        self,
        *,
        filename: str,
        content: bytes | BinaryIO,
        collection_key: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Stream one PDF for scanning and analysis; returns the 202 payload."""

        return self._request(
            "POST",
            "/api/v1/uploads",
            mutation=True,
            idempotency_key=idempotency_key,
            files={"file": (filename, content, "application/pdf")},
            data={"collection_key": collection_key, "idempotency_key": idempotency_key},
            timeout=httpx.Timeout(UPLOAD_READ_TIMEOUT_SECONDS, connect=5.0),
        ).json()

    def uploads(
        self,
        *,
        open_only: bool = False,
        collection_key: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        """List durable upload workspace rows for queue restoration."""

        return self._get_json(
            "/api/v1/uploads",
            params={
                "open": open_only or None,
                "collection_key": collection_key,
                "page": page,
                "page_size": page_size,
            },
        )

    def upload_status(self, upload_id: str) -> dict[str, Any]:
        """Poll one upload's durable document, operation, and analysis state."""

        return self._get_json(f"/api/v1/uploads/{upload_id}")

    def upload_analysis(
        self, upload_id: str, *, page: int = 1, page_size: int = 10
    ) -> dict[str, Any]:
        """Page through deterministic and LLM candidate evidence."""

        return self._get_json(
            f"/api/v1/uploads/{upload_id}/analysis",
            params={"page": page, "page_size": page_size},
        )

    def decide(
        self,
        upload_id: str,
        *,
        analysis_revision: int,
        action: DecisionAction,
        target_document_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record an explicit Keep, Replace, or Cancel review decision."""

        body: dict[str, Any] = {"analysis_revision": analysis_revision, "action": action}
        if target_document_id is not None:
            body["target_document_id"] = target_document_id
        return self._request(
            "POST",
            f"/api/v1/uploads/{upload_id}/decision",
            mutation=True,
            idempotency_key=idempotency_key,
            json=body,
        ).json()

    def retry_upload(self, upload_id: str) -> dict[str, Any]:
        """Queue a new attempt for retained work whose last attempt failed."""

        return self._request(
            "POST", f"/api/v1/uploads/{upload_id}/retry", mutation=True
        ).json()

    def cancel_upload(self, upload_id: str) -> dict[str, Any]:
        """Cancel eligible unpublished work and queue its cleanup."""

        return self._request("DELETE", f"/api/v1/uploads/{upload_id}", mutation=True).json()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        query: str,
        mode: SearchMode,
        collections: list[str],
        include_hits: bool,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Proxy the external retrieval contract for the operator workspace."""

        return self._request(
            "POST",
            "/api/v1/search",
            mutation=True,
            json={
                "query": query,
                "mode": mode,
                "collections": collections,
                "include_hits": include_hits,
                "page": page,
                "page_size": page_size,
            },
        ).json()
