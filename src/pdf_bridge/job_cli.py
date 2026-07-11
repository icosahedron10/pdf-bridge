"""Jenkins-facing client for atomic batch staging and result reporting."""

from __future__ import annotations

import hashlib
import os
import shutil
import ssl
import tempfile
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pdf_bridge.models import OperationType
from pdf_bridge.schemas import (
    BatchClaimRequest,
    BatchClaimResponse,
    BatchManifestItem,
    BatchManifestResponse,
    BatchResultsRequest,
    BatchResultsResponse,
    BatchStageRequest,
    BatchStageResponse,
    OperationResultInput,
)

MANIFEST_FILENAME = "manifest.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 60.0
COPY_CHUNK_BYTES = 1024 * 1024
MAX_JOB_TOKEN_CHARACTERS = 4096
MAX_PULL_RESULT_BYTES = 64 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_STAGED_MANIFEST_BYTES = 2 * 1024 * 1024

app = typer.Typer(
    name="pdf-bridge-job",
    no_args_is_help=True,
    help="Claim PDF Bridge work, stage verified files, and report pipeline results.",
)


class CliModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StagedManifestItem(CliModel):
    operation_id: uuid.UUID
    document_id: uuid.UUID
    operation_type: OperationType
    filename: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    local_path: str | None = None

    @model_validator(mode="after")
    def require_ingest_file(self) -> StagedManifestItem:
        if self.operation_type == OperationType.INGEST and not self.local_path:
            raise ValueError("INGEST operations require local_path")
        if self.operation_type == OperationType.DELETE and self.local_path is not None:
            raise ValueError("DELETE operations must not include local_path")
        return self


class StagedManifest(CliModel):
    version: Literal[1] = 1
    batch_id: uuid.UUID
    request_id: str
    claimed_at: str
    lease_expires_at: str
    operations: list[StagedManifestItem]


class PullResult(CliModel):
    version: Literal[1] = 1
    batch_id: uuid.UUID | None
    request_id: str
    operation_count: int = Field(ge=0)
    batch_directory: str | None
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    idempotent_replay: bool = False


class ReportFile(CliModel):
    version: Literal[1] = 1
    batch_id: uuid.UUID
    pipeline_run_id: str = Field(min_length=1, max_length=255)
    results: list[OperationResultInput] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_results(self) -> ReportFile:
        operation_ids = [result.operation_id for result in self.results]
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("results must contain exactly one entry per operation")
        return self


class BridgeClientError(RuntimeError):
    """A safe, user-facing error from the Jenkins HTTP boundary."""


def _problem_message(response: httpx.Response) -> str:
    request_id = response.headers.get("x-request-id")
    suffix = f" (request {request_id})" if request_id else ""
    content_type = response.headers.get("content-type", "").partition(";")[0].casefold()
    if content_type == "application/problem+json":
        try:
            body = response.json()
        except (ValueError, httpx.ResponseNotRead):
            body = None
        if isinstance(body, dict):
            title = str(body.get("title", "Request failed"))[:200]
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
    if not candidate or any(character.isspace() or ord(character) < 32 for character in candidate):
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
        raise BridgeClientError("--base-url must not contain credentials, a query, or a fragment")
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


def _manifest_item(item: BatchManifestItem) -> StagedManifestItem:
    local_path = None
    if item.operation_type == OperationType.INGEST:
        local_path = f"files/{item.operation_id}.pdf"
    return StagedManifestItem(
        operation_id=item.operation_id,
        document_id=item.document_id,
        operation_type=item.operation_type,
        filename=item.filename,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        local_path=local_path,
    )


def _local_manifest(remote: BatchManifestResponse) -> StagedManifest:
    if remote.version != 1:
        raise BridgeClientError(f"unsupported server manifest version: {remote.version}")
    return StagedManifest(
        batch_id=remote.batch_id,
        request_id=remote.request_id,
        claimed_at=remote.claimed_at.isoformat(),
        lease_expires_at=remote.lease_expires_at.isoformat(),
        operations=[_manifest_item(item) for item in remote.operations],
    )


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(COPY_CHUNK_BYTES), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _download_operation(
    client: BridgeJobClient,
    remote: BatchManifestItem,
    destination: Path,
) -> None:
    if remote.operation_type != OperationType.INGEST:
        raise BridgeClientError("internal error: attempted to download a DELETE operation")
    if not remote.download_url:
        raise BridgeClientError(f"INGEST operation {remote.operation_id} has no download_url")

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with client.stream_operation(remote.download_url) as response:
        _ensure_success(response)
        with destination.open("xb") as output:
            for block in response.iter_bytes(COPY_CHUNK_BYTES):
                size += len(block)
                if size > remote.size_bytes:
                    raise BridgeClientError(
                        f"operation {remote.operation_id} exceeded declared size "
                        f"{remote.size_bytes}"
                    )
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())

    if size != remote.size_bytes:
        raise BridgeClientError(
            f"operation {remote.operation_id} size mismatch: "
            f"expected {remote.size_bytes}, got {size}"
        )
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != remote.sha256:
        raise BridgeClientError(
            f"operation {remote.operation_id} checksum mismatch: "
            f"expected {remote.sha256}, got {actual_sha256}"
        )


def _manifest_bytes(manifest: StagedManifest) -> bytes:
    body = manifest.model_dump_json(indent=2).encode("utf-8")
    return body + b"\n"


def _write_new_file(path: Path, body: bytes) -> None:
    with path.open("xb") as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())


def _validate_existing_batch(final_directory: Path, expected: StagedManifest) -> str:
    if final_directory.is_symlink() or not final_directory.is_dir():
        raise BridgeClientError(f"existing batch path is not a real directory: {final_directory}")
    manifest_path = final_directory / MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BridgeClientError(f"existing batch has no regular {MANIFEST_FILENAME}")
    if manifest_path.stat().st_size > MAX_STAGED_MANIFEST_BYTES:
        raise BridgeClientError("existing batch manifest exceeds the client safety limit")
    try:
        actual = StagedManifest.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise BridgeClientError(f"existing batch manifest is invalid: {exc}") from exc
    if actual != expected:
        raise BridgeClientError("existing batch manifest does not match the server batch")

    for operation in actual.operations:
        if operation.local_path is None:
            continue
        path = final_directory / Path(operation.local_path)
        if path.is_symlink() or not path.is_file():
            raise BridgeClientError(
                f"staged file is missing or is a symlink: {operation.local_path}"
            )
        actual_sha256, actual_size = _sha256_file(path)
        if actual_size != operation.size_bytes or actual_sha256 != operation.sha256:
            raise BridgeClientError(f"staged file failed verification: {operation.local_path}")
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def _stage_new_batch(
    client: BridgeJobClient,
    destination_root: Path,
    remote: BatchManifestResponse,
    manifest: StagedManifest,
) -> tuple[Path, str]:
    final_directory = destination_root / str(remote.batch_id)
    if final_directory.exists() or final_directory.is_symlink():
        checksum = _validate_existing_batch(final_directory, manifest)
        return final_directory, checksum

    temporary_directory = Path(
        tempfile.mkdtemp(prefix=f".{remote.batch_id}.tmp-", dir=destination_root)
    )
    try:
        remote_by_id = {item.operation_id: item for item in remote.operations}
        if len(remote_by_id) != len(remote.operations):
            raise BridgeClientError("server manifest contains duplicate operation IDs")
        for item in manifest.operations:
            if item.local_path is None:
                continue
            _download_operation(
                client,
                remote_by_id[item.operation_id],
                temporary_directory / Path(item.local_path),
            )

        manifest_body = _manifest_bytes(manifest)
        _write_new_file(temporary_directory / MANIFEST_FILENAME, manifest_body)
        manifest_sha256 = hashlib.sha256(manifest_body).hexdigest()
        os.replace(temporary_directory, final_directory)
        return final_directory, manifest_sha256
    except Exception as original_error:
        try:
            shutil.rmtree(temporary_directory)
        except OSError as cleanup_error:
            raise BridgeClientError(
                f"batch staging failed and temporary cleanup also failed at "
                f"{temporary_directory}: {cleanup_error}"
            ) from original_error
        raise


def _write_json_result(path: Path, model: BaseModel) -> None:
    resolved = path.expanduser().resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.tmp-{uuid.uuid4().hex}")
    body = model.model_dump_json(indent=2).encode("utf-8") + b"\n"
    try:
        _write_new_file(temporary, body)
        os.replace(temporary, resolved)
    finally:
        temporary.unlink(missing_ok=True)


def _print_model(model: BaseModel) -> None:
    typer.echo(model.model_dump_json(indent=2))


def _print_failure(exc: Exception) -> None:
    if isinstance(exc, httpx.RequestError):
        detail = f"could not reach PDF Bridge: {exc}"
    elif isinstance(exc, ValidationError):
        detail = f"PDF Bridge returned or received invalid data: {exc}"
    else:
        detail = str(exc)
    typer.echo(f"error: {detail}", err=True)


def _read_report(path: Path) -> ReportFile:
    if path.is_symlink() or not path.is_file():
        raise BridgeClientError("report must be a regular, non-symlink file")
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise BridgeClientError(f"report exceeds the {MAX_REPORT_BYTES}-byte client safety limit")
    return ReportFile.model_validate_json(path.read_bytes())


def _read_pull_result(path: Path) -> PullResult:
    if path.is_symlink() or not path.is_file():
        raise BridgeClientError("pull result must be a regular, non-symlink file")
    if path.stat().st_size > MAX_PULL_RESULT_BYTES:
        raise BridgeClientError(
            f"pull result exceeds the {MAX_PULL_RESULT_BYTES}-byte client safety limit"
        )
    return PullResult.model_validate_json(path.read_bytes())


def _validate_destination_root(destination: Path) -> Path:
    resolved = destination.expanduser().resolve(strict=False)
    if any(part.casefold().startswith("onedrive") for part in resolved.parts):
        raise BridgeClientError("--destination must not be inside a OneDrive-synchronized path")
    workspace_value = os.environ.get("WORKSPACE")
    if workspace_value:
        workspace = Path(workspace_value).expanduser().resolve(strict=False)
        try:
            resolved.relative_to(workspace)
        except ValueError:
            pass
        else:
            raise BridgeClientError("--destination must be outside the Jenkins workspace")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    if resolved.is_symlink() or not resolved.is_dir():
        raise BridgeClientError("--destination must resolve to a real directory")
    return resolved


@app.command()
def pull(
    destination: Annotated[
        Path,
        typer.Option(
            "--destination",
            file_okay=False,
            dir_okay=True,
            help="External directory under which the immutable batch directory is created.",
        ),
    ],
    allowed_host: Annotated[
        str,
        typer.Option(
            "--allowed-host",
            envvar="PDF_BRIDGE_JOB_ALLOWED_HOST",
            help="Exact hostname allowed to receive the Jenkins bearer token.",
        ),
    ],
    request_id: Annotated[
        str | None,
        typer.Option(
            "--request-id",
            help="Stable Jenkins run ID. Reuse it when retrying the same scheduled handoff.",
        ),
    ] = None,
    base_url: Annotated[
        str,
        typer.Option("--base-url", envvar="PDF_BRIDGE_URL", help="PDF Bridge service URL."),
    ] = DEFAULT_BASE_URL,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=500, help="Maximum operations to claim.")
    ] = 100,
    result_file: Annotated[
        Path | None,
        typer.Option("--result-file", help="Also atomically write the pull summary as JSON."),
    ] = None,
    token_file: Annotated[
        Path | None,
        typer.Option(
            "--token-file",
            exists=True,
            dir_okay=False,
            help="Read the job token from a credential file instead of PDF_BRIDGE_JOB_TOKEN.",
        ),
    ] = None,
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", min=1.0, help="Per-request timeout in seconds.")
    ] = DEFAULT_TIMEOUT_SECONDS,
    ca_bundle: Annotated[
        Path | None,
        typer.Option("--ca-bundle", exists=True, dir_okay=False, help="Private CA PEM bundle."),
    ] = None,
    insecure_skip_tls_verify: Annotated[
        bool,
        typer.Option(
            "--insecure-skip-tls-verify",
            help="Disable TLS verification for local diagnosis only.",
        ),
    ] = False,
    allow_http: Annotated[
        bool,
        typer.Option("--allow-http", help="Allow the bearer token over non-loopback HTTP."),
    ] = False,
) -> None:
    """Claim a batch and atomically stage all checksum-verified ingest PDFs."""

    stable_request_id = request_id or f"manual-{uuid.uuid4()}"
    try:
        claim_request = BatchClaimRequest(request_id=stable_request_id, limit=limit)
        destination_root = _validate_destination_root(destination)

        with _client(
            base_url=base_url,
            allowed_host=allowed_host,
            token_file=token_file,
            timeout_seconds=timeout_seconds,
            allow_http=allow_http,
            insecure_skip_tls_verify=insecure_skip_tls_verify,
            ca_bundle=ca_bundle,
        ) as client:
            claim = client.claim(claim_request)
            if claim is None:
                result = PullResult(
                    batch_id=None,
                    request_id=stable_request_id,
                    operation_count=0,
                    batch_directory=None,
                )
            else:
                remote = client.manifest(claim.batch_id)
                if remote.request_id != stable_request_id:
                    raise BridgeClientError("claimed batch request_id does not match the request")
                if remote.batch_id != claim.batch_id:
                    raise BridgeClientError("claimed batch ID does not match its manifest")
                if claim.operation_count != len(remote.operations):
                    raise BridgeClientError("claimed operation count does not match the manifest")
                if not remote.operations:
                    result = PullResult(
                        batch_id=remote.batch_id,
                        request_id=stable_request_id,
                        operation_count=0,
                        batch_directory=None,
                        idempotent_replay=claim.idempotent_replay,
                    )
                else:
                    local = _local_manifest(remote)
                    final_directory, manifest_sha256 = _stage_new_batch(
                        client, destination_root, remote, local
                    )
                    operation_ids = [item.operation_id for item in remote.operations]
                    stage_response = client.acknowledge_staged(
                        remote.batch_id, BatchStageRequest(operation_ids=operation_ids)
                    )
                    result = PullResult(
                        batch_id=remote.batch_id,
                        request_id=stable_request_id,
                        operation_count=len(remote.operations),
                        batch_directory=str(final_directory),
                        manifest_sha256=manifest_sha256,
                        idempotent_replay=(
                            claim.idempotent_replay or stage_response.idempotent_replay
                        ),
                    )

        if result_file is not None:
            _write_json_result(result_file, result)
        _print_model(result)
    except Exception as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc


@app.command()
def report(
    report_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="Version 1 pipeline result JSON file."),
    ],
    pull_result_path: Annotated[
        Path,
        typer.Option(
            "--pull-result",
            exists=True,
            dir_okay=False,
            help="Pull summary whose batch ID must match the pipeline report.",
        ),
    ],
    allowed_host: Annotated[
        str,
        typer.Option(
            "--allowed-host",
            envvar="PDF_BRIDGE_JOB_ALLOWED_HOST",
            help="Exact hostname allowed to receive the Jenkins bearer token.",
        ),
    ],
    base_url: Annotated[
        str,
        typer.Option("--base-url", envvar="PDF_BRIDGE_URL", help="PDF Bridge service URL."),
    ] = DEFAULT_BASE_URL,
    token_file: Annotated[
        Path | None,
        typer.Option(
            "--token-file",
            exists=True,
            dir_okay=False,
            help="Read the job token from a credential file instead of PDF_BRIDGE_JOB_TOKEN.",
        ),
    ] = None,
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", min=1.0, help="Request timeout in seconds.")
    ] = DEFAULT_TIMEOUT_SECONDS,
    ca_bundle: Annotated[
        Path | None,
        typer.Option("--ca-bundle", exists=True, dir_okay=False, help="Private CA PEM bundle."),
    ] = None,
    insecure_skip_tls_verify: Annotated[
        bool,
        typer.Option(
            "--insecure-skip-tls-verify",
            help="Disable TLS verification for local diagnosis only.",
        ),
    ] = False,
    allow_http: Annotated[
        bool,
        typer.Option("--allow-http", help="Allow the bearer token over non-loopback HTTP."),
    ] = False,
) -> None:
    """Validate and submit one outcome for every operation in a staged batch."""

    try:
        parsed = _read_report(report_path)
        pull_result = _read_pull_result(pull_result_path)
        if pull_result.batch_id is None or pull_result.operation_count == 0:
            raise BridgeClientError(
                "pull result contains no batch, so pipeline results cannot be submitted"
            )
        if parsed.batch_id != pull_result.batch_id:
            raise BridgeClientError(
                "pipeline report batch_id does not match the current pull result batch_id"
            )
        request = BatchResultsRequest(
            pipeline_run_id=parsed.pipeline_run_id,
            results=parsed.results,
        )
        with _client(
            base_url=base_url,
            allowed_host=allowed_host,
            token_file=token_file,
            timeout_seconds=timeout_seconds,
            allow_http=allow_http,
            insecure_skip_tls_verify=insecure_skip_tls_verify,
            ca_bundle=ca_bundle,
        ) as client:
            response = client.report_results(parsed.batch_id, request)
        if response.batch_id != parsed.batch_id:
            raise BridgeClientError("PDF Bridge result response returned the wrong batch_id")
        _print_model(response)
    except Exception as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
