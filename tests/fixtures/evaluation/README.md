# PDF evaluation corpus

This directory is PDF Bridge's checked-in, offline acceptance corpus. Every document is original
test material authored for this repository and released under `CC0-1.0`; no third-party text,
fonts, images, or confidential material is included.

The corpus covers native English headings and lists, a six-column table repeated across pages,
page-boundary provenance, a versioned filename family, and encrypted, malformed, and image-only
rejection paths. `manifest.json` records immutable file hashes plus structural golden expectations.
The acceptance test deliberately checks page/slice coverage and table semantics rather than
freezing a formatter's harmless whitespace choices or prose presentation.

Normal tests use the checked-in PDFs and need no generator dependency. To rebuild them:

1. Use Python 3.12 and install exactly `reportlab==4.4.3`.
2. Run `python tests/fixtures/evaluation/build_corpus.py` from the repository root.
3. Run the corpus acceptance test and render every PDF for visual inspection.
4. Review the content change, then update `manifest.json` hashes and golden values explicitly.

The builder uses ReportLab's invariant mode, uncompressed page streams, fixed metadata, built-in
fonts, and no clock or random input. It never rewrites `manifest.json`, so regenerated fixture drift
fails tests until a reviewer deliberately accepts new golden values. The encrypted fixture password
exists only to construct a deterministic rejection case and is not used by PDF Bridge.

Limitations: OCR remains out of scope, the image-only file uses vector artwork instead of a scanned
third-party image, and the test emulates the private vLLM protocol and tokenizer boundary. It still
runs the production extraction subprocess, English gate, strict formatter validation, and
structure-aware chunker offline.
