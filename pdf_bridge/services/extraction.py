"""Parent-side runner for the resource-limited PDF extraction subprocess.

Extraction failures are terminal, non-overridable rejections by design:
encrypted, malformed, image-only, and over-budget PDFs never reach review.
Only the subprocess machinery itself failing (for example, the Python
executable missing) raises ``ExtractionInfrastructureError`` instead.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pdf_bridge.services.chunking import PageText

EXTRACTION_VERSION = "pypdf/6.14.2"

RejectionReason = Literal[
    "encrypted",
    "malformed",
    "page-budget",
    "character-budget",
    "timeout",
]


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    """Safety limits applied to one extraction subprocess."""

    max_pages: int
    max_characters: int
    cpu_seconds: int
    memory_bytes: int
    wall_clock_seconds: float


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """Successful page-mapped extraction result."""

    page_count: int
    pages: list[PageText]


class ExtractionRejectedError(RuntimeError):
    """Terminal, non-overridable parser rejection of an uploaded PDF."""

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class ExtractionInfrastructureError(RuntimeError):
    """The extraction subprocess could not be run at all; retryable."""


def extract_pdf_text(path: Path, limits: ExtractionLimits) -> ExtractedDocument:
    """Extract page text in a subprocess, enforcing every configured limit."""

    command = [
        sys.executable,
        "-m",
        "pdf_bridge.services.extraction_child",
        str(path),
        "--max-pages",
        str(limits.max_pages),
        "--max-characters",
        str(limits.max_characters),
        "--cpu-seconds",
        str(limits.cpu_seconds),
        "--memory-bytes",
        str(limits.memory_bytes),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=limits.wall_clock_seconds,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionRejectedError(
            "timeout",
            f"PDF parsing exceeded the {limits.wall_clock_seconds:g}-second limit",
        ) from exc
    except OSError as exc:
        raise ExtractionInfrastructureError(
            f"the extraction subprocess could not be started: {exc}"
        ) from exc

    if completed.returncode != 0:
        # Killed by a resource limit or crashed inside the parser: both are
        # hostile-input shapes and therefore terminal rejections.
        detail = completed.stderr.decode("utf-8", errors="replace")[-2000:]
        raise ExtractionRejectedError(
            "malformed",
            f"the PDF parser exited with status {completed.returncode}: {detail}".strip(),
        )
    try:
        payload = json.loads(completed.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExtractionRejectedError(
            "malformed", "the PDF parser returned unreadable output"
        ) from exc

    if not payload.get("ok"):
        reason = payload.get("reason")
        detail = str(payload.get("detail", "the PDF could not be parsed"))
        if reason not in {"encrypted", "malformed", "page-budget", "character-budget"}:
            reason = "malformed"
        raise ExtractionRejectedError(reason, detail)

    pages_payload = payload.get("pages")
    page_count = payload.get("page_count")
    if not isinstance(pages_payload, list) or not isinstance(page_count, int):
        raise ExtractionRejectedError("malformed", "the PDF parser output was incomplete")
    pages: list[PageText] = []
    for entry in pages_payload:
        number = entry.get("number") if isinstance(entry, dict) else None
        text = entry.get("text") if isinstance(entry, dict) else None
        if not isinstance(number, int) or not isinstance(text, str):
            raise ExtractionRejectedError("malformed", "the PDF parser output was invalid")
        pages.append(PageText(number=number, text=text))
    return ExtractedDocument(page_count=page_count, pages=pages)
