"""Local administrative commands for controlled PDF Bridge maintenance."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from pdf_bridge.config import get_settings
from pdf_bridge.db import session_scope
from pdf_bridge.lifecycle import import_historical_manifest
from pdf_bridge.scanner import scanner_from_settings
from pdf_bridge.storage import StorageLayout

MAX_IMPORT_MANIFEST_BYTES = 32 * 1024 * 1024

app = typer.Typer(
    name="pdf-bridge",
    no_args_is_help=True,
    help="Run local, audited PDF Bridge administration.",
)


@app.callback()
def main() -> None:
    """Run local, audited PDF Bridge administration."""


def _validate_actor_id(actor_id: str) -> str:
    normalized = unicodedata.normalize("NFKC", actor_id).strip()
    if not normalized:
        raise ValueError("--actor-id cannot be blank")
    if len(normalized) > 255:
        raise ValueError("--actor-id must be 255 characters or fewer")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("--actor-id must not contain control characters")
    return normalized


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
            help="Strict version 2 historical import manifest.",
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
    """Validate or apply a controlled import of already-ingested historical PDFs."""

    try:
        resolved_manifest = manifest_path.expanduser().resolve(strict=True)
        resolved_source_root = source_root.expanduser().resolve(strict=True)
        if not resolved_manifest.is_file():
            raise ValueError("manifest path is not a regular file")
        if resolved_manifest.stat().st_size > MAX_IMPORT_MANIFEST_BYTES:
            raise ValueError(f"manifest exceeds the {MAX_IMPORT_MANIFEST_BYTES}-byte safety limit")
        if not resolved_source_root.is_dir():
            raise ValueError("source root is not a directory")

        settings = get_settings()
        layout = StorageLayout.from_root(settings.storage_root)
        if (
            layout.root == resolved_source_root
            or layout.root in resolved_source_root.parents
            or resolved_source_root in layout.root.parents
        ):
            raise ValueError("source root and bridge storage root must not contain one another")
        scanner = scanner_from_settings(settings)
        with session_scope() as session:
            response = import_historical_manifest(
                session,
                manifest_path=resolved_manifest,
                source_root=resolved_source_root,
                layout=layout,
                scanner=scanner,
                max_bytes=settings.max_upload_bytes,
                dry_run=dry_run,
                actor_id=_validate_actor_id(actor_id),
                configured_collections={collection.key for collection in settings.collections},
            )
        typer.echo(response.model_dump_json(indent=2))
    except Exception as exc:
        _print_failure(exc)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
