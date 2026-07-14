from __future__ import annotations

import hashlib
import json as jsonlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pdf_bridge.services.extraction import (
    ExtractionLimits,
    ExtractionRejectedError,
    LinguaEnglishDetector,
    extract_pdf_layout,
    validate_native_english,
)
from pdf_bridge.services.filenames import compare_filenames, profile_filename
from pdf_bridge.services.markdown_chunking import (
    HARD_MAX_TOKENS,
    MarkdownPage,
    chunk_markdown,
)
from pdf_bridge.services.markdown_formatter import (
    FormatterConfig,
    LayoutPage,
    format_markdown_document,
)

CORPUS_ROOT = Path(__file__).parent / "fixtures" / "evaluation"
MANIFEST_PATH = CORPUS_ROOT / "manifest.json"
ACCEPT_FILENAMES = (
    "operations-guide.pdf",
    "inventory-tables.pdf",
    "page-boundary-procedure.pdf",
    "employee-onboarding-handbook-v1.pdf",
    "employee-onboarding-handbook-v2.pdf",
)
REJECTION_REASONS = {
    "encrypted-notice.pdf": "encrypted",
    "image-only-diagram.pdf": "image-only",
    "malformed-truncated.pdf": "malformed",
}
HEADING_LEVELS = {
    "Operations Guide": 1,
    "Installation": 2,
    "Verification": 2,
    "Regional Inventory Table": 1,
    "Regional Inventory Table (continued)": 2,
    "Recovery Procedure": 1,
    "Checkpoint": 2,
    "Resume": 2,
    "Employee Onboarding Handbook": 1,
    "Revision 1": 2,
    "Revision 2": 2,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TABLE_SEPARATOR = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|){2,}$")


def _manifest() -> dict[str, Any]:
    return jsonlib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _entries_by_name() -> dict[str, dict[str, Any]]:
    return {entry["filename"]: entry for entry in _manifest()["documents"]}


def _extraction_limits() -> ExtractionLimits:
    return ExtractionLimits(
        max_pages=20,
        max_characters=100_000,
        cpu_seconds=10,
        memory_bytes=512 * 1024 * 1024,
        wall_clock_seconds=20,
    )


@pytest.fixture(scope="module")
def english_detector() -> LinguaEnglishDetector:
    return LinguaEnglishDetector()


@dataclass(frozen=True, slots=True)
class _Response:
    payload: object
    status_code: int = 200

    def json(self) -> object:
        return self.payload


def _source_pages(request: dict[str, Any]) -> list[dict[str, Any]]:
    content = request["messages"][-1]["content"]
    return jsonlib.loads(content.split("\n", 1)[1])["pages"]


def _split_layout_row(line: str) -> list[str]:
    return re.split(r"\s{2,}", line.strip())


def _format_source_slice(source_text: str) -> str:
    """Deterministic vLLM stand-in; production validation still judges its output."""

    lines = source_text.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        heading_level = HEADING_LEVELS.get(line)
        if heading_level is not None:
            blocks.append(f"{'#' * heading_level} {line}")
            continue
        cells = _split_layout_row(line)
        if cells == ["Region", "Item", "SKU", "Owner", "Window", "Status"]:
            table_lines = [
                "| " + " | ".join(cells) + " |",
                "| " + " | ".join("---" for _ in cells) + " |",
            ]
            while index < len(lines):
                candidate = lines[index].strip()
                if not candidate:
                    index += 1
                    continue
                row = _split_layout_row(candidate)
                if len(row) != len(cells):
                    break
                table_lines.append("| " + " | ".join(row) + " |")
                index += 1
            blocks.append("\n".join(table_lines))
            continue
        blocks.append(line)
    return "\n\n".join(blocks)


class _OfflineFormatterClient:
    """Emulate only the pinned private vLLM protocol, never its validation logic."""

    def __init__(self) -> None:
        self.chat_calls = 0

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> _Response:
        assert url == "http://offline-formatter/tokenizer_info"
        assert headers == {"Accept": "application/json"}
        assert timeout == 10
        return _Response(
            {"tokenizer_class": "CorpusTokenizer", "model": "corpus-formatter"}
        )

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> _Response:
        assert headers == {"Accept": "application/json"}
        assert timeout == 10
        if url == "http://offline-formatter/tokenize":
            token_source = json.get("prompt")
            if token_source is None:
                token_source = "\n".join(message["content"] for message in json["messages"])
            tokens = list(range(len(token_source)))
            return _Response(
                {
                    "count": len(tokens),
                    "max_model_len": 8192,
                    "model": "corpus-formatter",
                    "tokens": tokens,
                }
            )

        assert url == "http://offline-formatter/v1/chat/completions"
        self.chat_calls += 1
        pages = _source_pages(json)
        output = {
            "pages": [
                {
                    "page_number": page["page_number"],
                    "slices": [
                        {
                            "slice_index": source_slice["slice_index"],
                            "source_text_sha256": source_slice["source_text_sha256"],
                            "markdown": _format_source_slice(source_slice["source_text"]),
                        }
                        for source_slice in page["slices"]
                    ],
                }
                for page in pages
            ]
        }
        return _Response(
            {
                "model": "corpus-formatter",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": jsonlib.dumps(output, ensure_ascii=False),
                        },
                    }
                ],
            }
        )


class _OfflineChunkTokenizer:
    """Deterministic exact-count boundary used in place of deployment model assets."""

    def count_tokens(self, text: str) -> int:
        return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def _formatter_config() -> FormatterConfig:
    return FormatterConfig(
        api_url="http://offline-formatter",
        model_id="corpus-formatter",
        expected_tokenizer_class="CorpusTokenizer",
        timeout_seconds=10,
        max_input_tokens=6_000,
        max_output_tokens=1_000,
        token_safety_reserve=128,
        max_pages_per_request=8,
        max_attempts=1,
    )


def _markdown_headings(markdown: str) -> list[str]:
    return [
        match.group(1).strip()
        for line in markdown.splitlines()
        if (match := re.match(r"^#{1,6}\s+(.+)$", line)) is not None
    ]


def _pipe_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _markdown_tables(markdown: str) -> list[dict[str, list[list[str]] | list[str]]]:
    lines = markdown.splitlines()
    tables: list[dict[str, list[list[str]] | list[str]]] = []
    for index, line in enumerate(lines):
        if not _TABLE_SEPARATOR.fullmatch(line.strip()) or index == 0:
            continue
        rows: list[list[str]] = []
        row_index = index + 1
        while row_index < len(lines) and lines[row_index].strip().startswith("|"):
            rows.append(_pipe_cells(lines[row_index]))
            row_index += 1
        tables.append({"columns": _pipe_cells(lines[index - 1]), "rows": rows})
    return tables


def test_manifest_pins_a_complete_original_offline_corpus() -> None:
    manifest = _manifest()
    assert set(manifest) == {
        "schema_version",
        "corpus_id",
        "license",
        "provenance",
        "generator",
        "filename_families",
        "documents",
    }
    assert manifest["schema_version"] == 1
    assert manifest["corpus_id"] == "pdf-bridge-evaluation-v1"
    assert manifest["license"] == "CC0-1.0"
    assert "no external content" in manifest["provenance"]

    entries = _entries_by_name()
    expected_names = set(ACCEPT_FILENAMES) | set(REJECTION_REASONS)
    assert set(entries) == expected_names
    assert {path.name for path in CORPUS_ROOT.glob("*.pdf")} == expected_names
    assert {
        name for name, entry in entries.items() if entry["expected"]["outcome"] == "accept"
    } == set(ACCEPT_FILENAMES)
    assert {
        name for name, entry in entries.items() if entry["expected"]["outcome"] == "reject"
    } == set(REJECTION_REASONS)

    for filename, entry in entries.items():
        assert set(entry) == {
            "filename",
            "sha256",
            "size_bytes",
            "license",
            "provenance",
            "expected",
        }
        assert filename == Path(filename).name and filename.endswith(".pdf")
        assert entry["license"] == "CC0-1.0"
        assert entry["provenance"].startswith("Original")
        assert _SHA256.fullmatch(entry["sha256"])
        fixture = CORPUS_ROOT / filename
        content = fixture.read_bytes()
        assert len(content) == entry["size_bytes"]
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]


@pytest.mark.parametrize("filename", ACCEPT_FILENAMES)
def test_native_corpus_runs_through_extraction_formatting_and_chunking(
    filename: str,
    english_detector: LinguaEnglishDetector,
) -> None:
    expected = _entries_by_name()[filename]["expected"]
    extracted = extract_pdf_layout(CORPUS_ROOT / filename, _extraction_limits())

    assert extracted.page_count == expected["page_count"]
    assert [page.page_number for page in extracted.pages] == list(
        range(1, expected["page_count"] + 1)
    )
    assert [page.text_sha256 for page in extracted.pages] == expected["page_text_sha256"]
    for page, required_fragments in zip(
        extracted.pages, expected["page_contains"], strict=True
    ):
        assert all(fragment in page.layout_text for fragment in required_fragments)
    validate_native_english(extracted, english_detector)

    formatter_client = _OfflineFormatterClient()
    formatted = format_markdown_document(
        [LayoutPage(page.page_number, page.layout_text) for page in extracted.pages],
        _formatter_config(),
        client=formatter_client,
    )
    assert formatter_client.chat_calls == 1
    assert [len(page.slices) for page in formatted.pages] == expected[
        "formatter_slice_counts"
    ]
    assert [page.source_text_sha256 for page in formatted.pages] == expected[
        "page_text_sha256"
    ]
    for page, expected_headings in zip(
        formatted.pages, expected["headings_by_page"], strict=True
    ):
        assert _markdown_headings(page.markdown) == expected_headings
        expected_tables = [
            {"columns": table["columns"], "rows": table["rows"]}
            for table in expected["tables"]
            if table["page"] == page.page_number
        ]
        assert _markdown_tables(page.markdown) == expected_tables

    document_id = uuid.uuid5(uuid.NAMESPACE_URL, f"corpus-document:{filename}")
    revision_id = uuid.uuid5(uuid.NAMESPACE_URL, f"corpus-revision:{filename}")
    chunks = chunk_markdown(
        [MarkdownPage(page.page_number, page.markdown) for page in formatted.pages],
        document_id=document_id,
        prepared_revision_id=revision_id,
        tokenizer=_OfflineChunkTokenizer(),
    )
    assert chunks
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert all(0 < chunk.token_count <= HARD_MAX_TOKENS for chunk in chunks)
    covered_pages = sorted(
        {
            page_number
            for chunk in chunks
            for page_number in range(chunk.page_start, chunk.page_end + 1)
        }
    )
    assert covered_pages == expected["chunk_page_coverage"]
    assert any(chunk.page_start < chunk.page_end for chunk in chunks) is expected[
        "spans_page_boundary"
    ]


@pytest.mark.parametrize(("filename", "reason"), REJECTION_REASONS.items())
def test_rejection_corpus_fails_at_the_expected_content_gate(
    filename: str,
    reason: str,
    english_detector: LinguaEnglishDetector,
) -> None:
    expected = _entries_by_name()[filename]["expected"]
    assert expected["reason"] == reason

    if reason in {"encrypted", "malformed"}:
        with pytest.raises(ExtractionRejectedError) as raised:
            extract_pdf_layout(CORPUS_ROOT / filename, _extraction_limits())
        assert raised.value.reason == reason
        return

    extracted = extract_pdf_layout(CORPUS_ROOT / filename, _extraction_limits())
    assert extracted.page_count == expected["page_count"]
    assert [page.text_sha256 for page in extracted.pages] == expected["page_text_sha256"]
    with pytest.raises(ExtractionRejectedError) as raised:
        validate_native_english(extracted, english_detector)
    assert raised.value.reason == reason


def test_versioned_handbooks_are_a_real_distinct_filename_family() -> None:
    family = _manifest()["filename_families"][0]
    first_name, second_name = family["members"]
    first_profile = profile_filename(first_name)
    second_profile = profile_filename(second_name)

    assert list(first_profile.family_key) == family["expected_family_key"]
    assert second_profile.family_key == first_profile.family_key
    match = compare_filenames(first_profile, second_profile)
    assert match is not None and match.kind == "filename-family"

    first = extract_pdf_layout(CORPUS_ROOT / first_name, _extraction_limits())
    second = extract_pdf_layout(CORPUS_ROOT / second_name, _extraction_limits())
    assert first.pages[0].text_sha256 != second.pages[0].text_sha256
    assert (CORPUS_ROOT / first_name).read_bytes() != (CORPUS_ROOT / second_name).read_bytes()
