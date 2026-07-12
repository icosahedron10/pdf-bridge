"""Authenticated HTTP transport for the Jenkins-facing PDF Bridge client."""

from __future__ import annotations

import os
import ssl
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from pdf_bridge.contracts.job_contracts import BridgeClientError, ClientOptions
from pdf_bridge.contracts.schemas import (
    BatchClaimRequest,
    BatchClaimResponse,
    BatchManifestResponse,
    BatchResultsRequest,
    BatchResultsResponse,
    BatchStageRequest,
    BatchStageResponse,
)

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_JOB_TOKEN_CHARACTERS = 4096


def _problem_message(response: httpx.Response) -> str:
    request_id = response.headers.get("x-request-id")
    suffix = f" (request {request_id})" if request_id else ""
    content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            body = response.json()
        except (ValueError, httpx.ResponseNotRead):
            body = None
        if isinstance(body, dict):
            title = str(body.get("title") or response.reason_phrase or "Request failed")[:200]
            detail = str(body.get("detail", "")).strip()[:1000]
            code = str(body.get("code", "")).strip()[:100]
            message = f"{response.status_code} {title}"
            if code:
                message += f" [{code}]"
            if detail:
                message += f": {detail}"
            return message + suffix
    return f"{response.status_code} {response.reason_phrase}{suffix}"


def _ensure_success(response: httpx.Response) -> None:
    if not 200 <= response.status_code < 300:
        raise BridgeClientError(_problem_message(response))


def _ssl_verification(
    *, insecure_skip_tls_verify: bool, ca_bundle: Path | None
) -> bool | ssl.SSLContext:
    if insecure_skip_tls_verify and ca_bundle is not None:
        raise BridgeClientError("choose either --ca-bundle or --insecure-skip-tls-verify")
    if insecure_skip_tls_verify:
        return False
    if ca_bundle is None:
        return True
    bundle = ca_bundle.expanduser().resolve(strict=True)
    if not bundle.is_file():
        raise BridgeClientError(f"CA bundle is not a file: {bundle}")
    return ssl.create_default_context(cafile=str(bundle))


def _normalize_allowed_host(allowed_host: str) -> str:
    candidate = allowed_host.strip()
    if not candidate or any(
        character.isspace() or ord(character) < 32 for character in candidate
    ):
        raise BridgeClientError("--allowed-host must be a non-blank hostname without whitespace")
    parsed = urlsplit(f"//{candidate}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BridgeClientError(
            "--allowed-host must contain only a hostname, without a port"
        ) from exc
    if (
        not parsed.hostname
        or port is not None
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise BridgeClientError("--allowed-host must contain only a hostname, without a port")
    return parsed.hostname.rstrip(".").casefold()


def _validate_base_url(base_url: str, *, allowed_host: str, allow_http: bool) -> str:
    normalized = base_url.rstrip("/") + "/"
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BridgeClientError("--base-url must be an absolute HTTP(S) URL")
    pinned_host = _normalize_allowed_host(allowed_host)
    actual_host = parsed.hostname.rstrip(".").casefold()
    if actual_host != pinned_host:
        raise BridgeClientError(
            f"refusing to send the job token to {actual_host!r}; "
            f"--allowed-host pins it to {pinned_host!r}"
        )
    is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not is_loopback and not allow_http:
        raise BridgeClientError(
            "refusing to send the job token over HTTP; use HTTPS or explicitly pass --allow-http"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BridgeClientError(
            "--base-url must not contain credentials, a query, or a fragment"
        )
    return normalized


def _read_job_token(token_file: Path | None) -> str:
    if token_file is None:
        token = os.environ.get("PDF_BRIDGE_JOB_TOKEN", "")
        source = "PDF_BRIDGE_JOB_TOKEN"
    else:
        resolved = token_file.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise BridgeClientError(f"job token file is not a file: {resolved}")
        token = resolved.read_text(encoding="utf-8")
        source = str(resolved)
    token = token.strip()
    if not token:
        raise BridgeClientError(f"job token is empty or missing ({source})")
    if len(token) > MAX_JOB_TOKEN_CHARACTERS:
        raise BridgeClientError("job token exceeds the client safety limit")
    if any(character.isspace() or ord(character) < 32 for character in token):
        raise BridgeClientError("job token must not contain whitespace or control characters")
    return token


class BridgeJobClient:
    """Stateful authenticated client with an explicit connection lifecycle."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        verify: bool | ssl.SSLContext,
    ) -> None:
        if timeout_seconds <= 0:
            raise BridgeClientError("--timeout must be positive")
        self._base_url = httpx.URL(base_url)
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "pdf-bridge-job/0.1",
            },
            timeout=httpx.Timeout(timeout_seconds),
            verify=verify,
            follow_redirects=False,
        )

    def __enter__(self) -> BridgeJobClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._client.close()

    def claim(self, request: BatchClaimRequest) -> BatchClaimResponse | None:
        response = self._client.post(
            "api/v1/jobs/batches/claim", json=request.model_dump(mode="json")
        )
        if response.status_code == 204:
            return None
        _ensure_success(response)
        return BatchClaimResponse.model_validate_json(response.content)

    def manifest(self, batch_id: uuid.UUID) -> BatchManifestResponse:
        response = self._client.get(f"api/v1/jobs/batches/{batch_id}/manifest")
        _ensure_success(response)
        return BatchManifestResponse.model_validate_json(response.content)

    def stream_operation(self, download_url: str) -> AbstractContextManager[httpx.Response]:
        target = httpx.URL(download_url)
        resolved = self._base_url.join(target)
        if (
            resolved.scheme != self._base_url.scheme
            or resolved.host != self._base_url.host
            or resolved.port != self._base_url.port
        ):
            raise BridgeClientError("server returned a cross-origin operation download URL")
        if resolved.username or resolved.password or resolved.fragment:
            raise BridgeClientError("server returned an invalid operation download URL")
        return self._client.stream("GET", resolved)

    def acknowledge_staged(
        self, batch_id: uuid.UUID, request: BatchStageRequest
    ) -> BatchStageResponse:
        response = self._client.post(
            f"api/v1/jobs/batches/{batch_id}/staged",
            json=request.model_dump(mode="json"),
        )
        _ensure_success(response)
        return BatchStageResponse.model_validate_json(response.content)

    def report_results(
        self, batch_id: uuid.UUID, request: BatchResultsRequest
    ) -> BatchResultsResponse:
        response = self._client.post(
            f"api/v1/jobs/batches/{batch_id}/results",
            json=request.model_dump(mode="json"),
        )
        _ensure_success(response)
        return BatchResultsResponse.model_validate_json(response.content)


def _client(
    *,
    base_url: str,
    allowed_host: str,
    token_file: Path | None,
    timeout_seconds: float,
    allow_http: bool,
    insecure_skip_tls_verify: bool,
    ca_bundle: Path | None,
) -> BridgeJobClient:
    normalized_url = _validate_base_url(
        base_url,
        allowed_host=allowed_host,
        allow_http=allow_http,
    )
    token = _read_job_token(token_file)
    verify = _ssl_verification(
        insecure_skip_tls_verify=insecure_skip_tls_verify,
        ca_bundle=ca_bundle,
    )
    return BridgeJobClient(
        base_url=normalized_url,
        token=token,
        timeout_seconds=timeout_seconds,
        verify=verify,
    )


def client_from_options(options: ClientOptions) -> BridgeJobClient:
    return _client(
        base_url=options.base_url,
        allowed_host=options.allowed_host,
        token_file=options.token_file,
        timeout_seconds=options.timeout_seconds,
        allow_http=options.allow_http,
        insecure_skip_tls_verify=options.insecure_skip_tls_verify,
        ca_bundle=options.ca_bundle,
    )
