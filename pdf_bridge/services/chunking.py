"""Deterministic normalization, tokenization, and chunking of extracted text.

Every function here is pure and deterministic: identical page text always
produces identical chunk boundaries, hashes, and token counts. The constants
participate in the versioned pipeline fingerprint, so changing any of them
must be treated as a new analysis pipeline version.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

CHUNKING_VERSION = "chunker/v2"
TARGET_CHUNK_TOKENS = 400
OVERLAP_TOKENS = 60
CHUNK_HARD_CAP_CHARS = 3_500

MAX_PAGES = 2_000
MAX_NORMALIZED_CHARS = 5_000_000
MAX_CHUNKS = 10_000

MIN_DOCUMENT_ALNUM_CHARS = 50
MIN_SUBSTANTIVE_DISTINCT_TOKENS = 12
MIN_SUBSTANTIVE_ALNUM_CHARS = 40

_LEXICAL_TOKEN = re.compile(r"\w+", re.UNICODE)
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")
_INLINE_WHITESPACE = re.compile(r"[^\S\n]+")


class TextBudgetExceededError(RuntimeError):
    """Raised when extracted text exceeds a configured safety cap.

    The caps exist so hostile or degenerate PDFs cannot silently produce
    truncated analyses; exceeding them is an explicit, terminal rejection.
    """

    def __init__(self, limit_name: str, limit: int, observed: int) -> None:
        super().__init__(f"document exceeds the {limit_name} safety cap ({observed} > {limit})")
        self.limit_name = limit_name
        self.limit = limit
        self.observed = observed


class InsufficientTextError(RuntimeError):
    """Raised when a PDF has too little usable text to analyze or index."""


@dataclass(frozen=True, slots=True)
class PageText:
    """Extracted text for one PDF page (1-indexed)."""

    number: int
    text: str


@dataclass(frozen=True, slots=True)
class Chunk:
    """One deterministic, page-mapped chunk of normalized document text."""

    index: int
    text: str
    page_start: int
    page_end: int
    token_count: int
    text_hash: str


@dataclass(frozen=True, slots=True)
class _Segment:
    """A sentence-or-smaller unit that never crosses a page boundary."""

    text: str
    page: int
    tokens: int


def normalize_text(text: str) -> str:
    """Normalize text deterministically: NFKC, collapsed spaces, kept newlines."""

    candidate = unicodedata.normalize("NFKC", text)
    candidate = candidate.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    candidate = "".join(
        character
        for character in candidate
        if character == "\n" or unicodedata.category(character) != "Cc"
    )
    candidate = _INLINE_WHITESPACE.sub(" ", candidate)
    lines = [line.strip() for line in candidate.split("\n")]
    return "\n".join(lines).strip()


def lexical_tokens(text: str) -> list[str]:
    """Return casefolded Unicode word tokens used for chunk sizing."""

    return [match.group(0).casefold() for match in _LEXICAL_TOKEN.finditer(text)]


def count_alphanumeric(text: str) -> int:
    """Count Unicode alphanumeric characters, the basis of the text gate."""

    return sum(1 for character in text if character.isalnum())


def sha256_text(text: str) -> str:
    """Hash text with SHA-256 over UTF-8 bytes."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_text_hash(pages: list[PageText]) -> str:
    """Hash the canonical normalized document text for exact-text matching.

    Page boundaries and whitespace reflow are layout details, so each page is
    normalized and flattened before non-empty page text is joined by one
    space. Meaningful characters such as punctuation remain part of the hash.
    """

    flattened_pages = [" ".join(normalize_text(page.text).split()) for page in pages]
    canonical = " ".join(text for text in flattened_pages if text)
    return sha256_text(canonical)


def is_substantive(text: str) -> bool:
    """Return whether chunk text is substantive rather than boilerplate.

    Boilerplate here means content-free filler: repeated headers, page
    numbers, separator runs. The heuristic is deliberately simple and
    deterministic — enough distinct word tokens and alphanumeric characters.
    """

    tokens = lexical_tokens(text)
    return (
        len(set(tokens)) >= MIN_SUBSTANTIVE_DISTINCT_TOKENS
        and count_alphanumeric(text) >= MIN_SUBSTANTIVE_ALNUM_CHARS
    )


def _split_oversized(text: str, page: int) -> list[_Segment]:
    """Split a single overlong sentence into hard-capped token-boundary pieces."""

    segments: list[_Segment] = []
    remaining = text
    while remaining:
        if len(remaining) <= CHUNK_HARD_CAP_CHARS:
            piece, remaining = remaining, ""
        else:
            window = remaining[: CHUNK_HARD_CAP_CHARS + 1]
            split_at = window.rfind(" ")
            if split_at <= 0:
                split_at = CHUNK_HARD_CAP_CHARS
            piece, remaining = remaining[:split_at].rstrip(), remaining[split_at:].lstrip()
        if piece:
            segments.append(_Segment(text=piece, page=page, tokens=len(lexical_tokens(piece))))
    return segments


def _page_segments(page: PageText) -> list[_Segment]:
    """Split one page into paragraph- and sentence-aware segments."""

    segments: list[_Segment] = []
    normalized = normalize_text(page.text)
    for paragraph in _PARAGRAPH_BREAK.split(normalized):
        flattened = " ".join(paragraph.split())
        if not flattened:
            continue
        for sentence in _SENTENCE_BOUNDARY.split(flattened):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > CHUNK_HARD_CAP_CHARS:
                segments.extend(_split_oversized(sentence, page.number))
            else:
                segments.append(
                    _Segment(
                        text=sentence,
                        page=page.number,
                        tokens=len(lexical_tokens(sentence)),
                    )
                )
    return segments


def _finish_chunk(parts: list[_Segment], index: int) -> Chunk:
    text = "\n".join(part.text for part in parts)
    return Chunk(
        index=index,
        text=text,
        page_start=min(part.page for part in parts),
        page_end=max(part.page for part in parts),
        token_count=sum(part.tokens for part in parts),
        text_hash=sha256_text(text),
    )


def _overlap_tail(parts: list[_Segment]) -> list[_Segment]:
    """Return the trailing segments carried into the next chunk as overlap."""

    tail: list[_Segment] = []
    tokens = 0
    for part in reversed(parts):
        if tail and tokens + part.tokens > OVERLAP_TOKENS:
            break
        tail.insert(0, part)
        tokens += part.tokens
        if tokens >= OVERLAP_TOKENS:
            break
    # Overlap must never constitute the whole next chunk's budget, and a chunk
    # made only of overlap would repeat forever.
    if len(tail) == len(parts):
        tail = tail[1:]
    return tail


def chunk_pages(
    pages: list[PageText],
    *,
    max_pages: int = MAX_PAGES,
    max_chars: int = MAX_NORMALIZED_CHARS,
    max_chunks: int = MAX_CHUNKS,
) -> list[Chunk]:
    """Chunk page-mapped text deterministically under the configured budgets.

    Raises ``TextBudgetExceededError`` rather than silently truncating, and
    ``InsufficientTextError`` when the document fails the minimum text gate.
    """

    if len(pages) > max_pages:
        raise TextBudgetExceededError("page-count", max_pages, len(pages))

    normalized_chars = sum(len(normalize_text(page.text)) for page in pages)
    if normalized_chars > max_chars:
        raise TextBudgetExceededError("normalized-character-count", max_chars, normalized_chars)

    segments: list[_Segment] = []
    for page in pages:
        segments.extend(_page_segments(page))

    total_alnum = sum(count_alphanumeric(segment.text) for segment in segments)
    if total_alnum < MIN_DOCUMENT_ALNUM_CHARS:
        raise InsufficientTextError(
            "the document does not contain the minimum "
            f"{MIN_DOCUMENT_ALNUM_CHARS} alphanumeric characters of extractable text"
        )

    chunks: list[Chunk] = []
    parts: list[_Segment] = []
    part_tokens = 0
    part_chars = 0

    def flush() -> None:
        nonlocal parts, part_tokens, part_chars
        if not parts:
            return
        chunks.append(_finish_chunk(parts, len(chunks)))
        if len(chunks) > max_chunks:
            raise TextBudgetExceededError("chunk-count", max_chunks, len(chunks))
        tail = _overlap_tail(parts)
        parts = list(tail)
        part_tokens = sum(part.tokens for part in parts)
        part_chars = sum(len(part.text) for part in parts) + max(len(parts) - 1, 0)

    for segment in segments:
        candidate_chars = part_chars + (1 if parts else 0) + len(segment.text)
        if parts and (
            part_tokens + segment.tokens > TARGET_CHUNK_TOKENS
            or candidate_chars > CHUNK_HARD_CAP_CHARS
        ):
            flush()
            candidate_chars = part_chars + (1 if parts else 0) + len(segment.text)
            # The carried overlap plus an oversized segment can still overflow
            # the hard cap; drop the overlap rather than the segment.
            if parts and candidate_chars > CHUNK_HARD_CAP_CHARS:
                parts = []
                part_tokens = 0
                part_chars = 0
        parts.append(segment)
        part_tokens += segment.tokens
        part_chars = sum(len(part.text) for part in parts) + max(len(parts) - 1, 0)

    if parts:
        chunks.append(_finish_chunk(parts, len(chunks)))
        if len(chunks) > max_chunks:
            raise TextBudgetExceededError("chunk-count", max_chunks, len(chunks))

    if not any(is_substantive(chunk.text) for chunk in chunks):
        raise InsufficientTextError(
            "the document does not contain at least one substantive, non-boilerplate chunk of text"
        )
    return chunks
