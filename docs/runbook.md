# Operations and troubleshooting

This runbook assumes the Docker Compose POC on Linux. Direct installations are also Linux-only and
use `systemd` for process supervision; translate the Compose operations to the corresponding
`systemctl` and `journalctl` commands for that installation.

## Normal start and health

```text
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 app clamav
```

The health endpoints have deliberately different meanings:

- `GET /api/v1/health/live`: the application process can answer HTTP. Use for restart/liveness.
- `GET /api/v1/health/ready`: required local dependencies, including database/storage/scanner, are
  usable. Use before admitting traffic or running Jenkins.
- `GET /api/v1/health/dependencies`: detailed dependency state for operators; restrict it to the
  internal network.

Do not restart on every transient retrieval outage: search can be unavailable while uploads and
queue transparency remain useful. Surface that dependency failure to the user.

## First ClamAV start is slow

The pinned image initializes/updates the named signature volume and FreshClam may need several
minutes. Compose gives the health check a three-minute start period, but a slow or proxied network
can take longer.

```text
docker compose logs -f clamav
docker compose exec clamav clamdscan --ping=10
```

If updates fail, check DNS, outbound HTTPS/proxy rules, time synchronization, volume capacity, and
the FreshClam message. Do not bypass the dependency. Restart after fixing the cause and wait for a
healthy state.

## Common failures

### Upload says scanner unavailable

1. Check `docker compose ps` and ClamAV logs.
2. Confirm the app uses host `clamav`, port `3310`, and both services share the Compose network.
3. Check memory pressure; signature loading may require more than 2 GiB.
4. Check signature update state.
5. Retry the upload only after readiness is healthy. Failed temporary uploads should have been
   removed; investigate accumulating files under `temporary/` before deleting them.

### Jenkins gets 401

Confirm Jenkins and the service use the same current `PDF_BRIDGE_JOB_TOKEN`. Inspect credential
binding without printing the secret. Check that the Authorization header is not stripped by the
reverse proxy. A rotation must update both ends in one change window.

### Jenkins checksum or existing-batch verification fails

Stop the job. Record the batch/operation IDs and preserve the batch directory. Compare the server
manifest, disk/volume health, proxy behavior, and any security event. Do not edit `manifest.json`,
rename files, or pass a flag to skip verification. After the cause is resolved, remove only a known
incomplete temporary sibling and rerun with the same request ID.

### Claim lease expired

A claim is expected to be staged within `PDF_BRIDGE_CLAIM_LEASE_MINUTES`; ingestion can take longer
after staging. Check Jenkins/download performance, increase the setting within the 1–1440 minute
range if justified, and let the bridge requeue expired claims. Do not create overlapping manual
batches. An expired request ID is intentionally retired; the next claim must use a new Jenkins
request ID (normally a new build ID).

### SQLite is locked or readiness fails

Verify exactly one application process/container is running and Uvicorn has `--workers 1`. Look
for an admin import or unsupported process holding a long transaction. Check disk space and I/O.
Do not delete SQLite `-wal` or `-shm` files from a live database. Repeated contention is a signal to
move to managed PostgreSQL, not to weaken transaction boundaries.

### Library search fails

Check the configured retrieval URL/token, TLS chain, timeout, and retrieval service logs. Validate
that every chunk payload contains the bridge UUID, collection key, and language; that the response
contains exactly one group for every requested collection; and that exact totals include zeroes.
Root search is count-only across collections, while a collection page is hit-producing and scoped
to one collection. A forged cross-collection hit, wrong-language hit, inactive ID, missing group, or
total above the eligible catalog population produces a 502 and no partial results. The bridge
intentionally will not substitute metadata search.

### Startup rejects collection configuration

Validate that `PDF_BRIDGE_COLLECTIONS` is nonempty JSON, every key is unique lowercase path-safe
ASCII, and every object has a display name, description, and `customer` or `internal` audience.
The key must match both the Qdrant collection name and chatbot-manager `allowed_collections` value.

Startup also fails when an active catalog document references a removed/renamed key or is
unassigned outside classification review. Do not rename configuration to get past the guard. Stop
traffic and follow the collection/language maintenance cutover with a backup and reviewed catalog
reconciliation.

### Documents need language review

`review_required` is a safe content outcome, not an operational parser failure. The pipeline must
not write BM25 or dense content for that document. In **Needs review**, inspect the bounded reason
(`no_text`, `ocr_required`, `encrypted`, `bilingual`, `unsupported`, or `low_confidence`). For a
pipeline-undetermined document, record an audited `en` or `fr` override with a nonblank reason or
request removal; its collection remains locked and automatic detection is not repeated.

Legacy unassigned rows may instead choose a collection and queue their first classification pass,
or receive an override with the same explicit collection assignment. A document already assigned
to a collection cannot be moved; delete it completely and re-upload to the correct destination.
Parser crashes, timeouts, and service outages belong in ingestion failure/retry, not Needs Review.

### A deletion is stuck or failed

Use the document ledger and pipeline run ID to identify which of `pdf_source`, `markdown`, `bm25`,
or `dense` failed. Repair that component, then use the UI retry action. Do not mark the catalog
deleted manually; canonical bytes are retained until a complete success report.

If the status is **Deletion cleanup** or **Cancellation cleanup**, downstream/catalog work already
finished but the canonical unlink did not. Check volume availability, permissions, and disk errors;
then replay the identical result report or use **Retry cleanup**. The retained storage key makes
that retry safe after a crash.

## Safe restart and upgrade

1. Avoid the Jenkins claim window and wait for active uploads to finish.
2. Back up the bridge volume.
3. Read application, dependency, Python-base, and ClamAV release notes.
4. Build and test the exact image revisions in a non-production environment.
5. Review database migrations. The single-process Compose entrypoint runs `alembic upgrade head`
   before Uvicorn and fails startup if it cannot migrate; enterprise rollout should run migrations
   once as a separately controlled deployment step before scaling application replicas.
6. Start one app process, wait for readiness, then run upload/claim/search smoke tests.
7. Keep the prior image and compatible backup until acceptance is complete.

Do not use a moving `latest` image in a controlled environment. Compose pins the ClamAV patch and
Python patch so upgrades are explicit.

## Collection/language maintenance cutover

Introducing or changing corpus partitioning is a maintenance migration, not a rolling UI update.
Use one approved change window for PDF Bridge, Jenkins, the parser/RAG store, retrieval service,
Qdrant, and the chatbot manager:

1. Publish the final `PDF_BRIDGE_COLLECTIONS` registry and map each stable key to the identically
   named Qdrant collection and chatbot-manager allowlist value. Record the customer/internal owner
   and approved placement rules.
2. Put chatbots and PDF Bridge uploads into maintenance mode. Stop Jenkins scheduling, then drain
   every version 1 claimed/staged batch and confirm no old result can arrive after the contract
   switch.
3. Stop the app and take a verified backup of the complete bridge volume plus approved Qdrant and
   downstream-corpus snapshots. Record counts and checksums by intended collection.
4. Deploy the catalog migration and required collection configuration. Legacy active documents are
   held unassigned in **Needs review** as `und`; deleted/cancelled tombstones remain historical.
   Do not bypass this hold by editing catalog state.
5. Using scoped, reviewed Qdrant and filesystem procedures, clear/recreate each configured Qdrant
   collection and the derived RAG PDF/markdown/chunk outputs. Verify the exact target roots before
   deletion; never recursively remove a computed or unreviewed path. Canonical bridge `objects/`
   remain intact.
6. Deploy the version 2 Jenkins manifest/result client, existing-parser language classification,
   `pdfs/{language}/{collection_key}/{document_id}.pdf` routing, and grouped retrieval contract.
   Keep chatbots unavailable while indexes are empty or partially rebuilt.
7. Resolve every review row: assign its immutable collection and request parser detection, or make
   an evidence-backed audited English/French override. Use version 2 historical import only after
   an external record is already rebuilt and its collection/language placement is independently
   attested; import does not enqueue missing index work.
8. Run Jenkins until every approved document is classified `en`/`fr` and rebuilt into exactly one
   Qdrant collection. `und` or review-required documents must have no BM25/dense entries.
9. Reconcile PDF Bridge available/processing/review and language counts against the downstream PDF
   tree and Qdrant payload counts for every collection. Investigate every unknown key, duplicate
   UUID, missing document, and stale cross-collection payload.
10. Run the isolation acceptance test: an HR-only topic returns internal `1` and customer `0`; a
    customer-product topic appears only in customer; entering customer sends a customer-only hit
    request; and a deliberately forged internal hit in a customer response yields a visible 502
    with no partial results.
11. Verify the chatbot manager derives its server-side `allowed_collections` from the authenticated
    user and intersects every requested list before retrieval. Restore chatbots and uploads only
    after owners sign off on counts, negative tests, and rollback evidence.

If acceptance fails, keep traffic paused. Restore the coordinated catalog/canonical/Qdrant snapshot
set or correct and repeat the rebuild; do not mix a pre-cutover catalog with post-cutover indexes.

## Backup

The complete business dataset is the `bridge_data` volume: SQLite (including any WAL state) plus
canonical objects. ClamAV signatures can be recreated and need not be in the business backup.

For a simple consistent POC backup:

1. Stop Jenkins scheduling and wait until no upload/import is active.
2. Stop only the app: `docker compose stop app`.
3. Snapshot or archive the entire `bridge_data` volume using the organization's approved volume
   backup mechanism into encrypted storage.
4. Record application version, backup time, volume name, and checksum/inventory.
5. Restart and confirm readiness: `docker compose start app`.

Do not copy only `catalog.sqlite3` while the app is live, and do not omit canonical `objects/`.
Test restoration periodically into an isolated network: restore the whole volume, start the same
app version, verify catalog/object checksums and representative previews, then exercise migration
to the target version. A backup that has not been restored is unproven.

## Retention and cleanup

- Browser-cancelled files and fully acknowledged deletions remove canonical PDF bytes; audit
  tombstones remain according to governance policy.
- Jenkins batch directories are copies, not the canonical store. Remove them only after the result
  was accepted and the pipeline's retention/forensic window elapsed.
- Temporary directories named `.<batch-id>.tmp-*` indicate an interrupted pull. Confirm no job is
  using them and retain evidence for checksum incidents before cleanup.
- Never recursively delete a computed storage path. Resolve and verify the exact volume/batch root
  before any cleanup command, including on Jenkins Linux agents.

## Minimal daily checks

- App and ClamAV readiness are healthy.
- FreshClam is updating and signatures are within organizational age limits.
- Volume capacity has room for uploads and temporary copies.
- The last Jenkins build reported every operation; no claimed/staged batch is unexpectedly old.
- Collection available/processing/review and `en`/`fr`/`und` counts reconcile with downstream
  storage and Qdrant; no review/`und` document is indexed.
- Retrieval returns expected bridge UUIDs only from their configured collection, and the HR-topic
  negative test still reports customer `0`.
- Backups and credential rotations are within policy.
