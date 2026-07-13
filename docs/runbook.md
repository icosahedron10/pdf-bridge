# Operations runbook

This runbook covers routine operation, recovery, and the coordinated disposable-POC reset. Commands
assume the service environment and storage paths have been reviewed against
[`configuration.md`](configuration.md).

## Daily checks

Check the process and dependency endpoints:

```bash
curl --fail https://pdf-bridge.internal/api/v1/health/live
curl --fail https://pdf-bridge.internal/api/v1/health/ready
```

Readiness verifies catalog access, every required storage directory (the storage root plus
`objects/`, `temporary/`, and `quarantine/`), and ClamAV. Configured collections and catalog
invariants are validated at startup against the same database the application serves. Treat a
readiness failure as an operational fault; do not route new uploads or job traffic until the failed
dependency is understood.

Monitor at least:

- upload rejection, malware scan errors, and canonical promotion failures;
- queued age, claim lease expiry, staging failures, and report rejection;
- ingestion and deletion failure counts by collection;
- retrieval correlation failures and downstream latency;
- canonical, handoff, database, and Qdrant capacity.

The library collection cards show available and processing counts. The queue is the single operator
workspace for pending and failed work.

## Upload investigation

### Upload is rejected

Use the problem response code and request ID to correlate logs.

| Symptom | Check |
|---|---|
| Request too large | Reverse-proxy and bridge upload limits |
| Invalid PDF | Signature, extension, MIME shape, and parser-independent structural checks |
| Malware or scanner error | ClamAV daemon health, signature age, socket permissions, and scan limits |
| Duplicate response | Existing active document UUID and collection returned by the API |
| Promotion failure | Quarantine and canonical filesystem ownership, free space, and atomic rename support |

Do not copy quarantined bytes into canonical storage manually. Correct the dependency and upload the
source again through the bridge.

### Collection is rejected

The upload must name a configured collection key. Compare the submitted key with
`PDF_BRIDGE_COLLECTIONS`. Collection assignment is immutable, so moving a document means deleting
and reingesting it under the intended collection.

## Queue and batch recovery

### Work remains queued

1. Confirm a Jenkins schedule is running and can reach `/api/v1/jobs/*`.
2. Check the job-token credential and exact allowed host.
3. Inspect the archived `pull-result.json` for zero work, batch ID, and request ID.
4. Check for a prior active lease. Let the configured lease expire or recover it through normal job
   execution; do not edit queue rows.

### Claim or staging fails

Re-run the same Jenkins build with the same request ID after fixing the cause. The client creates an
immutable batch directory and verifies every ingest PDF against manifest size and SHA-256 before
acknowledging staging. A partially downloaded directory is not valid input to the parser.

Common causes are canonical storage unavailability, expired lease, insufficient handoff capacity,
permission errors, or an old client that does not implement the current version 2 shape.

### Report is rejected

Validate the archived file against the current report contract:

- version is 2 and `batch_id` matches the pull summary;
- there is exactly one unique result per staged operation;
- each result uses `success`, the four component values, optional `chunk_count`, and optional
  `error` only;
- successful results have all components succeeded and no error;
- failed results contain a nonblank error.

Unknown or obsolete fields make the strict report invalid. Fix and redeploy the producer, then run a
fresh operation or replay a byte-for-byte valid report as appropriate.

### Lease expires

Expired claimed or staged work is recovered by the bridge lifecycle rules. Confirm the original job
is no longer running before allowing a new claim. Repeated expiry usually means the lease is shorter
than worst-case verified staging and pipeline execution, or the consumer is abandoning batches.

Adjust the lease only after measuring actual duration; extending it also delays recovery from a dead
consumer.

## Ingestion failures

Any condition that prevents all four components from succeeding enters `INGEST_FAILED`. This
includes encrypted content, required OCR that the pipeline cannot provide, no extractable text,
parser crashes, downstream timeouts, and index write failures.

From the queue:

1. Open the document ledger and read the operation error and component rows.
2. Fix the parser, source, storage, or index dependency.
3. Retry through the normal ingest action.
4. Confirm the new operation reaches `INGESTED` and search correlation succeeds.

Do not write catalog state directly or report success for a partial pipeline. If one component
failed, the pipeline should compensate partial downstream writes or safely overwrite them on retry.

## Deletion failures

Deletion starts only from an ingested document. A downstream failure enters `DELETE_FAILED` and is
retryable through the normal lifecycle.

Verify deletion by UUID and collection across:

- downstream PDF source at `pdfs/{collection_key}/{document_id}.pdf`;
- Markdown storage;
- BM25 content;
- dense/Qdrant points;
- bridge canonical storage after downstream success;
- subsequent collection-scoped search.

Targets already absent should be treated as successful downstream deletion so replay is idempotent.
If downstream deletion succeeded but canonical cleanup failed, restore bridge storage access and
replay the same report. The catalog intentionally stays in cleanup until canonical removal commits.

## Retrieval correlation failures

PDF Bridge validates the complete grouped response before returning any hit. A single invalid hit
rejects the response.

Check that:

- query, mode, requested collection set, and page boundaries match the request;
- every hit has a unique bridge UUID;
- each Qdrant payload contains the matching `document_id` and `collection_key`;
- the UUID exists in the bridge, belongs to the group collection, and is in a shared eligible state;
- a reported group total does not exceed the eligible bridge population.

Unknown, inactive, duplicate, cross-collection, missing-group, pagination, or impossible-total data
is a downstream integrity incident. Do not relax bridge validation to make a response pass. Correct
the index or retrieval adapter and replay the request.

## Startup catalog validation

Startup fails if any document, including a tombstone, lacks a collection. It also fails if an active
document references a collection that is no longer configured.

Do not rename configuration to bypass the guard. For an accidental configuration omission, restore
the key. For a deliberate collection retirement, drain and delete its active documents first, then
deploy the configuration change. Historical tombstones retain their original collection key.

## Backups and restore

Back up the catalog and canonical storage as one recovery unit. Record the database revision and
application version with each backup. The downstream corpus can be rebuilt, but retain source PDFs
until rebuild is proven.

A restore drill should verify:

1. catalog revision and startup invariants;
2. canonical object existence and checksum for a sample of active documents;
3. queue and batch leases are not mistakenly resumed from a stale environment;
4. downstream rebuild preserves bridge UUID and collection;
5. collection-scoped search and normal delete succeed for a sample item.

Never merge independently timed database and canonical-storage snapshots without a reconciliation
plan.

## Routine upgrades

For ordinary releases:

1. Back up catalog and canonical storage.
2. Stop bridge and job writers.
3. Apply migrations once.
4. Deploy all coordinated contract participants.
5. Start PDF Bridge and confirm readiness.
6. Start Jenkins consumers and run a small ingest/search/delete smoke test.
7. Resume operator traffic.

Migration `0002_collection_partitioning` is exceptional: it only upgrades an empty version-1
catalog. Its reset-required failure is deliberate and must not be bypassed or edited in place.

## Atomic POC reset

Use this sequence for the collection-only version 2 cutover:

1. Inventory and checksum every source PDF that must be reingested. Confirm the source set is
   readable and mapped to configured collections.
2. Stop operator traffic, bridge writers, scheduled Jenkins jobs, running pipeline jobs, handoff
   consumers, and retrieval/index writers.
3. Confirm no job consumer remains active and archive any evidence needed for incident history.
4. Clear the disposable catalog and its migration state, bridge canonical storage, downstream
   handoff/source/Markdown/BM25 data, and the entire Qdrant corpus.
5. Deploy the rewritten migration and bridge, current `pdf-bridge-job` client, parser/RAG report
   producer, retrieval adapter, UI, and documentation as one release.
6. Initialize the empty catalog, start dependencies, and confirm readiness before enabling traffic.
7. Reingest the source set under `pdfs/{collection_key}/{document_id}.pdf` paths.
8. Reconcile available catalog UUIDs and collections with downstream storage, BM25, and Qdrant.
9. Run the downstream smoke test below, then resume normal scheduling and operator traffic.

Do not preserve old catalog rows or old version 2 artifacts as live inputs. There is no compatibility
shim, and mixed participants can corrupt placement or leave partial indexes.

## Required downstream smoke test

Choose a disposable source PDF with searchable text and a configured collection.

1. Upload it and record its bridge UUID and collection.
2. Claim and stage the ingest operation; assert the exact collection-only handoff path.
3. Report a complete successful ingest and inspect Qdrant to confirm its points contain only the
   bridge identity fields required for correlation: `document_id` and `collection_key`.
4. Search through PDF Bridge and confirm the UUID appears in the correct collection.
5. Delete through PDF Bridge, process the delete batch, and report complete success.
6. Confirm source/Markdown/BM25/Qdrant data are absent and the UUID no longer appears in search.

Archive the manifest, report, search response, and deletion evidence with the release record.

## Incident handling

For possible malware escape, cross-collection exposure, unknown retrieval UUIDs, forged job
traffic, or unexplained catalog/index drift:

1. Stop uploads, job consumers, and retrieval traffic as needed to contain impact.
2. Preserve request IDs, batch artifacts, audit events, logs, and relevant hashes.
3. Revoke or rotate affected credentials.
4. Identify the authoritative bridge UUID/collection set and quarantine inconsistent downstream
   data.
5. Correct the root cause and rebuild the affected corpus from verified source PDFs.
6. Complete the ingest/search/delete smoke test before reopening traffic.
