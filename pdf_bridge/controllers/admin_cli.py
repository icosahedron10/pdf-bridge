"""Typer controller for controlled PDF Bridge maintenance."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from pdf_bridge.core.config import get_settings
from pdf_bridge.managers.importing import run_manifest_import
from pdf_bridge.persistence.db import session_scope
from pdf_bridge.services.scanner import scanner_from_settings

app = typer.Typer(
    name="pdf-bridge",
    no_args_is_help=True,
    help="Run local, audited PDF Bridge administration.",
)


@app.callback()
def main() -> None:
    """Run local, audited PDF Bridge administration."""


def _print_failure(exc: Exception) -> None:
    if isinstance(exc, ValidationError):
        detail = f"configuration, manifest, or import data is invalid: {exc}"
    else:
        detail = str(exc)
    typer.echo(f"error: {detail}", err=True)


@app.command("import-manifest")
def import_manifest_command(
    manifest_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Strict version 3 historical import manifest.",
        ),
    ],
    source_root: Annotated[
        Path,
        typer.Option(
            "--source-root",
            exists=True,
            file_okay=False,
            readable=True,
            help="Explicit root beneath which every manifest PDF must resolve.",
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply",
            help="Validate, copy, hash, and scan without catalog/canonical changes, or apply.",
        ),
    ] = True,
    actor_id: Annotated[
        str,
        typer.Option(
            "--actor-id",
            help="Non-secret change/operator identifier written to apply-mode audit events.",
        ),
    ] = "local-import",
) -> None:
    """Validate or queue a controlled import through normal semantic intake."""

    try:
        response = run_manifest_import(
            manifest_path=manifest_path,
            source_root=source_root,
            dry_run=dry_run,
            actor_id=actor_id,
            settings_provider=get_settings,
            scanner_factory=scanner_from_settings,
            session_scope_factory=session_scope,
        )
        typer.echo(response.model_dump_json(indent=2))
    except Exception as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
