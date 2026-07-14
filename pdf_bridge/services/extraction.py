"""Resource-bounded, page-scoped pypdf layout extraction."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

EXTRACTION_PROFILE = "pypdf/6.14.2:layout:nfkc-controls-v1"
MINIMUM_ALPHANUMERIC_CHARACTERS = 80
LANGUAGE_SAMPLE_CHARACTERS = 50_000
LANGUAGE_PROFILE = (
    "lingua-language-detector/2.2.0:all-languages:v1:"
    f"minimum-alphanumeric={MINIMUM_ALPHANUMERIC_CHARACTERS}:"
    f"sample-characters={LANGUAGE_SAMPLE_CHARACTERS}"
)

RejectionReason = Literal[
    "encrypted",
    "malformed",
    "empty",
    "page-budget",
    "character-budget",
    "timeout",
    "image-only",
    "text-insufficient",
    "non-english",
]


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_pages: int
    max_characters: int
    cpu_seconds: int
    memory_bytes: int
    wall_clock_seconds: float


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int
    layout_text: str
    character_count: int
    text_sha256: str


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    page_count: int
    character_count: int
    pages: tuple[ExtractedPage, ...]


class EnglishDetector(Protocol):
    def detect_language(self, text: str) -> str | None: ...


class ExtractionRejectedError(RuntimeError):
    """The PDF is outside the supported native-English corpus."""

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class ExtractionInfrastructureError(RuntimeError):
    """The extraction or language-classification boundary could not run."""


def normalize_layout_text(text: str) -> str:
    """Normalize safely while preserving spaces that communicate table columns."""

    candidate = unicodedata.normalize("NFKC", text)
    candidate = candidate.replace("\r\n", "\n").replace("\r", "\n")
    candidate = "".join(
        character
        for character in candidate
        if character in {"\n", "\t"} or unicodedata.category(character) != "Cc"
    )
    lines = [line.rstrip() for line in candidate.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _parse_child_payload(payload: object, limits: ExtractionLimits) -> ExtractedDocument:
    if not isinstance(payload, dict):
        raise ExtractionRejectedError("malformed", "the PDF parser returned an invalid result")
    if not payload.get("ok"):
        reason = payload.get("reason")
        allowed = {"encrypted", "malformed", "empty", "page-budget", "character-budget"}
        if reason not in allowed:
            reason = "malformed"
        raise ExtractionRejectedError(
            reason, str(payload.get("detail", "the PDF could not be parsed"))[:500]
        )
    page_count = payload.get("page_count")
    raw_pages = payload.get("pages")
    if not isinstance(page_count, int) or not isinstance(raw_pages, list):
        raise ExtractionRejectedError("malformed", "the PDF parser result was incomplete")
    if page_count <= 0 or page_count != len(raw_pages) or page_count > limits.max_pages:
        raise ExtractionRejectedError("malformed", "the PDF parser returned invalid page coverage")

    pages: list[ExtractedPage] = []
    total = 0
    for expected, raw_page in enumerate(raw_pages, start=1):
        if not isinstance(raw_page, dict):
            raise ExtractionRejectedError("malformed", "the PDF parser returned an invalid page")
        page_number = raw_page.get("page_number")
        text = raw_page.get("layout_text")
        if page_number != expected or not isinstance(text, str):
            raise ExtractionRejectedError("malformed", "the PDF parser reordered or omitted pages")
        normalized = normalize_layout_text(text)
        total += len(normalized)
        if total > limits.max_characters:
            raise ExtractionRejectedError(
                "character-budget",
                f"normalized layout text exceeds {limits.max_characters} characters",
            )
        pages.append(
            ExtractedPage(
                page_number=page_number,
                layout_text=normalized,
                character_count=len(normalized),
                text_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            )
        )
    return ExtractedDocument(page_count=page_count, character_count=total, pages=tuple(pages))


def extract_pdf_layout(path: Path, limits: ExtractionLimits) -> ExtractedDocument:
    """Run pypdf layout extraction in a bounded child process."""

    if min(
        limits.max_pages,
        limits.max_characters,
        limits.cpu_seconds,
        limits.memory_bytes,
    ) <= 0 or limits.wall_clock_seconds <= 0:
        raise ValueError("all extraction limits must be positive")
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
            check=False,
            env={
                **{
                    name: value
                    for name in ("SYSTEMROOT", "WINDIR")
                    if (value := os.environ.get(name)) is not None
                },
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionRejectedError(
            "timeout", f"PDF parsing exceeded {limits.wall_clock_seconds:g} seconds"
        ) from exc
    except OSError as exc:
        raise ExtractionInfrastructureError("the extraction child could not be started") from exc
    if completed.returncode != 0:
        raise ExtractionRejectedError(
            "malformed", f"the PDF parser exited with status {completed.returncode}"
        )
    try:
        payload = json.loads(completed.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExtractionRejectedError(
            "malformed", "the PDF parser returned unreadable output"
        ) from exc
    return _parse_child_payload(payload, limits)


def _substantive_text(document: ExtractedDocument) -> str:
    return "\n".join(page.layout_text for page in document.pages if page.layout_text)


def validate_native_english(
    document: ExtractedDocument,
    detector: EnglishDetector,
    *,
    minimum_alphanumeric: int = MINIMUM_ALPHANUMERIC_CHARACTERS,
) -> None:
    """Reject image-only, text-insufficient, or non-English extractions."""

    text = _substantive_text(document)
    alphanumeric = sum(character.isalnum() for character in text)
    if not text.strip():
        raise ExtractionRejectedError("image-only", "the PDF has no embedded text")
    if alphanumeric < minimum_alphanumeric:
        raise ExtractionRejectedError(
            "text-insufficient",
            f"the PDF has fewer than {minimum_alphanumeric} usable text characters",
        )
    sample = text[:LANGUAGE_SAMPLE_CHARACTERS]
    try:
        language = detector.detect_language(sample)
    except Exception as exc:
        raise ExtractionInfrastructureError("English language detection failed") from exc
    if language is None:
        raise ExtractionInfrastructureError("English language detection was inconclusive")
    if language.casefold() not in {"en", "eng", "english"}:
        raise ExtractionRejectedError("non-english", "only English native-text PDFs are supported")


class LinguaEnglishDetector:
    """Offline high-accuracy language detector loaded without remote assets."""

    def __init__(self) -> None:
        try:
            from lingua import Language, LanguageDetectorBuilder
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise ExtractionInfrastructureError(
                "lingua-language-detector is not installed"
            ) from exc
        self._english = Language.ENGLISH
        self._detector = LanguageDetectorBuilder.from_all_languages().build()

    def detect_language(self, text: str) -> str | None:
        language = self._detector.detect_language_of(text)
        if language is None:
            return None
        return "english" if language == self._english else str(language)
