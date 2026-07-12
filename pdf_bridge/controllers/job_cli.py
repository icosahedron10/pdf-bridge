"""Typer controller for the Jenkins-facing PDF Bridge job client."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import httpx
import typer
from pydantic import BaseModel, ValidationError

from pdf_bridge.contracts.job_contracts import ClientOptions
from pdf_bridge.managers import job_client
from pdf_bridge.services.job_http import DEFAULT_BASE_URL, DEFAULT_TIMEOUT_SECONDS

app = typer.Typer(
    name="pdf-bridge-job",
    no_args_is_help=True,
    help="Claim PDF Bridge work, stage verified files, and report pipeline results.",
)


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

    try:
        result = job_client.pull_batch(
            destination=destination,
            request_id=request_id,
            limit=limit,
            result_file=result_file,
            client_options=ClientOptions(
                base_url=base_url,
                allowed_host=allowed_host,
                token_file=token_file,
                timeout_seconds=timeout_seconds,
                allow_http=allow_http,
                insecure_skip_tls_verify=insecure_skip_tls_verify,
                ca_bundle=ca_bundle,
            ),
        )
        _print_model(result)
    except Exception as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc


@app.command()
def report(
    report_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="Version 2 pipeline result JSON file."),
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
        response = job_client.report_batch(
            report_path=report_path,
            pull_result_path=pull_result_path,
            client_options=ClientOptions(
                base_url=base_url,
                allowed_host=allowed_host,
                token_file=token_file,
                timeout_seconds=timeout_seconds,
                allow_http=allow_http,
                insecure_skip_tls_verify=insecure_skip_tls_verify,
                ca_bundle=ca_bundle,
            ),
        )
        _print_model(response)
    except Exception as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
