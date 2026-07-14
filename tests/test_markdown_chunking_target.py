from __future__ import annotations

import re
import uuid

import pytest

from pdf_bridge.services.markdown_chunking import (
    HARD_MAX_TOKENS,
    MarkdownChunkingError,
    MarkdownPage,
    canonical_markdown,
    chunk_markdown,
)


class WordpieceStub:
    """Deterministic tokenizer stand-in that counts words and Markdown punctuation."""

    def count_tokens(self, text: str) -> int:
        return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def test_canonical_markdown_retains_explicit_page_boundaries() -> None:
    pages = [
        MarkdownPage(page_number=1, markdown="# Install\n\nStart here."),
        MarkdownPage(page_number=2, markdown="## Windows\n\nRun setup."),
    ]

    assert canonical_markdown(pages) == (
        "<!-- page:1 -->\n\n# Install\n\nStart here.\n\n"
        "<!-- page:2 -->\n\n## Windows\n\nRun setup."
    )


def test_chunks_are_stable_bounded_and_retain_heading_and_page_provenance() -> None:
    tokenizer = WordpieceStub()
    document_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    revision_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    pages = [
        MarkdownPage(
            page_number=1,
            markdown="# Guide\n\n## Setup\n\n" + "Install the package safely. " * 90,
        ),
        MarkdownPage(
            page_number=2,
            markdown="## Verify\n\n" + "Confirm the checksum exactly. " * 90,
        ),
    ]

    first = chunk_markdown(
        pages,
        document_id=document_id,
        prepared_revision_id=revision_id,
        tokenizer=tokenizer,
    )
    second = chunk_markdown(
        pages,
        document_id=document_id,
        prepared_revision_id=revision_id,
        tokenizer=tokenizer,
    )

    assert first == second
    assert len(first) >= 2
    assert [chunk.chunk_index for chunk in first] == list(range(len(first)))
    assert all(0 < chunk.token_count <= HARD_MAX_TOKENS for chunk in first)
    assert all(chunk.page_start <= chunk.page_end for chunk in first)
    assert first[0].heading_path == ("Guide", "Setup")
    assert first[-1].heading_path == ("Guide", "Verify")


def test_oversized_table_repeats_header_without_overlapping_whole_table() -> None:
    tokenizer = WordpieceStub()
    rows = "\n".join(f"| Item {index} | {'value ' * 12}|" for index in range(80))
    pages = [
        MarkdownPage(
            page_number=1,
            markdown=f"# Inventory\n\n| Item | Detail |\n|---|---|\n{rows}",
        )
    ]

    chunks = chunk_markdown(
        pages,
        document_id=uuid.uuid4(),
        prepared_revision_id=uuid.uuid4(),
        tokenizer=tokenizer,
    )

    table_chunks = [chunk for chunk in chunks if "| Item | Detail |" in chunk.markdown]
    assert len(table_chunks) >= 2
    assert all("|---|---|" in chunk.markdown for chunk in table_chunks)
    assert all(chunk.token_count <= HARD_MAX_TOKENS for chunk in table_chunks)


def test_oversized_single_table_row_fails_hard() -> None:
    pages = [
        MarkdownPage(
            page_number=1,
            markdown="| Item | Detail |\n|---|---|\n| A | " + "word " * 500 + "|",
        )
    ]

    with pytest.raises(MarkdownChunkingError, match="table row"):
        chunk_markdown(
            pages,
            document_id=uuid.uuid4(),
            prepared_revision_id=uuid.uuid4(),
            tokenizer=WordpieceStub(),
        )


def test_page_sequence_and_empty_markdown_fail_closed() -> None:
    with pytest.raises(MarkdownChunkingError, match="one-based order"):
        canonical_markdown([MarkdownPage(page_number=2, markdown="content")])
    with pytest.raises(MarkdownChunkingError, match="empty Markdown"):
        canonical_markdown([MarkdownPage(page_number=1, markdown="  ")])
