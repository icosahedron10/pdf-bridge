"""Deterministic structure-aware chunking for canonical Markdown.

The formatter owns presentation; this module only groups validated Markdown into
MPNet-sized chunks while retaining page and heading provenance.  Token counts are
delegated to the exact pinned MPNet tokenizer so the 384-wordpiece limit is an
enforced boundary rather than an estimate.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Protocol

TARGET_TOKENS = 320
OVERLAP_TOKENS = 48
HARD_MAX_TOKENS = 384
CHUNKER_PROFILE = "markdown-structure/v1:target=320:overlap=48:max=384"

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})([^`]*)$")
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_BREAK = re.compile(r"(?:\n+|(?<=[.!?])\s+|\s+)")


class MarkdownChunkingError(RuntimeError):
    """Canonical Markdown cannot be represented within the hard token bound."""


class Tokenizer(Protocol):
    """The small tokenizer boundary needed by the deterministic chunker."""

    def count_tokens(self, text: str) -> int:
        """Return the exact number of model wordpieces in ``text``."""


@dataclass(frozen=True, slots=True)
class MarkdownPage:
    """Validated Markdown for one one-based PDF page."""

    page_number: int
    markdown: str


@dataclass(frozen=True, slots=True)
class MarkdownChunk:
    """One immutable Markdown chunk and its source provenance."""

    chunk_id: uuid.UUID
    chunk_index: int
    page_start: int
    page_end: int
    heading_path: tuple[str, ...]
    token_count: int
    text_sha256: str
    markdown: str


@dataclass(frozen=True, slots=True)
class _Block:
    text: str
    page: int
    heading_path: tuple[str, ...]
    kind: str
    token_count: int


def canonical_markdown(pages: list[MarkdownPage]) -> str:
    """Assemble the stable document view with explicit page boundaries."""

    _validate_pages(pages)
    return "\n\n".join(
        f"<!-- page:{page.page_number} -->\n\n{page.markdown.strip()}" for page in pages
    )


def _validate_pages(pages: list[MarkdownPage]) -> None:
    if not pages:
        raise MarkdownChunkingError("at least one formatted page is required")
    expected = 1
    for page in pages:
        if page.page_number != expected:
            raise MarkdownChunkingError("formatted pages must be complete and in one-based order")
        if not page.markdown.strip():
            raise MarkdownChunkingError(f"page {page.page_number} has empty Markdown")
        expected += 1


def _is_table_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    cells = [cell.strip() for cell in stripped.split("|")]
    return len(cells) >= 2 and all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in cells)


def _is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and _is_table_separator(lines[index + 1])
    )


def _parse_blocks(
    page: MarkdownPage,
    tokenizer: Tokenizer,
    inherited_headings: tuple[str, ...],
) -> tuple[list[_Block], tuple[str, ...]]:
    lines = page.markdown.strip().splitlines()
    headings = list(inherited_headings)
    blocks: list[_Block] = []
    index = 0

    def append(text: str, kind: str) -> None:
        candidate = text.strip()
        if candidate:
            blocks.append(
                _Block(
                    text=candidate,
                    page=page.page_number,
                    heading_path=tuple(headings),
                    kind=kind,
                    token_count=tokenizer.count_tokens(candidate),
                )
            )

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            headings[level - 1 :] = [title]
            append(line, "heading")
            index += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            captured = [line]
            index += 1
            while index < len(lines):
                captured.append(lines[index])
                closing = lines[index].lstrip()
                index += 1
                if closing.startswith(marker[0] * len(marker)):
                    break
            append("\n".join(captured), "fence")
            continue

        if _is_table_start(lines, index):
            captured = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                captured.append(lines[index])
                index += 1
            append("\n".join(captured), "table")
            continue

        captured = [line]
        index += 1
        while index < len(lines):
            if not lines[index].strip():
                break
            if _HEADING.match(lines[index]) or _FENCE.match(lines[index]):
                break
            if _is_table_start(lines, index):
                break
            captured.append(lines[index])
            index += 1
        append("\n".join(captured), "prose")

    return blocks, tuple(headings)


def _break_positions(text: str) -> list[int]:
    positions = {match.end() for match in _BREAK.finditer(text)}
    positions.update(range(1, len(text) + 1))
    return sorted(positions)


def _largest_prefix(text: str, max_tokens: int, tokenizer: Tokenizer) -> tuple[str, str]:
    """Split at the farthest textual boundary whose exact count fits."""

    positions = _break_positions(text)
    low = 0
    high = len(positions) - 1
    best = 0
    while low <= high:
        middle = (low + high) // 2
        end = positions[middle]
        candidate = text[:end].rstrip()
        if candidate and tokenizer.count_tokens(candidate) <= max_tokens:
            best = end
            low = middle + 1
        else:
            high = middle - 1
    if best <= 0:
        raise MarkdownChunkingError("the tokenizer cannot fit one source character")
    return text[:best].rstrip(), text[best:].lstrip()


def _largest_suffix(text: str, max_tokens: int, tokenizer: Tokenizer) -> str:
    """Return the longest exact-token suffix, preferring textual boundaries."""

    starts = [0, *[match.end() for match in _BREAK.finditer(text)]]
    best = ""
    for start in reversed(starts):
        candidate = text[start:].lstrip()
        if candidate and tokenizer.count_tokens(candidate) <= max_tokens:
            best = candidate
            continue
        if best:
            break
    if best:
        return best
    # One model wordpiece can span punctuation in ways that defeat our normal
    # boundaries. A character scan remains deterministic and exact.
    for start in range(len(text) - 1, -1, -1):
        candidate = text[start:].lstrip()
        if candidate and tokenizer.count_tokens(candidate) <= max_tokens:
            best = candidate
        elif best:
            break
    return best


def _split_prose(block: _Block, tokenizer: Tokenizer) -> list[_Block]:
    pieces: list[_Block] = []
    remaining = block.text
    while remaining:
        piece, remaining = _largest_prefix(remaining, HARD_MAX_TOKENS, tokenizer)
        pieces.append(
            _Block(
                text=piece,
                page=block.page,
                heading_path=block.heading_path,
                kind="prose",
                token_count=tokenizer.count_tokens(piece),
            )
        )
    return pieces


def _split_table(block: _Block, tokenizer: Tokenizer) -> list[_Block]:
    lines = block.text.splitlines()
    if len(lines) < 2 or not _is_table_separator(lines[1]):
        raise MarkdownChunkingError("formatter returned a malformed Markdown table")
    header = "\n".join(lines[:2])
    if tokenizer.count_tokens(header) > HARD_MAX_TOKENS:
        raise MarkdownChunkingError("a Markdown table header exceeds 384 MPNet wordpieces")
    if len(lines) == 2:
        return [block]

    pieces: list[_Block] = []
    rows: list[str] = []
    for row in lines[2:]:
        candidate = "\n".join([header, *rows, row])
        if tokenizer.count_tokens(candidate) <= HARD_MAX_TOKENS:
            rows.append(row)
            continue
        if not rows:
            raise MarkdownChunkingError("a Markdown table row exceeds 384 MPNet wordpieces")
        text = "\n".join([header, *rows])
        pieces.append(
            _Block(
                text=text,
                page=block.page,
                heading_path=block.heading_path,
                kind="table",
                token_count=tokenizer.count_tokens(text),
            )
        )
        rows = [row]
        if tokenizer.count_tokens("\n".join([header, row])) > HARD_MAX_TOKENS:
            raise MarkdownChunkingError("a Markdown table row exceeds 384 MPNet wordpieces")
    text = "\n".join([header, *rows])
    pieces.append(
        _Block(
            text=text,
            page=block.page,
            heading_path=block.heading_path,
            kind="table",
            token_count=tokenizer.count_tokens(text),
        )
    )
    return pieces


def _split_fence(block: _Block, tokenizer: Tokenizer) -> list[_Block]:
    lines = block.text.splitlines()
    if len(lines) < 2:
        raise MarkdownChunkingError("formatter returned an unterminated Markdown fence")
    opening = lines[0]
    closing = lines[-1]
    body = "\n".join(lines[1:-1])
    wrapper_tokens = tokenizer.count_tokens(f"{opening}\n\n{closing}")
    budget = HARD_MAX_TOKENS - wrapper_tokens
    if budget <= 0:
        raise MarkdownChunkingError("a Markdown fence wrapper exceeds the hard token limit")
    pieces: list[_Block] = []
    remaining = body
    while remaining:
        piece, remaining = _largest_prefix(remaining, budget, tokenizer)
        text = f"{opening}\n{piece}\n{closing}"
        count = tokenizer.count_tokens(text)
        if count > HARD_MAX_TOKENS:
            # Tokenizers are not generally additive around boundaries. Tighten
            # deterministically until the complete fenced value fits.
            piece, restored = _largest_prefix(piece, max(budget - 1, 1), tokenizer)
            remaining = "\n".join(part for part in (restored, remaining) if part)
            text = f"{opening}\n{piece}\n{closing}"
            count = tokenizer.count_tokens(text)
        if count > HARD_MAX_TOKENS:
            raise MarkdownChunkingError("a fenced block cannot be split below 384 wordpieces")
        pieces.append(
            _Block(
                text=text,
                page=block.page,
                heading_path=block.heading_path,
                kind="fence",
                token_count=count,
            )
        )
    return pieces


def _bounded_blocks(pages: list[MarkdownPage], tokenizer: Tokenizer) -> list[_Block]:
    bounded: list[_Block] = []
    headings: tuple[str, ...] = ()
    for page in pages:
        page_blocks, headings = _parse_blocks(page, tokenizer, headings)
        for block in page_blocks:
            if block.token_count <= HARD_MAX_TOKENS:
                bounded.append(block)
            elif block.kind == "table":
                bounded.extend(_split_table(block, tokenizer))
            elif block.kind == "fence":
                bounded.extend(_split_fence(block, tokenizer))
            else:
                bounded.extend(_split_prose(block, tokenizer))
    return bounded


def _overlap(parts: list[_Block], tokenizer: Tokenizer) -> list[_Block]:
    prose = next((part for part in reversed(parts) if part.kind == "prose"), None)
    if prose is None:
        return []
    if prose.token_count <= OVERLAP_TOKENS:
        text = prose.text
    else:
        text = _largest_suffix(prose.text, OVERLAP_TOKENS, tokenizer)
    if not text:
        return []
    return [
        _Block(
            text=text,
            page=prose.page,
            heading_path=prose.heading_path,
            kind="overlap",
            token_count=tokenizer.count_tokens(text),
        )
    ]


def _finish_chunk(
    document_id: uuid.UUID,
    prepared_revision_id: uuid.UUID,
    index: int,
    parts: list[_Block],
    tokenizer: Tokenizer,
) -> MarkdownChunk:
    markdown = "\n\n".join(part.text for part in parts).strip()
    if not markdown:
        raise MarkdownChunkingError("empty chunks are forbidden")
    count = tokenizer.count_tokens(markdown)
    if count > HARD_MAX_TOKENS:
        raise MarkdownChunkingError(
            f"chunk {index} exceeds the 384-wordpiece hard maximum ({count})"
        )
    text_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    chunk_id = uuid.uuid5(
        document_id,
        f"{prepared_revision_id}:{index}:{text_hash}",
    )
    return MarkdownChunk(
        chunk_id=chunk_id,
        chunk_index=index,
        page_start=min(part.page for part in parts),
        page_end=max(part.page for part in parts),
        heading_path=parts[-1].heading_path,
        token_count=count,
        text_sha256=text_hash,
        markdown=markdown,
    )


def chunk_markdown(
    pages: list[MarkdownPage],
    *,
    document_id: uuid.UUID,
    prepared_revision_id: uuid.UUID,
    tokenizer: Tokenizer,
    max_chunks: int = 10_000,
) -> list[MarkdownChunk]:
    """Create stable, provenance-rich chunks under exact MPNet token limits."""

    _validate_pages(pages)
    if max_chunks <= 0:
        raise ValueError("max_chunks must be positive")
    blocks = _bounded_blocks(pages, tokenizer)
    if not blocks:
        raise MarkdownChunkingError("formatted Markdown contains no indexable content")

    chunks: list[MarkdownChunk] = []
    parts: list[_Block] = []

    def total(candidate: list[_Block]) -> int:
        return tokenizer.count_tokens("\n\n".join(part.text for part in candidate))

    def flush() -> None:
        nonlocal parts
        if not parts:
            return
        chunks.append(
            _finish_chunk(document_id, prepared_revision_id, len(chunks), parts, tokenizer)
        )
        if len(chunks) > max_chunks:
            raise MarkdownChunkingError(f"document exceeds the {max_chunks}-chunk limit")
        parts = _overlap(parts, tokenizer)

    for block in blocks:
        if parts and total([*parts, block]) > TARGET_TOKENS:
            flush()
        if parts and total([*parts, block]) > HARD_MAX_TOKENS:
            parts = []
        parts.append(block)
        if total(parts) > HARD_MAX_TOKENS:
            raise MarkdownChunkingError("a bounded block exceeded the hard chunk limit")
    flush()

    # ``flush`` prepares overlap for a hypothetical next chunk; it must never
    # create an overlap-only trailing chunk.
    return chunks
