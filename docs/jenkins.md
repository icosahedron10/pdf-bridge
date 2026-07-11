# Jenkins handoff guide

Jenkins should run the bridge handoff immediately before the existing daily ingestion. The
provided client is intentionally small: `pull` produces a fully verified immutable directory;
`report` validates the pipeline's result document and submits it. It does not invoke the RAG
pipeline itself.

## Agent setup

1. Use a controlled agent with Python 3.12 and network access to the internal bridge URL.
2. Allocate a durable handoff root outside the Jenkins workspace, Git checkout, and OneDrive.
3. Store the bridge job token as a Jenkins secret-text credential.
4. Publish the exact released `pdf-bridge` wheel and its dependencies to an approved internal
   package index. Install an exact version with `--only-binary=:all:`; never install `.` from the
   mutable Jenkins checkout.
5. Keep the bridge URL and its separate allowed-host pin as SCM-reviewed pipeline constants, not
   build parameters. A trigger must not be able to redirect the bearer token.
6. Use an HTTPS URL trusted by the agent. For a private CA, provision a PEM and pass `--ca-bundle`.

The token can claim and download canonical PDFs and mutate lifecycle state. Do not put it in job
parameters, source control, console arguments, or report files. The CLI reads
`PDF_BRIDGE_JOB_TOKEN` or an explicit `--token-file`.

The example starts by deleting and checking out a clean workspace, removes current-run marker/result
files before claiming, and deletes the workspace after archiving. This prevents an old `report.json`
or `pull-result.json` from a prior build/stage retry being submitted. The durable handoff root is
outside the workspace and is intentionally not deleted.

Configure these values as trusted constants in the reviewed Jenkinsfile:

```text
PDF_BRIDGE_URL=https://pdf-bridge.internal
PDF_BRIDGE_JOB_ALLOWED_HOST=pdf-bridge.internal
PDF_BRIDGE_PACKAGE_INDEX=https://python-packages.internal/simple
PDF_BRIDGE_CLIENT_VERSION=0.1.0
```

The package-index URL must not embed credentials; use the agent's approved pip credential/config
mechanism. The example verifies the installed distribution version after installation.

## Pull a batch

```text
pdf-bridge-job pull \
  --base-url https://pdf-bridge.internal \
  --allowed-host pdf-bridge.internal \
  --destination /srv/rag/pdf-bridge-handoff \
  --request-id "$BUILD_TAG" \
  --limit 100 \
  --result-file pull-result.json
```

`request_id` must be 8–128 characters using letters, digits, `.`, `_`, `:`, or `-`. It identifies
the logical handoff, not an individual command attempt. Keep it unchanged when Jenkins retries the
same build; use a new value for the next scheduled build.

`--allowed-host` is required even when `--base-url` comes from `PDF_BRIDGE_URL`. It accepts only a
hostname (no scheme, port, path, credentials, query, or fragment), and the CLI validates the URL
against it before reading/sending the bearer token. Redirect following is disabled. For Jenkins,
both values must be immutable SCM-reviewed constants.

The result file is versioned and contains no credential:

```json
{
  "version": 1,
  "batch_id": "b6eab35c-d552-4894-8d43-2c0b7ef9f513",
  "request_id": "jenkins-pdf-ingest-412",
  "operation_count": 2,
  "batch_directory": "/srv/rag/pdf-bridge-handoff/b6eab35c-d552-4894-8d43-2c0b7ef9f513",
  "manifest_sha256": "7f1b2da0c83f77f832f6126f1ddda5f14f978de09043245e33f63a85c001d45b",
  "idempotent_replay": false
}
```

When there is no work, `operation_count` is zero and the directory/checksum values are null.

## Staged manifest contract

The batch directory is promoted only after every ingest PDF passes byte-count and SHA-256 checks.
Its `manifest.json` is:

```json
{
  "version": 1,
  "batch_id": "b6eab35c-d552-4894-8d43-2c0b7ef9f513",
  "request_id": "jenkins-pdf-ingest-412",
  "claimed_at": "2026-07-10T05:55:00+00:00",
  "lease_expires_at": "2026-07-10T06:25:00+00:00",
  "operations": [
    {
      "operation_id": "dbcc76d3-6338-4f09-9f3a-2b66d8095f82",
      "document_id": "d8fb31ea-bda8-4355-b52e-97c30bcbe35b",
      "operation_type": "INGEST",
      "filename": "Safety handbook.pdf",
      "size_bytes": 348121,
      "sha256": "c5ac957ff6a8adf6bde68b0f2d9858f72091c1a0383ac673be163a7b528af02f",
      "local_path": "files/dbcc76d3-6338-4f09-9f3a-2b66d8095f82.pdf"
    },
    {
      "operation_id": "5e7cfbf5-962b-441d-95c3-f15546c98985",
      "document_id": "0df9842d-7860-4d24-9839-78adf1e6c517",
      "operation_type": "DELETE",
      "filename": "Retired policy.pdf",
      "size_bytes": 92215,
      "sha256": "9d9766d70f6e3f1416bc44bab063b37c552e9a71cf08869f219019de4229af4a",
      "local_path": null
    }
  ]
}
```

Treat `filename` as display metadata. Read the generated `local_path`; never derive paths from the
filename. An `INGEST` item has a file. A `DELETE` item does not: use `document_id` to remove its PDF
source, markdown, BM25 chunks, and dense chunks. Every Qdrant chunk must retain this bridge UUID in
its payload as `document_id`.

Use the batch directory read-only and write markdown/results elsewhere. That preserves the ability
to verify a replay. The pull algorithm is:

1. create `.<batch-id>.tmp-<random>` below the destination;
2. stream each generated operation path with exclusive creation;
3. check exact length and SHA-256 while streaming;
4. durably write the local manifest;
5. atomically rename the temporary directory to `<batch-id>`;
6. acknowledge the complete operation ID set.

An interrupted temporary directory is never considered staged. It can be removed after confirming
that no client process is active. If the final batch directory already exists, `pull` validates its
manifest and every referenced file before re-acknowledging it. Any mismatch stops the job.

## Report contract

The pipeline must emit exactly one result for every manifest operation:

```json
{
  "version": 1,
  "batch_id": "b6eab35c-d552-4894-8d43-2c0b7ef9f513",
  "pipeline_run_id": "rag-nightly-2026-07-10-412",
  "results": [
    {
      "operation_id": "dbcc76d3-6338-4f09-9f3a-2b66d8095f82",
      "success": true,
      "chunk_count": 84,
      "components": {
        "pdf_source": "succeeded",
        "markdown": "succeeded",
        "bm25": "succeeded",
        "dense": "succeeded"
      },
      "error": null
    },
    {
      "operation_id": "5e7cfbf5-962b-441d-95c3-f15546c98985",
      "success": false,
      "chunk_count": null,
      "components": {
        "pdf_source": "succeeded",
        "markdown": "succeeded",
        "bm25": "failed",
        "dense": "succeeded"
      },
      "error": "BM25 removal timed out after the configured retry limit"
    }
  ]
}
```

Component values are `succeeded`, `failed`, or `not_applicable`. A successful operation cannot
contain a failed component. A failed operation requires bounded error text (maximum 4,000
characters) suitable for the coworker's lifecycle view; exclude stack dumps, secrets, paths, and
document contents. Deletion is finalized only when all four components are `succeeded`.

The ingestion wrapper should write a complete report even when one operation fails. It may then
exit nonzero; the example uses Jenkins `catchError` so the report stage still runs while the build
remains failed for operator visibility. A crash before a complete report leaves the batch staged
for controlled retry and must not fabricate success rows.

Submit it with:

```text
pdf-bridge-job report report.json \
  --pull-result pull-result.json \
  --base-url https://pdf-bridge.internal \
  --allowed-host pdf-bridge.internal
```

Before contacting the service, `report` strictly parses both files and requires their `batch_id`
values to match. A no-work pull result cannot authorize a report. The pipeline should write its
report atomically (temporary file followed by same-directory rename) so the reporter never reads a
partially written document.

Reporting is idempotent when the same pipeline run and results are replayed. A conflicting replay
fails. Archive the pull/result JSON as normal Jenkins artifacts, but apply your data-retention rules
because filenames and operational errors may be sensitive.

## Failure and retry behavior

| Failure | Correct action |
|---|---|
| Bridge unavailable before claim | retry the same build/request ID |
| Download interrupted or checksum mismatch | retain evidence, rerun `pull` with the same request ID |
| Lease expires before staging | start a new claim with a new request/build ID after requeue |
| Staging acknowledged but ingestion never ran | rerun the same staged batch; do not make a new claim |
| Some pipeline operations fail | report every result; retry failed items later from the UI |
| Result response is lost | resubmit the identical report |
| Pull/report batch IDs differ | stop; remove stale workspace artifacts and investigate the producer |
| Existing batch verification fails | stop; investigate disk tampering/corruption before changing files |

Set the bridge claim lease above the worst expected download duration, not above the ingestion
duration: the lease protects claiming only until staging acknowledgement. Disable concurrent builds
for a single bridge instance, as shown in [Jenkinsfile.example](../Jenkinsfile.example).
