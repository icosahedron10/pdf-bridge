"""Collection-scoped filename-family analysis for advisory upload warnings.

Filenames are advisory evidence only: a family match never blocks an upload.
The analysis is deterministic and versioned; its thresholds participate in
the pipeline fingerprint recorded with every analysis.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

FILENAME_ANALYSIS_VERSION = "filenames/v1"
TOKEN_SET_SIMILARITY_THRESHOLD = 0.88
JARO_WINKLER_THRESHOLD = 0.90
MIN_FAMILY_SUBSTANTIVE_TOKENS = 2

_MONTH_TOKENS = {
    "jan",
    "january",
    "feb",
    "february",
    "mar",
    "march",
    "apr",
    "april",
    "may",
    "jun",
    "june",
    "jul",
    "july",
    "aug",
    "august",
    "sep",
    "sept",
    "september",
    "oct",
    "october",
    "nov",
    "november",
    "dec",
    "december",
}
_QUARTER_TOKENS = {"q1", "q2", "q3", "q4", "h1", "h2", "quarter", "quarterly"}
_VERSION_WORD_TOKENS = {"v", "ver", "version", "rev", "revision", "draft", "final", "copy"}
_STOP_TOKENS = {"a", "an", "and", "de", "der", "die", "el", "la", "le", "of", "or", "the", "to"}

_VERSION_PATTERN = re.compile(r"^(?:v|ver|rev|r)?\d+(?:[._-]\d+)*[a-z]?$")
_NUMERIC_PATTERN = re.compile(r"^\d+$")
_DATEISH_PATTERN = re.compile(r"^\d{1,4}(?:[._-]\d{1,2}){1,2}$")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)

MatchKind = Literal["filename-family", "token-set-similarity", "jaro-winkler-similarity"]


@dataclass(frozen=True, slots=True)
class FilenameProfile:
    """Deterministic normalized view of one PDF filename."""

    original: str
    normalized: str
    tokens: tuple[str, ...]
    family_key: tuple[str, ...]
    variable_tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FilenameMatch:
    """One advisory filename-family finding between two profiles."""

    kind: MatchKind
    similarity: float
    shared_family_tokens: tuple[str, ...]


def _is_variable_token(token: str) -> bool:
    """Classify month, date, quarter, version, and counter tokens."""

    return (
        token in _MONTH_TOKENS
        or token in _QUARTER_TOKENS
        or token in _VERSION_WORD_TOKENS
        or bool(_VERSION_PATTERN.fullmatch(token))
        or bool(_NUMERIC_PATTERN.fullmatch(token))
        or bool(_DATEISH_PATTERN.fullmatch(token))
    )


def profile_filename(filename: str) -> FilenameProfile:
    """Normalize a filename and derive its stable family key.

    Normalization applies NFKC, casefolding, `.pdf` removal, and punctuation
    collapse. The family key is the sorted set of substantive tokens after
    removing month/date/quarter/version tokens and single-letter stopwords.
    """

    candidate = unicodedata.normalize("NFKC", filename).casefold().strip()
    if candidate.endswith(".pdf"):
        candidate = candidate[: -len(".pdf")]
    normalized = " ".join(_NON_WORD.sub(" ", candidate).split())
    tokens = tuple(normalized.split())
    variable = tuple(token for token in tokens if _is_variable_token(token))
    family = tuple(
        sorted(
            {
                token
                for token in tokens
                if not _is_variable_token(token) and token not in _STOP_TOKENS
            }
        )
    )
    return FilenameProfile(
        original=filename,
        normalized=normalized,
        tokens=tokens,
        family_key=family,
        variable_tokens=variable,
    )


def compare_filenames(
    candidate: FilenameProfile, existing: FilenameProfile
) -> FilenameMatch | None:
    """Compare two filename profiles and return the strongest advisory match.

    A warning fires when the variable-token-free family keys are identical
    with at least two substantive tokens, when token-set similarity reaches
    the configured threshold, or when Jaro-Winkler similarity does.
    """

    if (
        candidate.family_key
        and len(candidate.family_key) >= MIN_FAMILY_SUBSTANTIVE_TOKENS
        and candidate.family_key == existing.family_key
    ):
        return FilenameMatch(
            kind="filename-family",
            similarity=1.0,
            shared_family_tokens=candidate.family_key,
        )

    if candidate.normalized and existing.normalized:
        token_set = fuzz.token_set_ratio(candidate.normalized, existing.normalized) / 100.0
        if token_set >= TOKEN_SET_SIMILARITY_THRESHOLD:
            return FilenameMatch(
                kind="token-set-similarity",
                similarity=round(token_set, 4),
                shared_family_tokens=tuple(
                    sorted(set(candidate.family_key) & set(existing.family_key))
                ),
            )
        jaro_winkler = JaroWinkler.normalized_similarity(candidate.normalized, existing.normalized)
        if jaro_winkler >= JARO_WINKLER_THRESHOLD:
            return FilenameMatch(
                kind="jaro-winkler-similarity",
                similarity=round(jaro_winkler, 4),
                shared_family_tokens=tuple(
                    sorted(set(candidate.family_key) & set(existing.family_key))
                ),
            )
    return None
