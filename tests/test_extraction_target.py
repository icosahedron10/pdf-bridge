from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf_bridge.services import extraction
from pdf_bridge.services.extraction import (
    LANGUAGE_PROFILE,
    LANGUAGE_SAMPLE_CHARACTERS,
    MINIMUM_ALPHANUMERIC_CHARACTERS,
    ExtractedDocument,
    ExtractedPage,
    ExtractionInfrastructureError,
    ExtractionLimits,
    ExtractionRejectedError,
    _parse_child_payload,
    normalize_layout_text,
    validate_native_english,
)


class Detector:
    def __init__(self, result: str | None = "english", *, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.seen_text = ""

    def detect_language(self, text: str) -> str | None:
        self.seen_text = text
        if self.fail:
            raise RuntimeError("offline detector failed")
        return self.result


def limits() -> ExtractionLimits:
    return ExtractionLimits(
        max_pages=10,
        max_characters=10_000,
        cpu_seconds=5,
        memory_bytes=128 * 1024 * 1024,
        wall_clock_seconds=10,
    )


def document(text: str) -> ExtractedDocument:
    page = ExtractedPage(
        page_number=1,
        layout_text=text,
        character_count=len(text),
        text_sha256="a" * 64,
    )
    return ExtractedDocument(page_count=1, character_count=len(text), pages=(page,))


def test_layout_normalization_preserves_columns_and_hashes_pages() -> None:
    payload = {
        "ok": True,
        "page_count": 2,
        "pages": [
            {"page_number": 1, "layout_text": "\r\nName     Value  \r\nA        1\x00"},
            {"page_number": 2, "layout_text": "Second page"},
        ],
    }

    extracted = _parse_child_payload(payload, limits())

    assert extracted.pages[0].layout_text == "Name     Value\nA        1"
    assert extracted.pages[0].text_sha256 != extracted.pages[1].text_sha256
    assert normalize_layout_text("A    B") == "A    B"


def test_parser_coverage_and_character_limits_fail_closed() -> None:
    with pytest.raises(ExtractionRejectedError, match="reordered or omitted"):
        _parse_child_payload(
            {
                "ok": True,
                "page_count": 1,
                "pages": [{"page_number": 2, "layout_text": "wrong"}],
            },
            limits(),
        )
    tight = limits()
    tight = ExtractionLimits(
        max_pages=tight.max_pages,
        max_characters=3,
        cpu_seconds=tight.cpu_seconds,
        memory_bytes=tight.memory_bytes,
        wall_clock_seconds=tight.wall_clock_seconds,
    )
    with pytest.raises(ExtractionRejectedError) as raised:
        _parse_child_payload(
            {
                "ok": True,
                "page_count": 1,
                "pages": [{"page_number": 1, "layout_text": "long"}],
            },
            tight,
        )
    assert raised.value.reason == "character-budget"


def test_native_english_gate_distinguishes_rejection_from_infrastructure() -> None:
    english = ("This is a complete English installation guide for operators. " * 4).strip()
    validate_native_english(document(english), Detector())

    with pytest.raises(ExtractionRejectedError) as image_only:
        validate_native_english(document(""), Detector())
    assert image_only.value.reason == "image-only"

    with pytest.raises(ExtractionRejectedError) as non_english:
        validate_native_english(document(english), Detector("FRENCH"))
    assert non_english.value.reason == "non-english"

    with pytest.raises(ExtractionInfrastructureError, match="inconclusive"):
        validate_native_english(document(english), Detector(None))
    with pytest.raises(ExtractionInfrastructureError, match="failed"):
        validate_native_english(document(english), Detector(fail=True))


def test_native_text_gate_is_bounded_and_has_a_profile_identity() -> None:
    detector = Detector()
    validate_native_english(document("A" * (LANGUAGE_SAMPLE_CHARACTERS + 100)), detector)

    assert len(detector.seen_text) == LANGUAGE_SAMPLE_CHARACTERS
    assert f"minimum-alphanumeric={MINIMUM_ALPHANUMERIC_CHARACTERS}" in LANGUAGE_PROFILE
    assert f"sample-characters={LANGUAGE_SAMPLE_CHARACTERS}" in LANGUAGE_PROFILE


def test_parser_child_receives_no_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_environment: dict[str, str] = {}

    def run(_command: list[str], **kwargs: object) -> SimpleNamespace:
        seen_environment.update(kwargs["env"])  # type: ignore[arg-type]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "page_count": 1,
                    "pages": [{"page_number": 1, "layout_text": "English content"}],
                }
            ).encode("utf-8"),
        )

    monkeypatch.setenv("PDF_BRIDGE_QDRANT_API_KEY", "must-not-reach-parser")
    monkeypatch.setenv("HTTPS_PROXY", "http://credential@example.test")
    monkeypatch.setattr(extraction.subprocess, "run", run)

    result = extraction.extract_pdf_layout(Path("staged.pdf"), limits())

    assert result.page_count == 1
    assert seen_environment["PYTHONUTF8"] == "1"
    assert seen_environment["PYTHONIOENCODING"] == "utf-8"
    assert not any(name.startswith("PDF_BRIDGE_") for name in seen_environment)
    assert "HTTPS_PROXY" not in seen_environment
