# Markdown, chunk, and Qdrant contract

Status: Current

This contract defines the immutable artifact that crosses the preflight/publication boundary. A
publication implementation may change internally, but it must preserve these identities, encodings,
and verification rules.

## Canonical preparation pipeline

### 1. Native-text extraction

PDF Bridge accepts only English PDFs with usable embedded text. A resource-bounded child process
opens the PDF with pinned `pypdf` and extracts every page using layout-oriented text extraction.
Page order and 1-based page numbers are authoritative. Encrypted, malformed, empty, image-only,
text-insufficient, non-English, over-page, or over-character inputs are rejected; OCR is not called.

The retained extraction artifact contains, per page, `page_number`, normalized layout text,
character count, and SHA-256. Normalization is limited to stable newlines, Unicode normalization,
and removal of disallowed control characters; spaces that express columns are preserved for the
formatter.

### 2. vLLM Markdown formatting

Bridge uses vLLM `/tokenize` and `/tokenizer_info` for the exact served model to pack consecutive
pages without exceeding the context window after prompt overhead, configured maximum output, and a
safety reserve. An individual page that exceeds the resulting input budget is divided at newline
boundaries into deterministic, non-overlapping slices with stable zero-based indices; a single
oversized line is split at a deterministic token boundary. Adjacent page context is included when
it fits so the model can recognize continued tables, but output remains page/slice scoped.

The configured vLLM OpenAI-compatible `/v1/chat/completions` endpoint receives temperature-zero
instructions to preserve meaning, headings, lists, paragraphs, code, and tables. Requests use
`n=1`, no tools, and `response_format=json_schema` with the exact schema below; only a normal stop
finish reason is accepted. Tables use GitHub-Flavored Markdown. The model may format text but may
not summarize, classify, omit, invent, or reorder content.

It must return JSON matching this strict shape, with no unknown fields or prose:

```json
{
  "pages": [
    {
      "page_number": 12,
      "slices": [
        {
          "slice_index": 0,
          "source_text_sha256": "3f91...9b20",
          "markdown": "## Service levels\n\n| Tier | Time |\n|---|---:|\n| A | 4 h |"
        }
      ]
    }
  ]
}
```

Validation requires every requested page and slice exactly once, in order, with the matching source
hash and non-empty Markdown. Markdown also passes structural checks for balanced fences and valid
table rows, and rejects raw HTML, image syntax, generated links, and disallowed control characters.
Fidelity is verified independently of JSON shape: Bridge projects both normalized
source and Markdown back to their ordered Unicode word/number sequences (after Unicode
normalization and removal of Markdown syntax) and requires exact token order and multiplicity.
Missing, invented, changed, or reordered words/numbers reject the response. Projection hashes and a
bounded private mismatch diagnostic are retained with the attempt. An invalid response is retried
with the same bounded input according to policy; after the retry budget, the document becomes
`PREFLIGHT_FAILED`. Missing output is never replaced with raw extraction text.

Validated page/slice Markdown is concatenated in source order. The canonical artifact retains
separate page records and a deterministic document view with `<!-- page:N -->` boundaries. Its hash
and formatter model/revision/prompt-schema profile are part of the prepared revision.

### 3. Structure-aware chunks

The chunker operates on Markdown blocks, not the original extraction. It carries the active heading
path and page range into every chunk, keeps fenced blocks and Markdown tables together when they
fit, and repeats table headers when an oversized table must split. Paragraph/sentence boundaries are
preferred before token boundaries.

- Target: 320 `all-mpnet-base-v2` tokenizer wordpieces.
- Overlap: 48 wordpieces from ordinary prose; never duplicate a complete table merely to create
  overlap.
- Hard maximum: 384 wordpieces, including any repeated heading/table context.
- Ordering: zero-based `chunk_index`, stable for the same canonical Markdown and chunker profile.
- Empty or formatting-only chunks are forbidden.

Each chunk stores `chunk_id`, index, page start/end, heading path, Markdown text, tokenizer count,
and text SHA-256. A chunk ID is deterministic UUIDv5 under the document UUID from the prepared
revision ID, index, and text hash. Any changed canonical content or chunker profile creates a new
prepared revision and therefore a disjoint point set.

## Vector encodings

### Dense

PDF Bridge loads `sentence-transformers/all-mpnet-base-v2` inside the service at a deployment-pinned
model revision. Startup fails if the model cannot load or does not produce finite 768-dimensional
vectors. Chunk Markdown is encoded with normalization enabled; Qdrant uses cosine distance. All
calls pass through one process-wide embedding semaphore, so at most one dense batch executes at a
time. There is no external embeddings endpoint and no alternate-model fallback.

### Sparse BM25

PDF Bridge loads FastEmbed model `Qdrant/bm25` at a pinned package/model revision. Publication and
screening points use **document encoding** (`embed`); candidate searches use **query encoding**
(`query_embed`). Reusing a document vector as a query vector is a contract violation because BM25
document length/term weighting and query weighting are not interchangeable.

Sparse indices and values must be finite, non-negative where required by the model, correlated, and
non-empty for a non-empty chunk. Qdrant sparse vector `bm25` is pre-provisioned with the IDF
modifier required by this model.

## Profiles and immutability

Three independently hashed profiles prevent unrelated policy changes from forcing re-embedding:

- `content_profile_id`: `pypdf` version/options, Unicode normalization, vLLM formatter model
  revision, exact served tokenizer class, strict prompt/output schema, assembly rules, and
  chunker/tokenizer configuration.
- `index_profile_id`: content profile plus MPNet model revision/dimension/normalization, FastEmbed
  and `Qdrant/bm25` revisions/options, vector names, point/payload schema version, and the resolved
  fixed active Qdrant collection target.
- `preflight_policy_id`: index profile plus duplicate thresholds, candidate limits, classifier and
  verifier model/prompt revisions, and completeness policy.

The prepared revision manifest hashes its ordered extraction, Markdown, chunks, both vector sets,
all profile IDs, the exact formatter tokenizer class, and the resolved
`active_qdrant_collection`. Review binds that manifest.
Publication verifies the hash and target and uses the stored artifacts; it may not call the parser,
formatter, chunker, or embedding models again. A changed logical-to-physical mapping makes the
revision stale and requires a new preflight revision.

## Fixed Qdrant schema

Configuration supplies one fixed active Qdrant collection name per logical collection and one
fixed private screening collection. The platform pre-provisions all of them with:

- named dense vector `dense`: size `768`, distance `Cosine`;
- named sparse vector `bm25`: sparse index enabled with modifier `IDF`;
- keyword payload indexes for `document_id`, `collection_key`, `prepared_revision_id`, and
  `schema_version`, plus boolean/keyword indexes for `published` and `visibility`;
- sufficient write/delete permission for Bridge and no screening access for external retrieval.

Bridge validates this shape and fails readiness on any mismatch. It never creates or deletes a
collection, creates an alias, changes vector configuration, or auto-creates a payload index.

## Point contract

Point ID equals the chunk UUID. Every point contains both named vectors and this payload:

```json
{
  "schema_version": 2,
  "document_id": "b7926e86-0efd-4c80-ae6f-12bd4d2bb2c9",
  "prepared_revision_id": "e44ada81-45f5-4dde-9cbc-e6b91a43da54",
  "collection_key": "customer",
  "active_qdrant_collection": "customer-pdfs",
  "chunk_id": "6de8a229-7914-58af-a175-6c830320bd12",
  "chunk_index": 0,
  "page_start": 1,
  "page_end": 2,
  "heading_path": ["Installation", "Windows"],
  "text_sha256": "1ae7...7c89",
  "markdown": "### Windows\n\nInstall the package...",
  "content_profile_id": "sha256:...",
  "index_profile_id": "sha256:...",
  "published": true,
  "visibility": "active"
}
```

Screening points use the same prepared vectors and payload, with `published=false` and
`visibility="screening"`. The logical `collection_key` filter is mandatory for screening searches.
Retrieval-visible active points require `published=true` and `visibility="active"`; publication
temporarily stages the exact revision in that collection as `published=false` and
`visibility="publishing"`. Retrieval must always enforce the active filter. Original filesystem
paths, full PDFs, raw extraction, prompts, raw LLM responses, and numeric vectors are not payload
metadata.

## Preflight search and publication

Candidate discovery is collection-scoped and searches both the mapped active collection and the
private screening collection. Dense queries use incoming normalized chunk vectors. Sparse queries
are freshly generated with `query_embed` from chunk text. Rankings are fused without treating raw
dense and sparse scores as comparable. Deterministic evidence is persisted before the LLM
classifier/verifier runs.

Publication is idempotent and visibility-gated:

1. Verify prepared manifest/profile hashes and valid approval.
2. Upsert every deterministic point ID to the fixed active collection with both vectors,
   `published=false` and `visibility=publishing`, then wait for apply.
3. Filter/count and retrieve the staged revision; require exactly the chunk count, no extra
   revision, both named vectors, and the expected payload schema.
4. Change only that verified revision to `published=true` and `visibility=active`, wait for apply,
   and repeat the exact verification against the active payload.
5. Delete the same document from screening, wait, and verify screening count zero.
6. Commit `READY` and the verified count.

Any partial, stale, or unverified result is `PUBLISH_FAILED`, not `READY`. Retry repeats deterministic
upserts and verification from the same prepared revision.

Deletion reverses visibility in strict order: active filter delete, active zero verification,
screening filter delete, screening zero verification, then filesystem/artifact purge. The
publication record and every deletion checkpoint persist the exact active Qdrant collection used;
retry deletes from that target rather than re-resolving current configuration. Bridge never deletes
the containing Qdrant collection.

See [the API contract](intake-api.md) and [architecture](../architecture.md).
