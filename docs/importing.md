# Historical import and backup handoff

`pdf-bridge import-manifest` is a local administrative command for registering PDFs that were
already ingested before the bridge existed. It uses the same filename, PDF signature, size,
checksum, exact-duplicate, and ClamAV controls as a new upload. It creates each document directly in
the `INGESTED` state plus a synthetic successful ingestion operation for lifecycle transparency; it
does not enqueue work for Jenkins.

Run it only on the bridge host/container with access to the catalog and canonical storage. This is
not a browser feature and does not accept a remote URL.

## Prepare a source root

Place or mount the approved historical PDFs under one explicit read-only source root, outside the
bridge storage root and outside the Git checkout. The command never modifies or moves source files;
it copies clean bytes into UUID-derived canonical storage.

The source root is a security boundary. Each manifest entry is resolved, including symlinks, and
must remain beneath the resolved root. Relative paths are strongly preferred because they make the
manifest reviewable and portable. A symlink pointing outside the root is rejected.

## Version 1 manifest

```json
{
  "version": 1,
  "documents": [
    {
      "path": "policies/remote-work.pdf",
      "filename": "Remote work policy.pdf",
      "ingested_at": "2026-06-18T13:05:00Z",
      "chunk_count": 47,
      "pipeline_run_id": "legacy-import-2026-06"
    },
    {
      "path": "handbooks/safety.pdf",
      "filename": null,
      "ingested_at": null,
      "chunk_count": null,
      "pipeline_run_id": null
    }
  ]
}
```

Fields:

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | must be `1` |
| `documents` | yes | 1–10,000 entries; duplicate source paths are rejected |
| `path` | yes | source path resolved under `--source-root` |
| `filename` | no | display filename; defaults to the source basename and must end in `.pdf` |
| `ingested_at` | no | known UTC-aware historical ingestion time; otherwise import time |
| `chunk_count` | no | known nonnegative downstream chunk count |
| `pipeline_run_id` | no | bounded identifier that can be correlated with pipeline records |

Use JSON `null` or omit an unknown optional field. Do not invent metadata merely to avoid an
“information unavailable” label in the document ledger.

## Validate first

Set normal bridge environment variables, including the external storage root and clamd address,
then run:

```text
pdf-bridge import-manifest historical.json \
  --source-root /mnt/approved-historical-pdfs \
  --dry-run
```

Dry-run is the default. It validates the strict manifest and resolved paths, streams each PDF
through size/signature/hash checks, scans it with ClamAV, and checks active exact duplicates. It
does not create document/audit rows or retain canonical copies. Scanner failure fails the run.

Review the JSON result: each item contains the effective filename, SHA-256, byte count, and a null
`document_id`. Record the manifest checksum and dry-run output with the approved change ticket.

## Apply once

After review, run the same package/configuration/manifest with explicit `--apply`:

```text
pdf-bridge import-manifest historical.json \
  --source-root /mnt/approved-historical-pdfs \
  --apply \
  --actor-id change-CHG001234
```

`--actor-id` identifies the controlled import in append-only audit events; it is not an identity
override for browser activity. Avoid personal data or secrets in it.

For each entry, apply mode:

1. resolves the path beneath the source root;
2. copies it to bridge temporary storage while enforcing the configured maximum and PDF signature;
3. calculates SHA-256 and rejects an active duplicate;
4. streams the copy to ClamAV and requires a clean result;
5. atomically promotes the clean copy to canonical storage;
6. creates an ingested document, a synthetic `SUCCEEDED` ingestion operation, and an audit event in
   one controlled transaction.

The command fails hard on the first invalid item and rolls back its catalog transaction. Because a
database transaction and filesystem promotion cannot form one distributed transaction, an abrupt
process/host failure still requires inspection of the catalog, temporary directory, and canonical
objects. Do not modify the manifest and blindly rerun: use a reviewed manifest containing only
confirmed missing documents.

## Import acceptance checks

- The reported `imported` count equals the approved manifest count.
- Every returned document UUID opens the expected ledger and clean preview.
- SHA-256 and size agree with the source inventory.
- Historical times/counts/run IDs appear when supplied; unknown values are labeled unavailable.
- Retrieval search returns these bridge UUIDs. If the existing Qdrant payloads lack them, update or
  reingest those chunks before declaring search integration complete.
- Source files remain unchanged.

## Backups versus imports

An import manifest is **not** a bridge backup. It cannot reconstruct queue/batch/audit history and
does not preserve canonical UUIDs unless the retrieval corpus has already been coordinated.
Back up/restore the whole bridge data volume as described in [the runbook](runbook.md). Use import
only for the one-time transition of a pre-bridge corpus or a separately approved catalog repair.
