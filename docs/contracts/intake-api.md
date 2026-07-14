# Intake API v2 contract

Status: Current

API v2 is the sole interface for Streamlit and approved administrative clients. It is a JSON/HTTP
API under `/api/v2`; the source endpoint returns PDF bytes while Markdown remains a structured JSON
resource with page provenance. API v1 is absent—there is no compatibility adapter, dual write, or
grace period.

## General rules

- UUIDs are lowercase canonical strings. Timestamps are UTC RFC 3339 values.
- JSON request and response objects are strict: unknown request fields are rejected.
- Every mutation requires authentication, CSRF protection for browser sessions, and an
  `Idempotency-Key` header. Reuse with a different request returns `409`.
- `POST` upload, decision, retry, and `DELETE` return `202 Accepted` with the current document and
  operation UUID. Processing never holds an HTTP request open for model or Qdrant work.
- Collection keys are configuration-owned. Unknown or disabled keys return `404`; clients cannot
  submit a Qdrant collection name.
- Metadata is immutable. There are no `PATCH` endpoints and no collection-management endpoints.
- Lists use opaque `cursor` pagination with `limit` from 1 through 100. Documents, audit events, and
  history use newest timestamp then UUID; collections retain configuration order and chunks retain
  ascending chunk order. Clients must not decode or synthesize cursors.

Errors use one shape and never expose source text, prompts, credentials, filesystem paths, or raw
provider output:

```json
{
  "error": {
    "code": "document_state_conflict",
    "message": "The document cannot be deleted from its current state.",
    "request_id": "2fd5c8b8-2ed2-4d50-bec4-160bc0b26d89",
    "retryable": false
  }
}
```

## Resources and endpoints

Except where shown otherwise, paths in this table are relative to `/api/v2`.

| Method and path | Result |
|---|---|
| `GET /health/live` | Process liveness only |
| `GET /health/ready` | Catalog, storage, ClamAV, and enabled-worker formatter/advisory, local-model, and Qdrant checks |
| `GET /collections` | Configured collection display metadata and state counts |
| `GET /collections/{key}` | One collection and its fixed Qdrant mapping/status |
| `GET /collections/{key}/documents` | Current documents in the collection, filterable by `state` |
| `POST /collections/{key}/name-check` | Filename-only upload advisory; never semantic preflight |
| `POST /collections/{key}/documents` | Stream one multipart PDF to UUID storage and start preflight |
| `GET /documents/{id}` | Immutable metadata, lifecycle, prepared revision summary, and allowed actions |
| `GET /documents/{id}/source` | Stored `application/pdf` while content access is allowed |
| `GET /documents/{id}/markdown` | Canonical prepared Markdown with page provenance |
| `GET /documents/{id}/chunks` | Paged chunk metadata and Markdown text |
| `GET /documents/{id}/preflight` | Current prepared revision, findings, candidates, and completeness |
| `POST /documents/{id}/decision` | Keep, Replace, or Cancel the named prepared revision |
| `POST /documents/{id}/retry` | Queue the eligible failed phase without changing approval |
| `DELETE /documents/{id}` | Set `DELETING`, block reads, and queue high-priority verified deletion |
| `GET /documents/{id}/events` | Content-free ordered audit events |
| `GET /operations/{id}` | Queue position, phase, attempts, and sanitized failure |
| `GET /operations/metrics` | Content-free queue counts, oldest age, and durable phase buckets |
| `GET /history` | Terminal tombstones, filterable by collection and disposition |
| `POST /operator/search` | Optional operator-only proxy to the configured external retrieval service |

There is no Bridge end-user search endpoint in v2. The operator proxy performs no ranking or
end-user authorization of its own; those behaviors remain outside the service.

## Filename advisory and operator search

`POST /api/v2/collections/{key}/name-check` accepts bounded filenames and reports exact-name or
filename-family matches already cataloged in that logical collection. It reads no PDF bytes and is
only an early Streamlit warning. It is deliberately not named preflight: semantic screening,
duplicate review, and LLM classification still run after durable upload and before publication.
Skipping the advisory never skips preflight.

`POST /api/v2/operator/search` accepts one configured logical collection, a bounded query, mode,
and result limit. When the optional retrieval integration is configured, Bridge forwards a strict
request, validates that every returned document is `READY` in the requested collection, and returns
the correlated bounded result. Streamlit never receives the upstream credential. If the integration
is not configured or is unavailable, the endpoint returns a sanitized `503`; Bridge does not fall
back to screening points, direct Qdrant search, or local ranking.

## Upload

`POST /api/v2/collections/{key}/documents` accepts exactly one multipart part named `file` with a
`.pdf` filename. The API bounds the stream, computes SHA-256, validates PDF shape, scans it, and
promotes it atomically before committing the document. English/native-text eligibility is decided
asynchronously during preflight.

```json
{
  "document": {
    "id": "b7926e86-0efd-4c80-ae6f-12bd4d2bb2c9",
    "collection_key": "customer",
    "original_filename": "guide.pdf",
    "content_type": "application/pdf",
    "size_bytes": 184220,
    "sha256": "399a63f4d4d7f2c5f53bde6a6d0c9cf1098f850f614fdf7d79bc13f47ab7e12b",
    "created_by": "anonymous:7c61ed927a",
    "state": "PREFLIGHTING",
    "created_at": "2026-07-13T18:30:00Z",
    "updated_at": "2026-07-13T18:30:00Z",
    "ready_at": null,
    "failure": null,
    "allowed_actions": []
  },
  "operation": {
    "id": "d2c4f6a4-bb50-4f2f-8e8f-74b79a54e8f7",
    "operation_type": "PREFLIGHT",
    "state": "QUEUED",
    "phase": "QUEUED",
    "priority": "NORMAL",
    "attempt": 1,
    "retryable": true,
    "created_at": "2026-07-13T18:30:00Z",
    "updated_at": "2026-07-13T18:30:00Z",
    "completed_at": null
  },
  "idempotent_replay": false
}
```

An exact-byte match to retained content in the same logical collection returns `409` with the
existing document UUID. The same bytes in a different collection are permitted. Scanner failure is
`503` and fails closed; invalid/oversized media is rejected before durable intake.

## Document representation

`GET /documents/{id}` returns immutable source metadata and current processing facts:

- UUID, collection key, original filename, MIME type, byte size, SHA-256, creator and
  intake time;
- `state`, current operation/phase, sanitized failure, retryability, and allowed actions;
- page count, language/text eligibility, prepared revision UUID, content/index/policy profile IDs,
  formatter model, dense model/dimension, BM25 configuration, fixed Qdrant target, Markdown hash,
  chunk count, expected/verified active point count, verification time, and timestamps;
- review summary, replacement linkage, deletion progress, and audit/tombstone disposition.

The state enum is exactly:

| State | Meaning |
|---|---|
| `PREFLIGHTING` | Queued or running before active ingestion |
| `PREFLIGHT_FAILED` | Retryable extraction, formatting, embedding, or deterministic screening failure |
| `REVIEW_REQUIRED` | Prepared revision has candidates or incomplete classifier/verifier evidence and requires Keep, Replace, or Cancel |
| `PUBLISHING` | Approved immutable revision is being written and verified |
| `PUBLISH_FAILED` | Publication did not reach verified completeness; revision retained |
| `READY` | Exact expected active point set is verified |
| `DELETING` | Reads blocked; high-priority point/file deletion is running |
| `DELETE_FAILED` | Reads remain blocked; deletion resumes from its durable checkpoint |
| `REJECTED` | Unsupported/hard-failed input was purged; tombstone only |
| `CANCELLED` | Unpublished content was purged by decision; tombstone only |
| `DELETED` | Qdrant zero and content purge verified; tombstone only |

`source` is available after safe promotion except in `DELETING`, `DELETE_FAILED`, or terminal
content-free states. `markdown` and `chunks` return `409 artifact_not_ready` until a complete
prepared revision exists and return `410 content_purged` after content is inaccessible or purged.
Chunk responses include index, page range, heading path, token count, text hash, Markdown, and
prepared revision UUID; they never expose dense or sparse numeric vector values.

## Preflight and decisions

`GET /documents/{id}/preflight` returns the revision UUID, completeness, profile identifiers,
ordered deterministic findings, candidate documents, bounded evidence, independent LLM
classifier/verifier results, and formatting/chunk/point summaries. Provider prompts and raw output
remain UUID-addressed protected revision artifacts and are never returned or logged; the public
catalog view exposes only validated evidence and content-free hashes/diagnostics.

The decision request is strict:

```json
{
  "prepared_revision_id": "e44ada81-45f5-4dde-9cbc-e6b91a43da54",
  "action": "KEEP",
  "target_document_id": null
}
```

- `KEEP` requires no target and queues publication of that exact revision.
- `REPLACE` requires exactly one `READY` target in the same collection. The target cannot be the
  incoming document and cannot already participate in another mutation.
- `CANCEL` requires no target and queues purge; it never publishes.

A stale revision, changed candidate eligibility, or incompatible state returns `409`. Decisions are
immutable and idempotent. Retry of `PUBLISH_FAILED` reuses the decision and prepared revision; it
does not request another semantic review. Retry of `PREFLIGHT_FAILED` starts a new prepared revision
attempt because the failed revision is never promoted in place.

A failed replacement has one durable owner: the incoming document's `PUBLISH` operation. The old
target can simultaneously report `DELETE_FAILED`; retrying either document resumes that same
replacement operation and never creates an unbound `DELETE` operation.

## Deletion

`DELETE /documents/{id}` is valid for `READY` and for eligible unpublished states. For `READY`, the
accept transaction immediately changes state to `DELETING`, removes the item from ordinary active
lists, blocks source/Markdown/chunk reads, and returns a `DELETE` operation with `HIGH` priority.

Deletion records the exact active Qdrant collection persisted by the successful publication and
retains it in every checkpoint; retries never re-resolve a possibly changed collection mapping.
Operation phases are `DELETE_ACTIVE_POINTS`, `VERIFY_ACTIVE_ZERO`,
`DELETE_SCREENING_POINTS`, `VERIFY_SCREENING_ZERO`, `PURGE_STORAGE`, and `COMMIT_TOMBSTONE`. The API
reports the last durable phase so retry never guesses. `DELETE_FAILED` before active-zero retains
files; after active-zero it resumes purge only. A repeated request for `DELETING`, `DELETE_FAILED`,
or `DELETED` returns the existing operation/disposition and performs no contradictory action.

## Response codes

- `200`: successful read.
- `202`: accepted durable asynchronous mutation.
- `400`: malformed multipart or invalid parameters.
- `401`/`403`: authentication, CSRF, or authorization failure.
- `404`: unknown configured collection or document not visible to the caller.
- `405`: the API route exists but does not support the requested method.
- `409`: idempotency conflict, exact duplicate, stale revision, invalid lifecycle action, or
  artifact not ready.
- `410`: content was intentionally purged.
- `413`/`415`: upload size or media type rejected.
- `422`: strict request schema failure.
- `500`: an internal consistency or service failure was sanitized.
- `502`: a configured dependency returned an invalid response.
- `503`: a dependency needed to accept or safely process work is unavailable.

See [the service contract](../service-contract.md) and
[the chunk/Qdrant contract](chunks-qdrant.md).
