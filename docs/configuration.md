# Configuration

PDF Bridge reads `PDF_BRIDGE_*` environment variables through strict Pydantic settings. Linux is
the supported deployment host because parser CPU/address-space limits rely on Linux `resource`
controls. Docker Compose is the reference POC topology.

## Required identity and storage

| Variable | Purpose |
|---|---|
| `PDF_BRIDGE_STORAGE_ROOT` | Absolute writable root outside the checkout and synchronized folders |
| `PDF_BRIDGE_DATABASE_URL` | Optional SQLite URL; defaults to `catalog.sqlite3` under storage |
| `PDF_BRIDGE_COLLECTIONS` | JSON array of immutable collection definitions |
| `PDF_BRIDGE_SESSION_SECRET` | Session signing/encryption secret; unique and at least 32 characters |
| `PDF_BRIDGE_APP_ENV` | `development`, `test`, or `enterprise` |
| `PDF_BRIDGE_AUTH_MODE` | `anonymous-poc` or `trusted-header` |
| `PDF_BRIDGE_ALLOWED_HOSTS` | JSON list accepted by trusted-host middleware |
| `PDF_BRIDGE_TRUSTED_PROXY_CIDRS` | JSON CIDR list allowed to assert the identity header |
| `PDF_BRIDGE_TRUSTED_IDENTITY_HEADER` | Header accepted only from a trusted direct peer |

Collection entries contain `key`, `display_name`, `description`, and `audience`. Keys are lowercase
path-safe identifiers and also name stable active Qdrant aliases. Assignment is immutable.

```json
[
  {
    "key": "customer",
    "display_name": "Customer Product",
    "description": "Approved customer-facing material.",
    "audience": "customer"
  }
]
```

Outside tests, replace the development session secret. Enterprise mode additionally requires
trusted-header authentication, one or more trusted proxy CIDRs, and a non-wildcard host list.

## Upload and ClamAV

| Variable | Default | Notes |
|---|---:|---|
| `PDF_BRIDGE_MAX_UPLOAD_BYTES` | `52428800` | Must not exceed ClamAV stream maximum |
| `PDF_BRIDGE_MAX_UPLOAD_FILES` | `20` | Browser selection limit |
| `PDF_BRIDGE_UPLOAD_CHUNK_BYTES` | `1048576` | Streaming chunk size |
| `PDF_BRIDGE_CLAMD_HOST` | `clamav` | Private daemon host |
| `PDF_BRIDGE_CLAMD_PORT` | `3310` | Never publish this port |
| `PDF_BRIDGE_CLAMD_TIMEOUT` | `30` | Scan and probe timeout in seconds |
| `PDF_BRIDGE_CLAMD_STREAM_MAX_BYTES` | `67108864` | Must match or stay below clamd `StreamMaxLength` |

Scanner errors fail closed. The canonical object is promoted only after a clean verdict.

## Worker and parser limits

| Variable | Default | Constraint |
|---|---:|---|
| `PDF_BRIDGE_WORKER_ENABLED` | `true` | Disable only in isolated tests/maintenance |
| `PDF_BRIDGE_WORKER_POLL_SECONDS` | `1` | Positive |
| `PDF_BRIDGE_WORKER_LEASE_SECONDS` | `300` | 10–3600 seconds |
| `PDF_BRIDGE_WORKER_HEARTBEAT_SECONDS` | `30` | At least 1 and shorter than lease |
| `PDF_BRIDGE_PARSE_WALL_CLOCK_SECONDS` | `120` | Positive parent-side timeout |
| `PDF_BRIDGE_PARSE_CPU_SECONDS` | `90` | Positive Linux CPU limit |
| `PDF_BRIDGE_PARSE_MEMORY_BYTES` | `1073741824` | At least 64 MiB |
| `PDF_BRIDGE_ANALYSIS_MAX_PAGES` | `2000` | Reject above limit; never truncate |
| `PDF_BRIDGE_ANALYSIS_MAX_CHARACTERS` | `5000000` | Normalized character cap |
| `PDF_BRIDGE_ANALYSIS_MAX_CHUNKS` | `10000` | Deterministic chunk cap |

Run exactly one Uvicorn process. The worker already has two execution slots. Adding Uvicorn workers
creates independent in-process collection locks and violates the supported freshness model.

The parser child has no application credential or provider responsibility, but it is not a complete
sandbox. Production hardening still requires an owned disposable isolation boundary and restricted
network/process privileges.

## Embedding and classification providers

Both providers use private OpenAI-compatible HTTP APIs through the existing `httpx` client.

| Variable | Required for full analysis | Notes |
|---|---|---|
| `PDF_BRIDGE_EMBEDDING_API_URL` | Yes | Base URL; Bridge calls `/embeddings` |
| `PDF_BRIDGE_EMBEDDING_API_TOKEN` | Deployment policy | Bearer credential for the private endpoint |
| `PDF_BRIDGE_EMBEDDING_MODEL_ID` | Yes | Exact configured model ID |
| `PDF_BRIDGE_EMBEDDING_DIMENSION` | Yes | Exact finite-vector dimension |
| `PDF_BRIDGE_EMBEDDING_TIMEOUT` | Yes | Positive seconds, default `30` |
| `PDF_BRIDGE_LLM_API_URL` | Yes | Base URL; Bridge calls `/chat/completions` |
| `PDF_BRIDGE_LLM_API_TOKEN` | Deployment policy | Bearer credential |
| `PDF_BRIDGE_LLM_CLASSIFIER_MODEL` | Yes | First temperature-zero model |
| `PDF_BRIDGE_LLM_VERIFIER_MODEL` | Yes | Independent skeptical verifier |
| `PDF_BRIDGE_LLM_TIMEOUT` | Yes | Positive seconds, default `60` |

Model ID, embedding dimension, parser/chunker versions, thresholds, and both classifier model IDs are
hashed into the analysis pipeline fingerprint. Treat any intentional change as an evaluation and
reindex event, not a transparent configuration tweak.

If semantic providers are missing or unavailable, analysis records explicit incompleteness and asks
for review. Keep remains a durable decision, but publication cannot finish until embedding and
Qdrant recover.

## Qdrant

| Variable | Default in Compose | Purpose |
|---|---|---|
| `PDF_BRIDGE_QDRANT_URL` | `http://qdrant:6333` | Authenticated Qdrant REST endpoint |
| `PDF_BRIDGE_QDRANT_API_KEY` | Required | Administrative Bridge credential |
| `PDF_BRIDGE_QDRANT_TIMEOUT` | `30` | Positive request timeout |

Compose pins server `qdrant/qdrant:v1.18.1`, keeps port 6333 on an internal-only Docker network,
sets `QDRANT__SERVICE__API_KEY`, and enables `QDRANT__SERVICE__JWT_RBAC=true`. The Python dependency
pins `qdrant-client==1.18.0`.

The Bridge admin key can create collections, payload indexes, and aliases and can mutate active and
screening points. Do not distribute it to retrieval. Generate a separate HS256 JWT signed by the
Qdrant admin key with read-only `collection` access for each active physical collection or stable
alias required by retrieval. Do not grant access to `pdf-bridge-screening-v1`; do not use a global
read-only key because it would expose screening.

Self-hosted Qdrant is insecure by default. Outside the reference single-host private network, enable
TLS at Qdrant or a private ingress and restrict network reachability. See the
[Qdrant security guide](https://qdrant.tech/documentation/security/).

## External retrieval proxy

| Variable | Default | Purpose |
|---|---:|---|
| `PDF_BRIDGE_SEARCH_API_URL` | blank | External operator-search endpoint |
| `PDF_BRIDGE_SEARCH_API_TOKEN` | blank | Separate bearer token, required with URL |
| `PDF_BRIDGE_SEARCH_API_TIMEOUT` | `10` | Positive seconds |

These settings do not grant Qdrant access. The retrieval service separately holds its scoped JWT.
Its request/response contract must preserve query, mode, collection groups, `document_id`, and
`collection_key`, and it must query only published points at the current schema version.

## Branding

`PDF_BRIDGE_BRAND_PRIMARY_1`, `PDF_BRIDGE_BRAND_PRIMARY_2`,
`PDF_BRIDGE_BRAND_SECONDARY_1`, and `PDF_BRIDGE_BRAND_SECONDARY_2` require full `#RRGGBB` values.
`PDF_BRIDGE_THEME_DEFAULT` is `system`, `light`, or `dark`.

## Secret handling

- Inject session, Qdrant, embedding, LLM, and retrieval credentials from an approved secret store.
- Generate them independently; never reuse the session or Qdrant administrative key.
- Do not place credentials in URLs, logs, images, manifests, or support bundles.
- Rotate a Qdrant admin key as a coordinated event: all JWTs signed by the previous key become
  invalid and must be regenerated.
- Require TLS whenever a credential crosses a network not confined to the trusted host.

The checked-in `.env.example` is a shape reference only. Populated `.env` files must remain
untracked.
