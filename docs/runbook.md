# Operations and troubleshooting

This runbook assumes the Docker Compose POC. For a direct installation, use the equivalent process,
volume, and service-manager commands.

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
that every chunk payload contains the bridge UUID as `document_id` and that response IDs exist in
the catalog. The bridge intentionally will not substitute metadata search.

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
  before any cleanup command, especially on Windows agents.

## Minimal daily checks

- App and ClamAV readiness are healthy.
- FreshClam is updating and signatures are within organizational age limits.
- Volume capacity has room for uploads and temporary copies.
- The last Jenkins build reported every operation; no claimed/staged batch is unexpectedly old.
- Retrieval search returns expected bridge document IDs.
- Backups and credential rotations are within policy.
