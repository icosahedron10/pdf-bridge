# Operations runbook

**Status: Current**

This runbook describes the implemented post-refactor service and the operational work required to
release it. It does not claim that a particular environment has completed the coordinated reset,
reingestion, security review, or production acceptance.

PDF Bridge is the storage facade and real-time ingestion owner. It accepts and stores clean PDFs,
prepares canonical Markdown and vectors, runs preflight checks, publishes approved points directly
to fixed Qdrant collections, and removes both points and files on deletion. There is no Jenkins
handoff, batch claim directory, or downstream ingestion report.

## Supported topology

- one Litestar API process and one SQLite catalog;
- the canonical Streamlit operator client, using only the Bridge HTTP API;
- an in-process worker with two execution slots and one serialized local embedding lane;
- opaque UUID PDF and private-artifact storage beneath one configured root;
- ClamAV on a private service network;
- a resource-limited pypdf extraction child using layout mode;
- local <code>sentence-transformers/all-mpnet-base-v2</code> and FastEmbed
  <code>Qdrant/bm25</code>;
- one required private vLLM formatter endpoint and a separately configured advisory LLM endpoint
  serving the classifier and verifier model IDs;
- pre-provisioned, fixed-name active Qdrant collections plus one fixed private screening
  collection;
- an optional external retrieval service reached only through Bridge's operator search proxy.

Run exactly one Uvicorn process. Do not start a second worker, app replica, or process against the
SQLite catalog. The supported capacity is low concurrency with a best-effort peak queue of about
five documents. Two execution slots improve responsiveness, but the service has no per-document
latency SLA and local embedding remains serialized. Keep the Streamlit selection cap at its default
of five unless capacity testing justifies a different value within the supported 1–20 range.

## Real-time service contract

Upload performs bounded streaming, hashing, PDF-shape validation, ClamAV scanning, canonical
promotion, and durable operation creation. It then returns <code>202 Accepted</code>. Work starts
on the next available internal-worker slot without a schedule or manual trigger. Streamlit polls
the document resource and restores open operations after refresh or restart.

“Real time” means immediate best-effort dispatch and visible durable progress, not holding the
upload HTTP request open through parsing, model calls, or Qdrant publication. The same contract
applies to deletion: the request returns after the delete is durable, immediately blocks document
use, and queues a high-priority delete operation.

## Startup and readiness

Use the deployed health endpoints:

~~~bash
curl --fail http://127.0.0.1:8000/api/v2/health/live
curl --fail http://127.0.0.1:8000/api/v2/health/ready
curl --fail http://127.0.0.1:8501/_stcore/health
~~~

Liveness proves only that the process event loop responds. The HTTP readiness probe checks:

1. a catalog query succeeds and the storage, object, artifact, temporary, and quarantine
   directories are writable;
2. ClamAV responds to its bounded probe;
3. the formatter and advisory model-list endpoints report the configured model IDs;
4. the pinned local MPNet model produces finite normalized 768-dimensional output;
5. the exact sparse manifest hash, file inventory, and non-empty `english.txt` asset attest, and the pinned
   FastEmbed BM25 model produces valid document and query vectors;
6. every enabled active Qdrant collection and the screening collection are green and have the
   exact `dense`/`bm25`, Cosine/IDF, and required payload-index schema, with no alias participation.

When `PDF_BRIDGE_WORKER_ENABLED=false`, readiness reports the worker as `DISABLED` and can remain
HTTP-ready for isolated maintenance. That mode cannot process intake and must not be used to reopen
operator traffic.

The entrypoint applies `alembic upgrade head` before Uvicorn starts. Release automation must also
verify the migration head, cache/storage ownership and mount mode, approved dependency/profile
identities, ClamAV signature age, exactly one application process, and positive/negative Qdrant
credential permissions. Those deployment checks are not implied by a `200` readiness response.

Readiness reports the failing component and a content-free reason. It does not download a missing
model, create a Qdrant collection, change an alias, add an index, or silently select another model.

## Document phase health

Streamlit should show the durable phase, attempt count, last transition time, queue age, and a
bounded content-free failure for every open document.

| Phase | Healthy meaning | Stalled or failed meaning |
|---|---|---|
| Accepted / queued | PDF is stored and durable work awaits a slot | Queue age grows while no worker heartbeat is current |
| Parsing | pypdf child is within CPU, memory, page, character, and wall limits | Child exit, timeout, malformed/encrypted/image-only input, or limit violation |
| Formatting | Page batches are completing within vLLM token and time budgets | Missing page, invalid schema/fidelity, timeout, or exhausted bounded retry |
| Preparing index | Markdown chunks and local dense/sparse vectors are being built | Model load, non-finite vector, dimension, chunk, or resource failure |
| Preflight | Active and screening searches plus classifier/verifier checks are running | Deterministic Qdrant search failure or policy-profile mismatch fails preflight; incomplete advisory output enters review |
| Review required | An immutable bundle with candidates or incomplete advisory evidence awaits Keep, Replace, or Cancel | Decision targets a stale revision or unavailable replacement |
| Publishing | The approved bundle is being upserted and counted | Partial write, schema drift, timeout, or count mismatch |
| Ready | Expected points are query-visible and exactly counted | Any catalog/index reconciliation mismatch |
| Deleting | Access is blocked and high-priority Qdrant/storage purge is advancing | Retryable Qdrant or filesystem failure; content must not become active again |

Do not call a document ingested merely because an upsert request returned success. The worker must
verify the exact expected point count and vector/payload schema before committing
<code>READY</code>.

Alert on the oldest queued age, operation age relative to its configured hard timeout, expired
leases, repeated attempts, and counts/age of every failed terminal or retryable state. Also monitor
local model memory and CPU, vLLM latency/invalid-output rates, Qdrant capacity and auth failures,
ClamAV signature age, SQLite/storage free space, and active/screening count reconciliation.

## Upload and preflight investigation

### Synchronous rejection

| Symptom | Check |
|---|---|
| Request too large | Reverse-proxy body limit, Bridge upload limit, and ClamAV stream maximum |
| Invalid PDF | Filename, MIME/signature shape, nonempty bytes, and bounded stream completion |
| Exact same-collection duplicate | Existing content hash and intended logical collection |
| Scanner failure or unclean verdict | Daemon reachability, signatures, timeout, and INSTREAM limit |
| Promotion failure | Storage ownership, free space, and atomic rename support |

Never bypass scanning or place a PDF directly into canonical storage.

### Parsing or formatting failure

The supported source is English native text. Encrypted, malformed, image-only, empty,
text-insufficient, and over-budget PDFs are non-overridable; OCR is out of scope. Obtain a valid
source PDF and resubmit it.

For formatter failures, compare the retained protected request metadata with:

- exact model ID and formatter prompt/schema fingerprint;
- input/output token counts and complete page range;
- vLLM timeout and finish reason;
- strict JSON validation, one-to-one page coverage, and Markdown fidelity results.

An oversized page is divided into stable ordered slices and is not rejected merely for exceeding
one request budget. Confirm slice indices, source hashes, non-overlap, complete coverage, and
deterministic source-order reassembly. Repair a slicing defect rather than raising provider limits
or truncating the page.

Never publish pypdf layout text as a fallback and never hand-edit canonical Markdown. After bounded
retry is exhausted, the document remains failed and non-published until an operator retries after
the dependency or source problem is corrected.

### Preflight failure or review

Screening, duplicate review, classifier, and verifier checks all precede ingestion. Formatter,
local-model, or Qdrant failure is not a clear result and cannot auto-publish a document. A
deterministic active/screening search failure produces `PREFLIGHT_FAILED` and must be retried because
the candidate set is unknown. Invalid, timed-out, or unavailable classifier/verifier output is
retained as incomplete advisory evidence and produces `REVIEW_REQUIRED`; an operator may Keep the
exact revision with the incompleteness visible, Replace one eligible document, Cancel, or delete the
pending document. No failure is converted to an empty candidate result.

Keep publishes the exact approved bundle. Replace names one eligible ready document in the same
logical collection. Cancel purges the unpublished PDF, prepared artifacts, and screening points.
Reject decisions made against a stale preflight revision and reload the evidence in Streamlit.

## Publication recovery

Publication writes the already prepared dense and BM25 vectors to the logical collection's fixed
physical Qdrant name. It stages them as `published=false`/`visibility=publishing`, verifies the
complete staged revision, opens and verifies the active visibility gate, and only then removes
screening points and commits `READY`. Point IDs are deterministic and retries are idempotent.

When publication fails:

1. inspect the operation, durable mutation record, expected point count, and bounded Qdrant error;
2. confirm the configured physical collection still matches named <code>dense</code>
   (768/Cosine) plus named <code>bm25</code> (sparse/IDF) schema;
3. repair authentication, capacity, connectivity, or platform-owned schema drift;
4. retry the retained operation;
5. verify exact point count, both named vectors, current index profile, document ID, collection key,
   and publication payload before accepting <code>READY</code>;
6. verify the document has no remaining screening points.

A Qdrant call may have applied before Bridge recorded completion. Repeating the same deterministic
upsert or filtered delete is expected. Never edit catalog state to pretend a partial write is
complete.

## High-priority deletion

Deletion is Qdrant-first so a failed index removal never destroys the only source needed to
investigate or retry. The request itself is short:

1. validate the document and collection identity;
2. atomically mark it <code>DELETING</code>, block all Bridge content access and active views, and
   create a high-priority durable delete operation;
3. return <code>202 Accepted</code> for Streamlit to poll.

The worker then performs this exact sequence:

1. read the exact active physical name from the successful publication record/deletion checkpoint
   and the fixed screening name from trusted configuration; never re-resolve the active target from
   a changed logical mapping;
2. execute the exact durable phases <code>DELETE_ACTIVE_POINTS</code>,
   <code>VERIFY_ACTIVE_ZERO</code>, <code>DELETE_SCREENING_POINTS</code>, and
   <code>VERIFY_SCREENING_ZERO</code>, waiting for each mutation and failing if an exact count is
   not zero;
3. advance durably to <code>PURGE_STORAGE</code> only after both zero verifications;
4. remove the canonical PDF and every private raw-extraction, Markdown, chunk, vector, prompt, and
   model-output artifact;
5. verify every recorded object path is absent and clear content-bearing SQL rows;
6. commit a content-free <code>DELETED</code> tombstone and operation success.

Filesystem “not found” is idempotent success only after a prior purge attempt/checkpoint proves the
object may already have been removed. A source unexpectedly absent before purge is catalog/storage
drift and fails hard for investigation even though absence can be verified. Permission errors,
unexpected path resolution, residual files, or content-row cleanup failures are explicit retryable
purge failures; they must never be logged and ignored.

Recovery depends on the checkpoint:

- before <code>PURGE_STORAGE</code>, retain all storage and resume the exact Qdrant deletion or
  verification phase;
- at or after <code>PURGE_STORAGE</code>, never republish the document; resume only strict storage
  and SQL purge;
- after complete purge, retries are idempotent and return the existing tombstone.

The document remains inaccessible throughout <code>DELETING</code> or a deletion failure. A Qdrant
outage leaves the PDF retained for retry. A later filesystem outage leaves Qdrant empty and resumes
at the persisted checkpoint. Deleted documents disappear from active collection views but their
content-free tombstones remain available in Streamlit History.

Cancellation of an unpublished document follows the same index-first principle against screening,
then purges the source and prepared bundle. It never creates active points.

## Replacement

Replacement accepts exactly one currently ready document in the same logical collection:

1. finish and freeze the new document's Markdown, chunks, vectors, and successful preflight;
2. mark the old document unavailable and execute its Qdrant-first delete through both zero
   verifications and the durable <code>PURGE_STORAGE</code> phase;
3. strictly purge the old PDF and private artifacts and commit its tombstone;
4. publish and verify the new immutable bundle in the same configured physical collection;
5. remove new screening points and mark the replacement and new document successful.

Old and new points must never overlap. An old-delete failure stops before purge or new publication.
A failure after old points reach verified zero creates an explicit availability gap and retries new
publication; do not restore the old document ad hoc.

## Fixed-name Qdrant drift

Qdrant collections are platform-owned infrastructure. Bridge does not create or repair them.
Readiness fails on a missing/unhealthy configured target, alias participation or an unattestable
alias response, dense size/distance drift, sparse index/IDF drift, missing required payload indexes,
or a screening/active name collision. The Bridge credential therefore needs read-only collection
description and alias metadata in addition to point operations, but no topology mutation.
Deployment and incident reconciliation must additionally fail on:

- unexpected document points, active points in screening, or screening points in active storage.

Stop publication and deletion traffic if identity is ambiguous. Preserve operation IDs and counts,
have the platform owner restore the exact approved schema/name, then rerun readiness and
reconciliation before retrying. Do not point a logical collection at a different physical name as
an incident shortcut.

### Required live Qdrant RBAC gate

Readiness proves that the scoped Bridge JWT is authentic and can inspect every configured
collection, but an explicit negative test is required to prove denied rights. Run this gate from an
approved admin runner that can reach Qdrant after every signing-key/token rotation, collection-map
change, Qdrant upgrade, and before production cutover:

```powershell
$env:RUN_QDRANT_RBAC_LIVE_TEST = "1"
$env:PDF_BRIDGE_QDRANT_URL = "https://qdrant.internal.example:6333"
$env:PDF_BRIDGE_QDRANT_ADMIN_API_KEY = "<read temporarily from the admin secret store>"
$env:PDF_BRIDGE_QDRANT_API_KEY = "<deployed Bridge collection-scoped JWT>"
$env:PDF_BRIDGE_COLLECTIONS = '<the exact deployed JSON collection mapping>'
# Optional for a private CA:
$env:QDRANT_RBAC_TEST_CA_FILE = "C:\approved-ca\qdrant-ca.pem"

python -m pytest -q -m qdrant_rbac tests/integration/test_qdrant_rbac.py
```

The test creates uniquely named disposable collections with the admin credential and removes them
in a `finally` cleanup. It proves the Bridge token can describe one enabled active collection, that
collection listing hides the unrelated collection, and that unrelated metadata access, unrelated
point writes, and collection creation all return `403`. Any other result blocks deployment. Run the
test only against an approved non-user namespace/window, capture its content-free pass/fail result,
then remove the admin key and all test variables from the runner environment. Never inject the
admin key into the Bridge container to run this gate.

## Backups and restore

SQLite, canonical PDFs, and private derived artifacts form one Bridge recovery unit. Take them from
one consistent point, preferably with uploads and the worker drained. Record the application
version, database revision, collection mapping, content/index/policy fingerprints, and object
manifest hashes.

The Qdrant platform owner separately snapshots every fixed active collection and screening. Record
snapshot IDs and collection schemas with the Bridge recovery point. Backups are sensitive copies:
encryption, access control, retention, legal hold, and deletion propagation must be explicit.

A restore drill must prove:

1. catalog rows and recorded object hashes agree;
2. abandoned operations recover from durable leases and checkpoints;
3. all configured Qdrant names and schemas pass readiness;
4. exact active/screening counts reconcile by document ID;
5. local model revisions and vLLM model IDs match the stored profiles;
6. upload, automatic publish, review, replacement, cancellation, download, and high-priority
   deletion smoke tests pass.

Do not restore a content backup over newer deletion tombstones without a reviewed privacy decision
and a purge reconciliation plan.

## Coordinated reset and reingestion

The deployment cutover is a reset, not an in-place migration:

1. inventory and checksum every PDF to preserve, including its intended logical collection;
2. copy sources to approved external preservation storage and verify the copy;
3. stop Streamlit writes, Bridge, all retrieval traffic, and every other Qdrant writer;
4. back up the old SQLite/storage and Qdrant state for the approved retention window;
5. have the Qdrant platform owner delete all old points from the fixed active and screening
   collections without deleting, renaming, or changing their schemas;
6. reset the disposable Bridge SQLite catalog and object/artifact storage;
7. deploy the current release with the fixed collection map, pinned local model cache, and exact
   vLLM model IDs;
8. require full readiness before reopening Streamlit;
9. resubmit preserved PDFs with `pdf-bridge reingest-manifest --apply` so each enters the ordinary
   API v2 formatting, preflight, review, and publication lifecycle;
10. reconcile every source checksum, catalog document, object, Qdrant point count, and collection
    view before declaring cutover complete.

A catalog stamped with the retired `0001_semantic_intake` Alembic revision cannot be
`alembic upgrade`d: that revision was replaced by `0001_target_bridge`, so Alembic will fail with
an unknown-revision error. Step 6 is therefore mandatory — delete and recreate the old SQLite
catalog; never upgrade it in place.

There is no Jenkins compatibility mode, synthetic ingested record, alias migration, plain-text
fallback, or reuse of old incompatible points. Use the exact manifest, resume-state, and rollback
procedure in [coordinated reingestion](migration/historical-import.md).

## Required smoke test

Use disposable English native-text PDFs, including one with a table, in at least two configured
logical collections.

1. Upload a clear PDF; observe <code>202</code>, all durable phases, exact Markdown page coverage,
   and automatic publication.
2. Upload related content; inspect deterministic, classifier, and verifier evidence in Streamlit.
3. Keep it and prove publication reuses the prepared bundle without parsing or embedding again.
4. Cancel another pending document and prove screening, source, and artifacts are gone.
5. Replace an active document and prove zero old/new overlap plus the documented availability gap.
6. Delete an active document, interrupt once before and once after the transition to
   <code>PURGE_STORAGE</code>, and prove both recovery paths converge to zero points and zero files.
7. Verify collection/detail/metadata views and content-free History tombstones.
8. Attempt anonymous Qdrant access, screening access with a retrieval credential, and a collection
   mutation with the Bridge credential; all must fail.

Archive only content-free UUIDs, hashes, timings, versions, fingerprints, checkpoints, schemas, and
results under the release record.

## Incident handling

For malware escape, parser compromise, formatter prompt injection, model/cache compromise,
screening exposure, cross-collection publication, unexpected point overlap, deletion resurrection,
credential disclosure, or catalog/index drift:

1. contain Streamlit mutations, Bridge worker traffic, vLLM, and retrieval without destroying
   evidence;
2. preserve content-free IDs, checkpoints, timestamps, hashes, versions, and protected logs;
3. revoke affected credentials and quarantine inconsistent Qdrant points through the platform
   owner;
4. identify authoritative catalog/object state and the last durable operation checkpoint;
5. repair the dependency or deploy a reviewed migration;
6. rebuild from externally preserved verified sources when integrity is uncertain;
7. complete readiness, reconciliation, and the full smoke test before reopening traffic.

Never manually rewrite lifecycle, decision, checkpoint, or audit rows. Use supported idempotent
operations or a reviewed repair migration that preserves original evidence.
