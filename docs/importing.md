# Historical import

Historical import is a controlled way to submit externally preserved PDFs through the normal
semantic-intake lifecycle. Manifest version 3 does not mark documents ingested and does not move an
old Qdrant corpus. Every applied item is copied, hashed, PDF-validated, ClamAV-scanned, promoted,
registered as `ANALYZING`, and given a normal durable `ANALYZE` operation.

## Manifest version 3

```json
{
  "version": 3,
  "documents": [
    {
      "path": "customer/product-guide.pdf",
      "filename": "Product guide.pdf",
      "collection_key": "customer"
    },
    {
      "path": "internal/benefits.pdf",
      "collection_key": "internal"
    }
  ]
}
```

The root object and every document object are strict; unknown fields fail validation. `filename` is
optional and defaults to the source basename. `collection_key` must exist in the deployed
`PDF_BRIDGE_COLLECTIONS` configuration.

The source root is a security boundary. Each relative path is resolved, including symlinks, and
must remain beneath that root. The source root and Bridge storage root may not contain one another.
Duplicate source paths and duplicate bytes within the same manifest collection are rejected.

## Dry run, then apply

Run the exact released application and configuration intended for apply mode:

```bash
pdf-bridge import-manifest historical-v3.json \
  --source-root /approved/source-pdfs \
  --dry-run \
  --actor-id change-1234
```

Dry run still reads, bounds, hashes, validates, and scans every PDF. It does not create catalog rows
or canonical objects. Review the JSON result, source-set checksum, manifest checksum, collection
mapping, ClamAV signature age, and operator/change identifier.

Apply only the reviewed bytes and manifest:

```bash
pdf-bridge import-manifest historical-v3.json \
  --source-root /approved/source-pdfs \
  --apply \
  --actor-id change-1234
```

Apply is one catalog transaction. Canonical promotion is compensated if processing or commit fails.
After success, start or wake the application worker and follow the imported rows through analysis,
review, and publication. An import count is not proof that documents are retrieval-active.

## Idempotency and reruns

The manifest is not a reusable idempotency envelope. A successful apply creates ordinary upload
identities. Rerunning unchanged content in the same collection is rejected by the exact-byte gate;
the same bytes in another collection remain allowed by design.

If a partial operational failure is reported, inspect the catalog and canonical storage before
creating a smaller reviewed manifest containing only items that were not committed. Do not edit
SQLite state, synthesize ingested rows, or copy files directly into canonical object paths.

## After import

1. Poll `GET /api/v1/uploads?open=true` until every imported analysis reaches review, publication,
   rejection, or an explicit retryable failure.
2. Review every candidate and incomplete analysis through the same Keep/Replace/Cancel interface as
   browser uploads.
3. Verify active Qdrant point counts and payload schema for each published UUID.
4. Search each collection positively and verify a negative cross-collection query.
5. Archive the manifest hash, source-set hash, result JSON, application version, pipeline
   fingerprint, and change identifier under the approved retention policy.

## Backup distinction

A version-3 import manifest is not a Bridge backup. It cannot reconstruct decisions, analysis
evidence, audit history, replacement workflows, leases, outbox state, or tombstones. Back up SQLite,
canonical storage, and private analysis storage as one consistent unit, and manage Qdrant snapshots
according to the recovery plan.
