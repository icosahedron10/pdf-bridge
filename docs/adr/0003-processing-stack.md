# ADR 0003: Native-text Markdown and hybrid embedding stack

Status: Accepted

## Context

The corpus consists exclusively of English, native-text PDFs. Tables must become useful Markdown,
dense semantic retrieval must use the approved MPNet model, sparse retrieval must use standard
BM25 behavior, and licensing rules exclude PyMuPDF. Silent degradation would make an inspected
prepared revision differ from what retrieval receives.

## Decision

The processing stack is fixed as follows:

1. Pinned `pypdf` extracts page-scoped layout text under hard resource/content limits. OCR is out of
   scope; image-only and text-insufficient PDFs are rejected.
2. A configured vLLM OpenAI-compatible chat-completions endpoint formats bounded consecutive pages
   or deterministic page slices into strict page-scoped JSON containing Markdown. Tables use
   GitHub-Flavored Markdown. Exact ordered Unicode word/number projections of normalized source and
   formatted Markdown must match, rejecting omission, invention, or reordering. Invalid/incomplete
   output exhausts a bounded retry and fails preflight; raw text is never a fallback.
3. A structure-aware chunker targets 320 MPNet wordpieces with 48-wordpiece overlap and never
   exceeds the model's 384-wordpiece input limit.
4. PDF Bridge locally loads pinned `sentence-transformers/all-mpnet-base-v2`, produces normalized
   768-dimensional vectors, and serializes encoding through one lane. No embedding HTTP service or
   alternate dense model is used.
5. Pinned FastEmbed `Qdrant/bm25` produces sparse vectors. Stored points use document encoding;
   preflight/search requests use query encoding. Qdrant's sparse IDF modifier is required.
6. Formatting, chunks, both vector sets, provenance, and profile hashes form one immutable prepared
   revision. Approval publishes it without recomputation.

Duplicate screening and the independent LLM classifier/verifier remain after preparation and before
publication. They are preflight policy, not ingestion and not authority to mutate content.

## Consequences

- Model assets must be available at startup and readiness fails hard on missing assets, dimension
  mismatch, or incompatible Qdrant schema.
- vLLM formatting is a required dependency; an outage or invalid response blocks preparation rather
  than lowering quality invisibly.
- Page/slice correlation and source hashes make omissions and reordering detectable.
- One dense lane favors predictable memory over throughput and is appropriate for the expected
  five-document peak queue.
- Changes to parsing/formatting/chunking or embedding profiles create a new index revision; changes
  only to duplicate/LLM policy do not require re-embedding unchanged content.

## Rejected alternatives

- **PyMuPDF/PyMuPDF4LLM:** unacceptable licensing fit for this project.
- **OCR or auto-detection fallback:** expands the input and quality contract beyond the agreed
  native-text corpus.
- **Raw `pypdf` text as canonical content:** does not reliably preserve tables and would hide vLLM
  failure.
- **Remote dense embedding endpoint:** adds an unnecessary service boundary for the fixed approved
  model.
- **One BM25 encoding for documents and queries:** mathematically incorrect for the selected model.

Related contract: [Markdown, chunks, and Qdrant](../contracts/chunks-qdrant.md).
