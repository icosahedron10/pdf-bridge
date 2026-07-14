# Configuration

**Status: Current**

This document defines the implemented post-refactor configuration contract. The checked-in
`.env.example` is the canonical shape reference; it contains placeholders, not deployable values.

PDF Bridge reads strict <code>PDF_BRIDGE_*</code> settings and fails startup when a required value
is missing, inconsistent, or unsafe. There is no Jenkins configuration, external embedding
provider, OCR mode, or automatic Qdrant provisioning fallback.

The shared `.env` also contains nine launcher-only keys: `PDF_BRIDGE_BIND_ADDRESS`,
`PDF_BRIDGE_PORT`, `PDF_BRIDGE_MODEL_CACHE_HOST_PATH`, `PDF_BRIDGE_QDRANT_ADMIN_API_KEY`,
`PDF_BRIDGE_URL`, `PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES`,
`PDF_BRIDGE_STREAMLIT_IDENTITY_HEADER`, `PDF_BRIDGE_STREAMLIT_BIND_ADDRESS`, and
`PDF_BRIDGE_STREAMLIT_PORT`. The Litestar settings loader deliberately filters only those keys
from dotenv input; every other unknown dotenv key remains an error. Compose consumes them without
injecting the Qdrant admin/signing key into either Bridge or Streamlit.

| Launcher setting | Required/default | Purpose |
|---|---:|---|
| <code>PDF_BRIDGE_BIND_ADDRESS</code> | <code>127.0.0.1</code> | Host address where Compose publishes the API port |
| <code>PDF_BRIDGE_PORT</code> | <code>8000</code> | Host port published to container port 8000 |
| <code>PDF_BRIDGE_MODEL_CACHE_HOST_PATH</code> | Required by Compose | Absolute pre-seeded host directory mounted read-only at `/var/cache/pdf-bridge-models` |
| <code>PDF_BRIDGE_STREAMLIT_BIND_ADDRESS</code> | <code>127.0.0.1</code> | Host address where Compose publishes Streamlit |
| <code>PDF_BRIDGE_STREAMLIT_PORT</code> | <code>8501</code> | Host port published to Streamlit container port 8501 |

## Logical collections and storage

<code>PDF_BRIDGE_COLLECTIONS</code> is a JSON array. Each logical collection must name the
pre-provisioned physical Qdrant collection that stores its published chunks.

~~~json
[
  {
    "key": "customer",
    "display_name": "Customer Product",
    "description": "Approved customer-facing material.",
    "audience": "customer",
    "qdrant_collection_name": "customer-product-pdfs"
  },
  {
    "key": "internal",
    "display_name": "Internal",
    "description": "Internal reference material.",
    "audience": "internal",
    "qdrant_collection_name": "internal-pdfs"
  }
]
~~~

Collection keys and Qdrant names must be unique. A Qdrant name is a physical collection name, not
an alias. It cannot equal the screening collection name. Changing the mapping while documents
exist is a reset-and-reingestion event, not an online rename or migration.

| Setting | Required/default | Contract |
|---|---|---|
| <code>PDF_BRIDGE_STORAGE_ROOT</code> | Required | Absolute writable root for the opaque UUID object store and private derived artifacts |
| <code>PDF_BRIDGE_DATABASE_URL</code> | SQLite beneath storage by default | One catalog; other database engines are unsupported |
| <code>PDF_BRIDGE_COLLECTIONS</code> | Required | Immutable logical-to-physical collection map shown above |
| <code>PDF_BRIDGE_SESSION_SECRET</code> | Required | Unique secret of at least 32 characters |
| <code>PDF_BRIDGE_APP_ENV</code> | <code>development</code> | <code>development</code>, <code>test</code>, or <code>enterprise</code> |
| <code>PDF_BRIDGE_AUTH_MODE</code> | <code>anonymous-poc</code> | Use <code>trusted-header</code> behind approved SSO outside an isolated POC |
| <code>PDF_BRIDGE_ALLOWED_HOSTS</code> | Required outside development | JSON host allowlist |
| <code>PDF_BRIDGE_TRUSTED_PROXY_CIDRS</code> | Required for trusted-header mode | Direct peers allowed to assert identity |
| <code>PDF_BRIDGE_TRUSTED_IDENTITY_HEADER</code> | Deployment-specific | Identity header accepted only from a trusted direct peer |

Files remain in the existing opaque UUID-sharded object store. Collections are catalog and Qdrant
partitions; collection names and user filenames never become filesystem paths.

## Upload, scanning, and PDF extraction

| Setting | Default | Rule |
|---|---:|---|
| <code>PDF_BRIDGE_MAX_UPLOAD_BYTES</code> | <code>52428800</code> | Must not exceed the ClamAV stream maximum |
| <code>PDF_BRIDGE_UPLOAD_CHUNK_BYTES</code> | <code>1048576</code> | Streaming request and hash chunk |
| <code>PDF_BRIDGE_CLAMD_HOST</code> | <code>clamav</code> | Private daemon host |
| <code>PDF_BRIDGE_CLAMD_PORT</code> | <code>3310</code> | Must not be publicly exposed |
| <code>PDF_BRIDGE_CLAMD_TIMEOUT_SECONDS</code> | <code>30</code> | Hard probe and scan timeout |
| <code>PDF_BRIDGE_CLAMD_STREAM_MAX_BYTES</code> | <code>67108864</code> | Must be at least the upload limit |
| <code>PDF_BRIDGE_PYPDF_EXTRACTION_MODE</code> | <code>layout</code> | Fixed mode; preserves spacing needed by the formatter |
| <code>PDF_BRIDGE_PARSE_WALL_CLOCK_SECONDS</code> | <code>120</code> | Parent-side hard timeout |
| <code>PDF_BRIDGE_PARSE_CPU_SECONDS</code> | <code>90</code> | Linux child-process CPU limit |
| <code>PDF_BRIDGE_PARSE_MEMORY_BYTES</code> | <code>1073741824</code> | Linux child-process address-space limit |
| <code>PDF_BRIDGE_MAX_PAGES</code> | <code>2000</code> | Reject above the limit; never truncate |
| <code>PDF_BRIDGE_MAX_EXTRACTED_CHARACTERS</code> | <code>5000000</code> | Reject above the normalized-text limit |

The supported corpus is English, native-text PDF. OCR and image-to-text fallback are disabled.
Encrypted, image-only, malformed, empty, or text-insufficient PDFs fail preflight and are purged
according to the lifecycle policy.

## vLLM Markdown formatter and advisory models

Markdown formatting and duplicate-classification evidence are separate provider boundaries. Both
use private OpenAI-compatible chat-completions APIs, but each has its own required endpoint and
credential. Bridge never reroutes formatter work to the advisory endpoint or advisory work to the
formatter endpoint.

| Setting | Default | Contract |
|---|---:|---|
| <code>PDF_BRIDGE_FORMATTER_API_URL</code> | Required | vLLM server root used for `/v1/chat/completions`, `/tokenize`, and `/tokenizer_info` |
| <code>PDF_BRIDGE_FORMATTER_API_TOKEN</code> | Required | Formatter-only bearer credential |
| <code>PDF_BRIDGE_FORMATTER_MODEL_ID</code> | Required | Exact model used only to produce page-scoped Markdown |
| <code>PDF_BRIDGE_FORMATTER_MODEL_REVISION</code> | Required | Immutable served-model identity included in the content profile |
| <code>PDF_BRIDGE_FORMATTER_TOKENIZER_CLASS</code> | Required | Exact served tokenizer class included in the content profile |
| <code>PDF_BRIDGE_FORMATTER_PROMPT_REVISION</code> | Required | Immutable formatter prompt identity |
| <code>PDF_BRIDGE_FORMATTER_SCHEMA_REVISION</code> | Required | Immutable strict-output schema identity |
| <code>PDF_BRIDGE_FORMATTER_TIMEOUT_SECONDS</code> | <code>120</code> | Hard timeout per formatter request |
| <code>PDF_BRIDGE_FORMATTER_MAX_INPUT_TOKENS</code> | <code>24000</code> | Hard request budget |
| <code>PDF_BRIDGE_FORMATTER_MAX_OUTPUT_TOKENS</code> | <code>12000</code> | Hard response budget |
| <code>PDF_BRIDGE_FORMATTER_TOKEN_SAFETY_RESERVE</code> | <code>512</code> | Reserved context beyond prompt, input, and maximum output |
| <code>PDF_BRIDGE_FORMATTER_MAX_PAGES_PER_REQUEST</code> | <code>8</code> | Secondary cap; token budget remains authoritative |
| <code>PDF_BRIDGE_FORMATTER_MAX_ATTEMPTS</code> | <code>2</code> | Initial call plus one validation retry |
| <code>PDF_BRIDGE_LLM_API_URL</code> | Required | Separate advisory OpenAI-compatible base ending in `/v1`; Bridge uses `/v1/models`, `/v1/chat/completions`, and the server-root `/tokenize` |
| <code>PDF_BRIDGE_LLM_API_TOKEN</code> | Required | Advisory-only bearer credential |
| <code>PDF_BRIDGE_LLM_CLASSIFIER_MODEL</code> | Required | Exact duplicate-classifier model ID |
| <code>PDF_BRIDGE_LLM_CLASSIFIER_MODEL_REVISION</code> | Required | Immutable classifier model identity |
| <code>PDF_BRIDGE_LLM_CLASSIFIER_PROMPT_REVISION</code> | Required | Immutable classifier prompt identity |
| <code>PDF_BRIDGE_LLM_VERIFIER_MODEL</code> | Required | Exact skeptical-verifier model ID |
| <code>PDF_BRIDGE_LLM_VERIFIER_MODEL_REVISION</code> | Required | Immutable verifier model identity |
| <code>PDF_BRIDGE_LLM_VERIFIER_PROMPT_REVISION</code> | Required | Immutable verifier prompt identity |
| <code>PDF_BRIDGE_LLM_TIMEOUT_SECONDS</code> | <code>60</code> | Hard timeout per classifier or verifier call |
| <code>PDF_BRIDGE_LLM_MAX_INPUT_TOKENS</code> | <code>12000</code> | Hard evidence budget per call |
| <code>PDF_BRIDGE_LLM_MAX_OUTPUT_TOKENS</code> | <code>2048</code> | Hard structured-output budget per call |
| <code>PDF_BRIDGE_LLM_MAX_ATTEMPTS</code> | <code>2</code> | Initial advisory call plus one validation retry |

Bridge requires `/tokenizer_info` to report the exact configured tokenizer class; a missing or
different class fails readiness and formatting. The model list independently proves the configured
model ID, and the tokenizer endpoints supply served token counts and the context window. It packs
consecutive complete pages using the server's token count and requires prompt
tokens plus source tokens, configured maximum output, and the safety reserve to fit the served
context window. A page that cannot fit is divided into deterministic, ordered, non-overlapping
slices with stable zero-based indices, preferring newline boundaries and using a deterministic
token boundary only when one line itself cannot fit. Adjacent-page context may be included when it
fits, but output remains scoped to the requested page and slice. A page is not rejected merely
because it needs slicing.

Formatter output must satisfy the strict page/slice schema, source-hash correlation, complete
coverage, and fidelity checks before slices are reassembled in source order. Invalid, incomplete,
timed-out, or over-budget formatting blocks ingestion; raw pypdf text is never substituted as
canonical Markdown.

URL shape is validated at startup: the formatter value is the vLLM server root and must not end in
`/v1`, while the advisory value must end in `/v1`. This prevents doubled or missing version path
segments.

Classifier and verifier model IDs are independent configuration values on the advisory boundary.
Their outputs are preflight evidence only and never authorize publication, replacement, or
deletion. Invalid or unavailable advisory output is retained as incomplete evidence and forces
`REVIEW_REQUIRED`; it is never interpreted as a clear result. A deterministic screening/Qdrant
failure instead produces `PREFLIGHT_FAILED` because the candidate set is not trustworthy.

## Local dense and sparse embedding

Dense and sparse vectors are produced inside the PDF Bridge process. There is no embedding API URL
or embedding credential.

| Setting | Default | Rule |
|---|---:|---|
| <code>PDF_BRIDGE_DENSE_MODEL_ID</code> | <code>sentence-transformers/all-mpnet-base-v2</code> | Fixed model |
| <code>PDF_BRIDGE_DENSE_MODEL_REVISION</code> | Required | Immutable approved commit revision; floating branches or tags fail startup |
| <code>PDF_BRIDGE_MODEL_CACHE_DIR</code> | Required | Absolute controlled cache containing the approved model assets |
| <code>PDF_BRIDGE_MODEL_LOCAL_FILES_ONLY</code> | <code>true</code> | Enforced; runtime model downloads are forbidden |
| <code>PDF_BRIDGE_DENSE_DEVICE</code> | <code>cpu</code> | CPU is the supported default |
| <code>PDF_BRIDGE_DENSE_BATCH_SIZE</code> | <code>16</code> | Lower to meet the host memory budget |
| <code>PDF_BRIDGE_DENSE_DIMENSION</code> | <code>768</code> | Enforced fixed dimension |
| <code>PDF_BRIDGE_EMBEDDING_LANES</code> | <code>1</code> | Enforced; serializes local model execution |
| <code>PDF_BRIDGE_SPARSE_MODEL_ID</code> | <code>Qdrant/bm25</code> | Fixed FastEmbed sparse model |
| <code>PDF_BRIDGE_SPARSE_MODEL_REVISION</code> | Required | Exact 64-character SHA-256 of the canonical sparse `manifest.json`; also names its pre-seeded directory |
| <code>PDF_BRIDGE_SPARSE_IDF</code> | <code>true</code> | Enforced in the Qdrant sparse-vector schema |
| <code>PDF_BRIDGE_MAX_CHUNKS</code> | <code>10000</code> | Reject rather than truncate |

Pre-seed the Sentence Transformers cache below
<code>$PDF_BRIDGE_MODEL_CACHE_DIR/sentence-transformers</code> so the configured dense commit can be
resolved with local-files-only loading. Pre-seed the exact FastEmbed files at
<code>$PDF_BRIDGE_MODEL_CACHE_DIR/fastembed/$PDF_BRIDGE_SPARSE_MODEL_REVISION</code>; Bridge passes
that directory as FastEmbed's explicit model path. The revision is the SHA-256 of canonical
<code>manifest.json</code>, which names the fixed model and the SHA-256 of every file in the
directory. The inventory must match exactly, symlinks are rejected, and the required
<code>english.txt</code> stopword asset must be non-empty.
Both model paths must be readable through the container's read-only cache mount.

The dense model must load at startup and produce finite, normalized 768-dimensional vectors.
Sentence Transformer inputs are kept below the model's 384-wordpiece ceiling by the canonical
chunker. FastEmbed must use the document encoding for stored chunks and the query encoding for
preflight searches. Its <code>Qdrant/bm25</code> model and Qdrant IDF modifier are both required.

Pin the Sentence Transformers, FastEmbed, tokenizer, and model-artifact versions as one index
profile. Cache changes are deployments and require the same validation as dependency changes.

## Qdrant

| Setting | Required/default | Purpose |
|---|---|---|
| <code>PDF_BRIDGE_QDRANT_URL</code> | Required | Authenticated REST endpoint |
| <code>PDF_BRIDGE_QDRANT_ADMIN_API_KEY</code> | Compose launcher only | Independent HS256 signing/admin key injected into Qdrant and never into Bridge |
| <code>PDF_BRIDGE_QDRANT_API_KEY</code> | Required | Pre-generated granular HS256 JWT used by Bridge |
| <code>PDF_BRIDGE_QDRANT_TIMEOUT_SECONDS</code> | <code>30</code> | Hard request timeout |
| <code>PDF_BRIDGE_QDRANT_SCREENING_COLLECTION_NAME</code> | Required | Fixed pre-provisioned private collection for pending chunks |

Self-hosted Qdrant uses its configured admin API key as the HS256 signing key when JWT RBAC is
enabled. The Compose launcher therefore supplies `PDF_BRIDGE_QDRANT_ADMIN_API_KEY` only as
Qdrant's `QDRANT__SERVICE__API_KEY`; the Bridge container does not receive it. Generate Bridge's
token offline with the exact header `{"alg":"HS256","typ":"JWT"}` and a payload containing only
`exp` plus one `rw` rule for every enabled active physical collection and the screening collection:

```json
{
  "access": [
    {"collection": "customer-product-pdfs", "access": "rw"},
    {"collection": "internal-pdfs", "access": "rw"},
    {"collection": "pdf-bridge-screening", "access": "rw"}
  ],
  "exp": 1783987200
}
```

Sign the compact token with HS256 and the independent admin key, then store only the result in
`PDF_BRIDGE_QDRANT_API_KEY`. The `exp` value is a Unix timestamp and must remain more than 30
seconds in the future at Bridge startup. Rotate the scoped token before it expires; changing the
admin signing key invalidates every outstanding token and requires a coordinated Qdrant restart and
token replacement.

Outside test mode, Bridge decodes but does not verify the token locally. It rejects malformed or
non-canonical compact JWTs, any algorithm other than HS256, absent or near-expiry `exp`, global
`r`/`m` access, non-`rw` rules, duplicate rules, extra claims, disabled or unrelated collections,
or omission of an enabled/screening collection. Bridge cannot verify the signature because it
deliberately does not possess the signing key; Qdrant performs signature verification, and the
authenticated collection probes make an invalid or forged signature fail readiness. These claims
follow Qdrant's [granular access JWT contract](https://qdrant.tech/documentation/security/#jwt-configuration).

The platform team pre-provisions every configured active collection and the one screening
collection. PDF Bridge validates them through readiness, then only reads, searches, upserts,
counts, and deletes points. It never creates, deletes, renames, or reconfigures a collection,
payload index, or alias.

Every configured collection must have:

- named dense vector <code>dense</code>: size <code>768</code>, Cosine distance;
- named sparse vector <code>bm25</code>: Qdrant IDF modifier enabled;
- compatible on-disk vector and payload settings for the pinned Qdrant version;
- payload indexes required for document ID, collection key, publication state, and schema version
  filtering.

The screening collection has the same vector schema and is not an active retrieval source. Its
name must be different from every logical collection's <code>qdrant_collection_name</code>.
Missing collections, aliases supplied in place of physical names, dimension/distance drift, absent
IDF, or required-index drift make readiness fail. Bridge reports the difference and does not try to
repair it.

The exact collection-scoped `rw` JWT gives Bridge collection-description, filtered
alias-metadata-read, and point search/read/write/count/delete permissions for the configured active
and screening collections. It excludes collection creation/deletion/reconfiguration, alias
mutation, global manage access, and unrelated collections. External retrieval gets a separate
read-only JWT for active collections and no access to screening.

## Process and queue limits

| Setting | Default | Rule |
|---|---:|---|
| <code>PDF_BRIDGE_WORKER_ENABLED</code> | <code>true</code> | Disable only for isolated maintenance or tests |
| <code>PDF_BRIDGE_WORKER_EXECUTION_SLOTS</code> | <code>2</code> | Enforced value |
| <code>PDF_BRIDGE_WORKER_POLL_SECONDS</code> | <code>1</code> | Best-effort dispatch interval |
| <code>PDF_BRIDGE_WORKER_LEASE_SECONDS</code> | <code>300</code> | Durable recovery lease |
| <code>PDF_BRIDGE_WORKER_HEARTBEAT_SECONDS</code> | <code>30</code> | Must be shorter than the lease |

Run exactly one Uvicorn process and one in-process worker against the SQLite catalog. The worker
has two operation slots, while local embedding has one serialized lane. The capacity target is a
best-effort peak queue of approximately five documents, not a latency SLA. Multiple app processes,
replicas, or workers are unsupported until catalog coordination is redesigned.

Deletion operations are admitted at higher priority than parsing, formatting, preflight, and
publication work. Priority affects queue selection only; it does not interrupt an in-flight model
call.

## Pipeline identities

Persist three independent fingerprints:

- the content profile: pypdf version and layout settings, formatter model/revision, exact served
  tokenizer class and prompt, strict formatter schema, Markdown serializer, and chunker;
- the index profile: content profile plus dense model revision, tokenizer, normalization,
  FastEmbed/BM25 versions, IDF mode, vector names, dimensions, and point schema;
- the preflight policy profile: index profile plus duplicate thresholds, prompts, classifier and
  verifier IDs, and evidence limits.

A content or index profile change requires evaluation and reingestion. A policy-only change creates
a new preflight revision but does not by itself invalidate already published vectors.

## Canonical Streamlit client

Streamlit is the supported operator interface. It is a pure HTTP client of PDF Bridge and must use
the same authenticated session, CSRF, idempotency, upload, polling, decision, retry, replacement,
download, and deletion APIs as any other client. It never reads SQLite or storage paths directly.
The Litestar service remains the API and lifecycle authority; there is no embedded Litestar HTML
workspace.

These settings belong to the Streamlit process rather than `Settings` in the Litestar service:

| Setting | Default | Rule |
|---|---:|---|
| <code>PDF_BRIDGE_URL</code> | <code>http://127.0.0.1:8000</code> | Deployment-owned fixed private Bridge root; operators cannot override it |
| <code>PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES</code> | <code>5</code> | Selection cap from 1 through 20; each selected PDF is still one API request |
| <code>PDF_BRIDGE_STREAMLIT_IDENTITY_HEADER</code> | Unset | Optional proxy-injected identity header name that Streamlit requires on its incoming request and forwards to Bridge |

Streamlit refuses Bridge redirects and does not offer an operator-editable service URL. In
trusted-header mode, block direct access to both applications, inject the configured identity
header at the Streamlit ingress, and configure Bridge to trust Streamlit's direct-peer CIDR and the
same header name. Streamlit forwards the received value server-side; the browser does not choose or
manufacture it. Configure approved TLS trust at deployment. Do not give Streamlit a Qdrant
credential or direct filesystem access. The reference Compose service is read-only, joins only the
dedicated internal operator network, waits for Bridge readiness, and contains only the pinned
Streamlit and HTTP client runtimes; it does not contain the Bridge package or model stack. ClamAV
and Qdrant use separate internal networks that Streamlit cannot join.

## Optional operator search proxy

The external retrieval product remains separately owned. Bridge may expose its strict operator-only
proxy for the Streamlit Search page; it does not implement ranking or end-user authorization.

| Setting | Required/default | Rule |
|---|---|---|
| <code>PDF_BRIDGE_SEARCH_API_URL</code> | Optional | Private external retrieval base URL; blank disables operator search only |
| <code>PDF_BRIDGE_SEARCH_API_TOKEN</code> | Required when URL is set | Bridge-only upstream credential; never sent to Streamlit |
| <code>PDF_BRIDGE_SEARCH_API_TIMEOUT_SECONDS</code> | <code>10</code> | Hard proxy timeout |

Bridge validates returned logical collection keys and document UUIDs against the catalog and
rejects non-`READY`, unknown, or cross-collection hits. If the integration is disabled or fails,
the operator endpoint returns an explicit sanitized error; it never falls back to private screening
or direct local ranking. This optional dependency does not make core upload/delete readiness fail.

## Secrets and production behavior

- Inject session, ClamAV (when authenticated), Qdrant, formatter, advisory LLM, and retrieval
  credentials from an approved secret store.
- Never reuse the Qdrant key, formatter token, advisory LLM token, or session secret.
- Do not place credentials, PDF text, Markdown, prompts, vectors, model output, or full object
  paths in logs or support bundles.
- Require TLS whenever credentials or document content cross a host boundary.
- Treat the checked-in environment example as a shape reference only; populated environment files
  remain untracked.
- Fail readiness rather than falling back when ClamAV, vLLM, the local model cache, or Qdrant
  schema is unavailable or inconsistent.
