# Jenkins and downstream pipeline integration

Jenkins is the only supported batch consumer. Use the released `pdf-bridge-job` client from the
pipeline repository rather than reproducing claim, download, checksum, staging, or report logic in
Groovy. The client validates the strict version 2 contracts before changing catalog state.

`Jenkinsfile.example` is a starting point. Review its trusted constants and credential IDs before
copying it into the pipeline repository.

## Required configuration

The Jenkins agent needs Python 3.12, durable handoff storage, access to PDF Bridge, and a short-lived
or centrally rotated job token.

| Setting | Purpose |
|---|---|
| `PDF_BRIDGE_URL` | Base URL of PDF Bridge |
| `PDF_BRIDGE_JOB_ALLOWED_HOST` | Exact host allowed to receive the bearer token |
| `PDF_BRIDGE_JOB_TOKEN` | Jenkins secret-text credential exposed only around client calls |
| `HANDOFF_ROOT` | Durable root for immutable batch directories |
| `INGEST_COMMAND` | Parser/RAG entry point that reads a bridge manifest and writes a report |
| `PDF_BRIDGE_CLIENT_VERSION` | Exact released client version installed by the job |

Use HTTPS and keep certificate verification enabled. For an internal CA, pass `--ca-bundle`.
`--allow-http` and `--insecure-skip-tls-verify` exist only for local diagnosis and should not appear
in production pipeline code.

## End-to-end job sequence

1. Install an exact `pdf-bridge` wheel from an approved package index.
2. Run `pdf-bridge-job pull` with the stable Jenkins build ID as `--request-id`.
3. If `operation_count` is zero, finish without invoking the pipeline.
4. Give the emitted batch `manifest.json` to the parser/RAG pipeline.
5. Require the pipeline to write a nonempty version 2 report file.
6. Run `pdf-bridge-job report` with that report and the original pull summary.
7. Archive the pull summary and report, then remove the Jenkins workspace. Keep the durable handoff
   directory according to the approved retention policy.

The client performs this API sequence:

```text
POST /api/v1/jobs/batches/claim
GET  /api/v1/jobs/batches/{batch_id}/manifest
GET  /api/v1/jobs/batches/{batch_id}/operations/{operation_id}/content  # ingest only
POST /api/v1/jobs/batches/{batch_id}/staged
POST /api/v1/jobs/batches/{batch_id}/results
```

Claim and report requests are idempotent. Reuse the same request ID when retrying the same Jenkins
run. Do not generate a fresh ID merely because a network response was lost.

## Pull and staging

Example:

```bash
pdf-bridge-job pull \
  --allowed-host pdf-bridge.internal \
  --destination /srv/rag/pdf-bridge-handoff \
  --request-id "$BUILD_TAG" \
  --result-file pull-result.json
```

The client creates an immutable directory named for the batch, writes `manifest.json` atomically,
downloads each ingest PDF, verifies byte size and SHA-256, and acknowledges staging only after every
operation is durable. Any mismatch fails the pull and leaves the batch unreported.

The local pull summary is a client file with version 1. It correlates the later report to the batch
and records the manifest hash; it is not the pipeline manifest.

## Version 2 manifest

The manifest contains one item per operation:

```json
{
  "version": 2,
  "batch_id": "6c519b68-5fc4-42ea-a388-106ac88841bd",
  "request_id": "jenkins-pdf-bridge-1842",
  "state": "CLAIMED",
  "claimed_at": "2026-07-12T15:30:00Z",
  "lease_expires_at": "2026-07-12T16:00:00Z",
  "operations": [
    {
      "operation_id": "3e07f69c-4c54-4a8f-a649-72b7ab6db2c3",
      "document_id": "aa1327a6-68ec-4268-a538-c0f54d48d474",
      "operation_type": "INGEST",
      "filename": "handbook.pdf",
      "size_bytes": 48122,
      "sha256": "b05d9a41fd15b6a3e8f0ac22a130a2bb9cf7118f28ea759d4f31235a36f61d7a",
      "collection_key": "employee-handbook",
      "relative_path": "pdfs/employee-handbook/aa1327a6-68ec-4268-a538-c0f54d48d474.pdf",
      "download_url": "/api/v1/jobs/batches/6c519b68-5fc4-42ea-a388-106ac88841bd/operations/3e07f69c-4c54-4a8f-a649-72b7ab6db2c3/content"
    }
  ]
}
```

Every item has exactly these fields: operation and document IDs, operation type, filename, size,
checksum, collection key, exact relative path, and optional download URL. Unknown fields are
rejected.

The only valid handoff path is:

```text
pdfs/{collection_key}/{document_id}.pdf
```

The client rejects absolute paths, traversal, noncanonical separators, unexpected suffixes, or any
path whose collection or UUID disagrees with the manifest item. An ingest has a download URL. A
delete may omit it; downstream deletion uses the supplied UUID and collection to remove PDF source,
Markdown, BM25, and dense/Qdrant data.

## Downstream processing rules

For an ingest operation, the pipeline must:

1. Parse the staged PDF and produce the durable downstream PDF source and Markdown.
2. Write BM25 and dense/Qdrant content under the supplied collection and bridge UUID.
3. Put `document_id` and `collection_key` in every Qdrant payload used by retrieval correlation.
4. Report all four components as succeeded only after their writes are durable.

The pipeline must not infer collection placement from a filename or directory outside the manifest.
Encrypted PDFs, OCR-only PDFs, empty text, parser errors, timeouts, and any failed component are
ordinary ingestion failures. Do not partially advertise such a document as searchable.

For a delete operation, remove all four downstream components using the supplied identity. Report
success only after deletion is complete or the targets are already absent, so replay remains safe.

## Version 2 report file

The pipeline writes one result for every staged operation:

```json
{
  "version": 2,
  "batch_id": "6c519b68-5fc4-42ea-a388-106ac88841bd",
  "pipeline_run_id": "rag-ingest-1842",
  "results": [
    {
      "operation_id": "3e07f69c-4c54-4a8f-a649-72b7ab6db2c3",
      "success": true,
      "chunk_count": 17,
      "components": {
        "pdf_source": "succeeded",
        "markdown": "succeeded",
        "bm25": "succeeded",
        "dense": "succeeded"
      }
    }
  ]
}
```

A failed result is explicit:

```json
{
  "operation_id": "3e07f69c-4c54-4a8f-a649-72b7ab6db2c3",
  "success": false,
  "components": {
    "pdf_source": "succeeded",
    "markdown": "failed",
    "bm25": "not_applicable",
    "dense": "not_applicable"
  },
  "error": "PDF contains no extractable text"
}
```

Component values are `succeeded`, `failed`, or `not_applicable`. The report validator enforces:

- a successful result has all four components `succeeded` and no error;
- a failed result has a nonblank error;
- operation IDs are unique and match the staged batch exactly;
- the report has no unknown fields.

`chunk_count` is optional and nonnegative. Use it for a successful ingest when available; it is not
a substitute for component confirmation.

Submit with:

```bash
pdf-bridge-job report report.json \
  --pull-result pull-result.json \
  --allowed-host pdf-bridge.internal
```

The batch response aggregates only successful and failed operations.

## Failure and replay behavior

| Condition | Required action |
|---|---|
| Queue empty | Finish successfully without a report |
| Download, size, checksum, or durable-write failure | Fail before staging; retry the same request ID |
| Lease expires before staging | Start a new scheduled claim after the bridge recovers the lease |
| Parser or component failure | Report `success: false` with a nonblank error; operator may retry ingestion |
| Report response is lost | Replay the same report against the same pull summary |
| Canonical cleanup fails after downstream delete | Replay the same report after storage recovers |
| Contract validation fails | Fix the pipeline/report producer; do not edit the archived artifact by hand |

Never manufacture a successful report to drain the queue. A failed operation remains visible and
retryable, while a malformed report is rejected without partially applying results.

## Security and observability

- Scope the job token to `/api/v1/jobs/*` and store it only in Jenkins credentials.
- Pin the bridge client exactly and install only from a trusted internal index.
- Restrict the allowed host before attaching the bearer token.
- Keep manifest, pull summary, and report artifacts for correlation, but do not archive downloaded
  PDFs in the Jenkins workspace.
- Alert on repeated lease expiry, checksum mismatch, report rejection, and component failure.
- Do not log bearer tokens, download responses, or source PDF contents.

## Coordinated deployment and smoke test

The bridge, client, parser/RAG pipeline, and retrieval implementation must deploy together. Stop old
consumers before clearing the disposable catalog, canonical and handoff storage, and Qdrant corpus.
After reingestion, select one item and prove all of the following:

1. Its downstream PDF is at `pdfs/{collection_key}/{document_id}.pdf`.
2. Its Qdrant payload contains matching `document_id` and `collection_key`.
3. Collection-scoped search returns it through the bridge.
4. Normal bridge deletion removes it from downstream storage, BM25, Qdrant, and subsequent search.
