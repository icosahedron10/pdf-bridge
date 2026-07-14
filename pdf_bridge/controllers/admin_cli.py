"""Operator CLI for safe API-v2 reingestion."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pdf_bridge.services.reingestion import (
    ReingestionClient,
    ReingestionError,
    apply_manifest,
    validate_manifest,
)

app = typer.Typer(
    name="pdf-bridge",
    no_args_is_help=True,
    help="Run API-v2 PDF Bridge maintenance clients.",
)


@app.callback()
def main() -> None:
    """Run explicit PDF Bridge maintenance clients."""


@app.command("reingest-manifest")
def reingest_manifest_command(
    manifest_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True),
    ],
    source_root: Annotated[
        Path,
        typer.Option("--source-root", exists=True, file_okay=False, readable=True),
    ],
    bridge_storage_root: Annotated[
        Path,
        typer.Option(
            "--bridge-storage-root",
            help="Deployment storage root used only for source-overlap validation.",
        ),
    ],
    bridge_url: Annotated[
        str,
        typer.Option("--bridge-url", help="Private PDF Bridge API base URL."),
    ],
    apply: Annotated[
        bool,
        typer.Option("--apply/--dry-run", help="Accept files through API v2 or validate only."),
    ] = False,
    state_path: Annotated[
        Path | None,
        typer.Option("--state", help="Content-free resumable acceptance state JSON."),
    ] = None,
    wait_seconds: Annotated[
        float,
        typer.Option(
            min=0,
            help="Seconds to poll for capacity; zero accepts at most five and returns.",
        ),
    ] = 0.0,
    identity_header: Annotated[
        str | None,
        typer.Option(help="Optional trusted identity header name."),
    ] = None,
    identity: Annotated[
        str | None,
        typer.Option(help="Optional trusted identity value."),
    ] = None,
    ca_bundle: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, readable=True),
    ] = None,
) -> None:
    """Validate or resume a strict version-4 manifest through ordinary API v2."""

    try:
        manifest = validate_manifest(
            manifest_path,
            source_root=source_root,
            bridge_storage_root=bridge_storage_root,
        )
        verify: bool | str = str(ca_bundle) if ca_bundle is not None else True
        with ReingestionClient(
            bridge_url,
            verify=verify,
            identity_header=identity_header,
            identity=identity,
        ) as client:
            if not apply:
                collections = client.bootstrap()
                missing = sorted(
                    {item.collection_key for item in manifest.documents} - collections
                )
                if missing:
                    raise ReingestionError(
                        "Manifest uses unavailable collections: " + ", ".join(missing)
                    )
                typer.echo(
                    f"valid manifest sha256:{manifest.manifest_sha256} "
                    f"documents={len(manifest.documents)} bytes={manifest.total_bytes}"
                )
                return
            resolved_state = state_path or manifest_path.with_suffix(
                manifest_path.suffix + ".reingestion-state.json"
            )
            state = apply_manifest(
                manifest,
                state_path=resolved_state,
                client=client,
                wait_seconds=wait_seconds,
            )
            terminal = sum(
                item.state in {"READY", "REJECTED", "CANCELLED", "DELETED"}
                for item in state.accepted.values()
            )
            typer.echo(
                f"manifest sha256:{manifest.manifest_sha256} "
                f"accepted={len(state.accepted)}/{len(manifest.documents)} "
                f"terminal={terminal} state={resolved_state}"
            )
    except ReingestionError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
