"""Subprocess entry point that extracts PDF text under resource limits.

This module runs as ``python -m pdf_bridge.services.extraction_child`` in a
dedicated child process. It imports only the standard library and pypdf so a
hostile document can, at worst, exhaust the limits placed on this process.

CPU and address-space limits use ``resource`` and therefore apply on Linux
deployments only; page, character, and wall-clock limits are enforced
everywhere. A resource-limited subprocess is containment, not a sandbox.
"""

from __future__ import annotations

import argparse
import io
import json
import sys


def _apply_resource_limits(cpu_seconds: int, memory_bytes: int) -> None:
    try:
        import resource
    except ImportError:  # Windows development hosts; production is Linux.
        return
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def _fail(reason: str, detail: str) -> None:
    json.dump({"ok": False, "reason": reason, "detail": detail[:2000]}, sys.stdout)
    sys.stdout.flush()
    sys.exit(0)


def extract(path: str, *, max_pages: int, max_characters: int) -> dict[str, object]:
    """Extract page-mapped text, failing closed on any parser surprise."""

    from pypdf import PdfReader
    from pypdf.errors import FileNotDecryptedError, PdfReadError, WrongPasswordError

    try:
        with open(path, "rb") as handle:
            data = handle.read()
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return {"ok": False, "reason": "encrypted", "detail": "the PDF is encrypted"}
        page_count = len(reader.pages)
        if page_count > max_pages:
            return {
                "ok": False,
                "reason": "page-budget",
                "detail": f"the PDF has {page_count} pages; the limit is {max_pages}",
            }
        pages: list[dict[str, object]] = []
        total_characters = 0
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            total_characters += len(text)
            if total_characters > max_characters:
                return {
                    "ok": False,
                    "reason": "character-budget",
                    "detail": (f"extracted text exceeds the {max_characters}-character limit"),
                }
            pages.append({"number": number, "text": text})
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
            "detail": f"{type(exc).__name__}: {exc}"[:2000],
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
        _fail("malformed", "the parser exceeded its memory limit")
        return
    json.dump(result, sys.stdout)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
