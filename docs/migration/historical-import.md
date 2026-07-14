# Coordinated reset and source-PDF reingestion

Status: Current procedure

The refactor uses an empty target catalog and cleared fixed Qdrant collections. Historical catalog
rows, prepared artifacts, point IDs, pipeline reports, decisions, and tombstones are not migrated.
Only externally preserved, verified source PDFs and their intended logical collection assignment
are inputs to reingestion.

This is an executable cutover procedure, not a record that cutover has occurred. Keep environment-
specific approvals, timestamps, inventories, and reconciliation evidence in the deployment change.

## Reingestion manifest

The migration client accepts a strict version-4 JSON manifest:

```json
{
  "version": 4,
  "documents": [
    {
      "path": "customer/product-guide.pdf",
      "filename": "Product guide.pdf",
      "collection_key": "customer",
      "sha256": "399a63f4d4d7f2c5f53bde6a6d0c9cf1098f850f614fdf7d79bc13f47ab7e12b"
    }
  ]
}
```

Objects and document entries reject unknown fields. Paths are relative to an operator-supplied
source root; their resolved targets, including symlinks, must remain beneath that root. `filename`
is presentation metadata and cannot contain path separators. Collection keys must exist in the
target configuration. SHA-256 is required and must match before upload. Duplicate manifest paths,
duplicate `(collection_key, sha256)` entries, missing files, invalid PDFs, or an overlap between the
source root and Bridge storage root fail the dry run.

The `pdf-bridge reingest-manifest` command is a thin API v2 client, not a privileged catalog
importer. Dry run validates every entry, calculates a canonical manifest hash, checks collection
availability and API readiness, and writes nothing. Apply calls
`POST /api/v2/collections/{key}/documents` with an idempotency key derived from manifest hash and
entry index, records returned document/operation UUIDs, and limits outstanding nonterminal items to
five. It never writes the database, filesystem object paths, or Qdrant directly.

Run dry validation first, using paths and the private Bridge URL for the deployment:

```powershell
pdf-bridge reingest-manifest .\reingestion-v4.json `
  --source-root C:\approved\source-pdfs `
  --bridge-storage-root C:\pdf-bridge-data `
  --bridge-url https://pdf-bridge.internal `
  --dry-run
```

Apply the identical manifest and keep its content-free resume state outside Bridge storage:

```powershell
pdf-bridge reingest-manifest .\reingestion-v4.json `
  --source-root C:\approved\source-pdfs `
  --bridge-storage-root C:\pdf-bridge-data `
  --bridge-url https://pdf-bridge.internal `
  --state .\reingestion-v4.state.json `
  --wait-seconds 300 `
  --apply
```

Use `--ca-bundle` for a private CA. In trusted-header mode, run the client only from an approved
trusted peer and supply the deployment's `--identity-header` and `--identity` values. Never weaken
TLS verification or make the application trust an unapproved migration host.

## Pre-cutover preparation

1. Inventory every source PDF that should survive and assign exactly one target logical collection.
2. Copy the sources to an approved immutable location outside all Bridge/Jenkins/Qdrant storage;
   record size and SHA-256 and build the strict manifest.
3. Run the released migration client's dry run against the exact target collection configuration.
   Resolve all path, hash, duplicate, collection, and media errors before downtime.
4. Back up the old environment as one rollback unit and record its application/database/Qdrant
   versions. The backup is rollback evidence only and is never imported into the target.
5. Record the expected PDF count and byte total per logical collection plus the canonical manifest
   hash; obtain cutover approval.

## Cutover

1. Stop Streamlit, API traffic, Jenkins, imports, external retrieval, Bridge workers, and every
   Qdrant writer. Prove no process retains write access.
2. Verify the preserved source set still matches the approved manifest.
3. Have the platform team clear all points from every fixed target active collection and the fixed
   private screening collection, then validate the required `dense`/`bm25` schema and indexes.
   Bridge must not delete/recreate collections or aliases.
4. Wipe the disposable Bridge catalog, UUID source storage, generated artifacts, operation state,
   and old migration state. Do not seed terminal rows or reuse old UUIDs.
5. Deploy the target schema and one Bridge process. Require readiness for storage, ClamAV, vLLM,
   local MPNet/FastEmbed assets, and every fixed Qdrant collection.
6. Start the target Streamlit UI and run manifest apply. Keep at most five documents outstanding;
   uploads still enter the ordinary `PREFLIGHTING` lifecycle.
7. Resolve each `REVIEW_REQUIRED` item in Streamlit. Reingestion does not inherit old Keep/Replace
   decisions. Investigate and retry explicit failures; never mark rows ready manually.
8. Wait until every manifest entry is `READY`, `REJECTED`, `CANCELLED`, or an accepted documented
   exception. Reconcile source hash, prepared revision, chunk count, exact active point count, and
   zero screening points for every `READY` document.
9. Run same-collection duplicate, cross-collection isolation, table/Markdown inspection, restart,
   and high-priority delete smoke tests. Confirm API v1, Jinja routes, and Jenkins access are absent.
10. Reopen operator traffic. External retrieval may be re-enabled separately after its owner
    validates the new fixed collections; its rollout is outside PDF Bridge.

## Failure and rollback

Before target traffic opens, rollback means stop the target completely and restore the old
environment as its entire isolated backup unit. Never point old code at partially populated target
collections or merge target rows/points into old state.

After target operator writes are accepted, prefer fixing forward. If rollback is mandatory, first
stop all target writers, preserve new source PDFs separately, restore the old unit in isolation, and
schedule another full reset/reingestion that includes the newly preserved sources. There is no
dual-write reconciliation procedure.

A failed apply is resumed with the unchanged manifest and idempotency keys. The client reads its
recorded UUIDs, skips entries already durably accepted, and resubmits only requests whose acceptance
is unknown. Lifecycle failures are retried through API v2, not by rerunning a new manifest or
editing state.

## Completion evidence

Retain the manifest/hash, preserved-source inventory, migration client version, target release and
profile IDs, API result UUIDs, per-collection catalog/point reconciliation, review dispositions,
smoke-test results, and cutover approval under the normal audit policy. Do not retain duplicate
source text, Markdown, vectors, prompts, or raw model output in the migration report.

Once cutover and rollback retention close, this document may be relabeled Historical. Git remains
the only source for superseded import procedures; no legacy-doc archive is maintained.
