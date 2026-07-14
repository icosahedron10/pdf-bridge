"""Credential-free subprocess for pypdf layout extraction."""

from __future__ import annotations

import argparse
import json
import sys


def _apply_resource_limits(cpu_seconds: int, memory_bytes: int) -> None:
    try:
        import resource
    except ImportError:  # Windows development; production is Linux.
        return
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def extract(path: str, *, max_pages: int, max_characters: int) -> dict[str, object]:
    """Extract every page in layout mode without truncation or fallback."""

    from pypdf import PdfReader
    from pypdf.errors import FileNotDecryptedError, PdfReadError, WrongPasswordError

    try:
        with open(path, "rb") as handle:
            reader = PdfReader(handle)
            if reader.is_encrypted:
                return {"ok": False, "reason": "encrypted", "detail": "the PDF is encrypted"}
            page_count = len(reader.pages)
            if page_count == 0:
                return {"ok": False, "reason": "empty", "detail": "the PDF has no pages"}
            if page_count > max_pages:
                return {
                    "ok": False,
                    "reason": "page-budget",
                    "detail": f"the PDF has {page_count} pages; the limit is {max_pages}",
                }
            pages: list[dict[str, object]] = []
            total = 0
            for page_number, page in enumerate(reader.pages, start=1):
                text = page.extract_text(extraction_mode="layout") or ""
                total += len(text)
                if total > max_characters:
                    return {
                        "ok": False,
                        "reason": "character-budget",
                        "detail": f"extracted text exceeds {max_characters} characters",
                    }
                pages.append({"page_number": page_number, "layout_text": text})
        return {"ok": True, "page_count": page_count, "pages": pages}
    except (WrongPasswordError, FileNotDecryptedError):
        return {"ok": False, "reason": "encrypted", "detail": "the PDF requires a password"}
    except (
        PdfReadError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        OSError,
        RecursionError,
        OverflowError,
    ) as exc:
        return {
            "ok": False,
            "reason": "malformed",
            "detail": f"{type(exc).__name__}: {exc}"[:500],
        }


def main() -> None:
    parser = argparse.ArgumentParser(prog="pdf-bridge-extract")
    parser.add_argument("path")
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--max-characters", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--memory-bytes", type=int, required=True)
    arguments = parser.parse_args()
    _apply_resource_limits(arguments.cpu_seconds, arguments.memory_bytes)
    try:
        result = extract(
            arguments.path,
            max_pages=arguments.max_pages,
            max_characters=arguments.max_characters,
        )
    except MemoryError:
        result = {
            "ok": False,
            "reason": "malformed",
            "detail": "the parser exceeded its memory limit",
        }
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
