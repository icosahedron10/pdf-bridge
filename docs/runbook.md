# Operations runbook

This runbook covers the single-process semantic-intake POC. PDF Bridge owns parsing and indexing;
there is no scheduler, batch claim, handoff directory, or external ingestion report to recover.

## Supported topology

- exactly one Uvicorn application process;
- one SQLite catalog beneath the writable Bridge storage root;
- a lifespan-owned worker with two execution slots and per-collection locks;
- ClamAV reachable only on the private service network;
- Qdrant server `1.18.1` with authentication and JWT RBAC;
- configured private embedding and LLM endpoints;
- an external retrieval service holding only active-collection read JWTs.

Do not increase `--workers`, start a second app replica, or run a second worker against the same
catalog. That breaks process-local collection serialization and the supported epoch model.

## Startup and health

Container startup validates required secrets and storage, applies `alembic upgrade head`, and then
executes one Uvicorn process. Check:

```bash
docker compose ps
curl --fail http://127.0.0.1:8000/api/v1/health/live
curl --fail http://127.0.0.1:8000/api/v1/health/ready
```

Readiness covers dependencies that must serve request traffic. Semantic provider outages are also
visible on analysis resources as explicit incomplete reasons; do not interpret a reachable process
as proof that publication can finish.

At startup, verify:

- exactly one app container/process is running;
- the database revision is `0001_semantic_intake` on the reset catalog;
- storage directories are writable by the non-root app user;
- ClamAV is healthy and signatures are current;
- the Qdrant version is exactly `1.18.1`, API-key requests work, and anonymous requests fail;
- JWT RBAC is enabled and retrieval cannot list or query `pdf-bridge-screening-v1`;
- embedding model ID/dimension and both LLM model IDs match the approved pipeline fingerprint.

## Routine monitoring

Alert on:

- oldest `QUEUED` operation age and expired `RUNNING` leases;
- repeated worker lease recovery or heartbeat failures;
- counts and age of `INGEST_FAILED`, `REPLACE_FAILED`, `DELETE_FAILED`, and `CLEANUP_FAILED` rows;
- pending index-outbox age, attempts, and exact point-count mismatches;
- analysis incompleteness by provider and collection;
- parser rejection/timeout/resource-limit rates;
- ClamAV signature age and protocol failures;
- Qdrant disk capacity, rejected authentication, collection/alias drift, and active/screening counts;
- embedding/LLM latency, invalid structured output, and quote-validation failure;
- retrieval responses rejected for unknown, inactive, or cross-collection UUIDs;
- SQLite, canonical-object, private-analysis, and Qdrant backup capacity.

Use document, operation, analysis, replacement, and request UUIDs for correlation. Logs and alerts
must not contain PDF excerpts, prompts, vectors, raw model output, credentials, or full local paths.

## Upload investigation

### Synchronous rejection

| Problem | Check |
|---|---|
| Request too large | Proxy body limit, `MAX_UPLOAD_BYTES`, and clamd stream maximum |
| Invalid file | `.pdf` name, MIME shape, leading signature, nonempty bytes |
| Exact duplicate | Existing UUID in the same collection; cross-collection copies are allowed |
| Scanner unavailable/unclean | Daemon health, signatures, timeout, and INSTREAM limit |
| Promotion failure | Storage ownership, free space, and atomic rename support |

Never bypass the scanner or place a file directly in canonical storage. Correct the cause and submit
through the normal upload or import path.

### Parser rejection

Encrypted, malformed, image-only, insufficient-text, and over-budget PDFs are non-overridable.
Confirm the analysis moves through cleanup to `REJECTED`, canonical bytes are absent, screening
points count zero, and only content-free audit metadata remains. OCR is out of scope; obtain a clean
text-bearing source rather than changing state manually.

Parser CPU/address-space limits apply on Linux. A parser child killed by a resource limit is treated
as a hostile/malformed input. The subprocess is containment, not a complete sandbox; investigate
unexpected crashes as security-relevant events.

## Worker recovery

Operations are `ANALYZE`, `INGEST`, `DELETE`, or `CLEANUP` and move through `QUEUED`, `RUNNING`,
`SUCCEEDED`, `FAILED`, or `CANCELLED`. The worker heartbeats its leases. After an unclean process
exit, restart the same single process and allow expired `RUNNING` operations to return to the queue.

Do not edit lease timestamps. Before retrying manually:

1. confirm the previous process/thread is gone;
2. identify the last durable phase and pending outbox entry;
3. repair the dependency or storage fault;
4. use `POST /api/v1/uploads/{id}/retry` for an exposed retryable failure;
5. verify idempotent point counts and final catalog state.

A Qdrant call may have succeeded before Bridge recorded the outbox entry done. Repeating a mutation
is expected: point IDs are deterministic, deletes are filter-based, and exact counts decide success.

## Analysis and review failures

An embedding, Qdrant, or classifier outage must produce `REVIEW_REQUIRED` with an explicit
incomplete reason; it must not be reported as a clear analysis. Reviewers may Keep advisory findings.
That immutable decision remains valid while publication waits and retries, so do not ask for a
second semantic decision solely because a provider was down.

If the decision endpoint rejects a stale revision or collection epoch, reload the analysis and
review the current evidence. Never replay a target from an obsolete page.

For repeated invalid LLM output, inspect only approved protected artifacts. Confirm the schema,
model ID, temperature zero, absent tools, and quote references. Invalid or inconclusive output is
advisory; deterministic candidates remain visible.

## Ingestion failure

`INGEST_FAILED` means source and analysis are retained but active publication did not complete.

1. Check the operation error and pending outbox attempts.
2. Verify the configured embedding dimension matches Qdrant collection vectors.
3. Repair provider authentication, connectivity, model availability, alias, or capacity.
4. Retry the upload.
5. Confirm both named vectors exist for every point, exact active count equals chunk count,
   `published=true`, schema version matches, and screening count is zero.

Do not mark a document `INGESTED` based on a partial dense-only or sparse-only write.

## Replacement failure

Use the replacement state and operation phase to determine whether the old document was deleted.

- `PREPARING`: the old document must still be active; repair new-vector generation.
- `DELETING_OLD`: no new active write is permitted. Repair old-point deletion or verification.
- `INGESTING_NEW`: the old document is intentionally deleted and availability is reduced. Repair
  new publication and retry; do not restore the old document ad hoc.

For every replacement incident, prove call ordering from audit/outbox records: new artifacts ready,
old active delete applied and counted zero, old artifacts purged, then new active upsert. Any
old/new overlap is a retrieval integrity incident.

## Cancellation and deletion

Cancel removes unpublished source bytes, analysis artifacts, and screening points. Deletion first
removes and verifies active points, then purges source and analysis content and leaves a `DELETED`
tombstone. A cleanup failure retains an explicit retryable state rather than silently abandoning
content.

After retry, verify by UUID across:

- the active physical collection and stable alias;
- `pdf-bridge-screening-v1`;
- canonical object storage;
- private compressed analysis storage;
- SQL analysis/chunk/finding rows;
- external keyword, semantic, and hybrid search.

Audit history should retain the canonical manifest hash and metadata but no excerpts, prompts,
vectors, or raw outputs.

## Retrieval integrity

The external service must implement:

- keyword mode with `content_bm25`;
- semantic mode with `content_dense`;
- hybrid mode with reciprocal rank fusion;
- filters for `published=true` and the current `schema_version`;
- Bridge `document_id` and `collection_key` in every hit.

Bridge rejects a response containing unknown, duplicate, inactive, pending, tombstoned, or
cross-collection UUIDs, or an impossible group total. Treat that as catalog/index drift. Do not
relax validation. Remove or rebuild the inconsistent points, verify aliases and epochs, and rerun
positive and negative collection searches.

## Backups and restore

Back up SQLite, canonical PDFs, and compressed analysis storage as one recovery unit. Record the
application version, migration revision, and pipeline fingerprint. Back up Qdrant with its supported
snapshot procedure and store the active alias/epoch map. Preserve source PDFs outside Bridge until
restore and reindex are proven.

A restore drill must verify:

1. storage hashes and catalog rows agree;
2. no stale `RUNNING` operation is concurrently owned by another process;
3. pending outbox work reconciles idempotently;
4. active/screening point counts and payload schemas agree with SQL;
5. retrieval credentials cannot access screening;
6. upload, review, replacement, search, cancellation, and deletion smoke tests pass.

## Routine upgrade

1. Back up the complete recovery unit and Qdrant.
2. Stop operator and retrieval traffic.
3. Stop the single Bridge process and confirm no parser child remains.
4. Update the Qdrant client before or with a compatible server change; never float image tags.
5. Apply reviewed migrations once.
6. Deploy Bridge and external retrieval together when index schema or pipeline fingerprints change.
7. Start dependencies, then the single Bridge process.
8. Verify authentication, aliases, worker recovery, and the smoke test before reopening traffic.

## Coordinated semantic-intake reset

This release is an empty-only POC reset, not an in-place migration.

1. Inventory, checksum, and externally preserve every source PDF and its intended collection.
2. Stop operator traffic, Bridge, retrieval, imports, and every index writer.
3. Confirm no parser, worker, or retrieval process remains active.
4. Archive only approved evidence needed for audit; old live catalog/index artifacts are not inputs.
5. Wipe the disposable SQLite catalog and migration state, Bridge canonical/private-analysis
   storage, historical handoff storage, and every old active and screening Qdrant collection.
6. Deploy the new empty-only migration, Bridge, Qdrant `1.18.1`, and external retrieval together.
7. Configure the Bridge admin key and issue retrieval collection-scoped read JWTs that exclude
   screening.
8. Start dependencies and one Bridge process; verify readiness and security gates.
9. Reingest preserved PDFs through manifest version 3 or normal upload, including ordinary review.
10. Record the evaluation dataset hash and parser/model/threshold fingerprints; require candidate
    recall of at least `0.98` before leaving POC mode.
11. Reconcile SQL, storage, active/screening Qdrant counts, aliases, and retrieval results.

There is no dual API, Jenkins compatibility mode, synthesized ingested row, or safe way to preserve
old live state across this cutover.

## Required smoke test

Use disposable searchable PDFs in at least two configured collections.

1. Upload a clear PDF and confirm automatic publication.
2. Upload related content and inspect paginated deterministic and LLM evidence.
3. Exercise Keep and confirm publication without another decision after a simulated provider retry.
4. Exercise Cancel and prove complete purge.
5. Exercise Replace and prove zero old/new overlap plus the documented availability gap.
6. Search keyword, semantic, and hybrid modes; verify positive same-collection and negative
   cross-collection behavior.
7. Delete the active test document and prove both Qdrant index families and private artifacts are
   empty for its UUID.
8. Attempt anonymous Qdrant access and a retrieval-token screening query; both must fail.

Archive content-free results, hashes, UUIDs, versions, and fingerprints under the release record.

## Incident handling

For malware escape, parser compromise, screening exposure, old/new overlap, forged provider output,
credential disclosure, or unexplained catalog/index drift:

1. contain uploads, worker, provider, and retrieval traffic without destroying evidence;
2. preserve request/document/analysis/operation/outbox IDs, timestamps, hashes, and protected logs;
3. rotate affected provider or Qdrant credentials (and regenerate JWTs after an admin-key change);
4. identify the authoritative SQL UUID/collection set and quarantine inconsistent points;
5. correct the root cause and rebuild from verified external source PDFs;
6. complete the full smoke test before reopening traffic.

Do not manually rewrite lifecycle or audit rows. Use documented operations or a reviewed repair
migration that preserves the original evidence.
