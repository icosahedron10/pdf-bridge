# Jenkins retirement record

Status: Historical

Jenkins was part of an earlier architecture in which PDF Bridge stored files and an external
pipeline later parsed and indexed them. That boundary is retired. The current service performs
preflight and direct Qdrant point publication itself, as recorded in
[ADR 0001](../adr/0001-service-owned-ingestion.md).

Runtime code and configuration no longer depend on Jenkins. This historical status does not assert
that a particular deployment has disabled its old jobs, revoked credentials, or completed source
reingestion; owners must record those operational actions in the cutover change.

## Superseded concepts

None of the following is a target runtime or compatibility interface:

- scheduled ingestion jobs or Jenkinsfiles;
- handoff, claim, staging, result, quarantine, or completion directories shared with a pipeline;
- ingestion manifests/reports exchanged with a consumer;
- callbacks, polling endpoints, pipeline service tokens, or “awaiting Jenkins” states;
- Jenkins-owned parsing, chunking, embeddings, Qdrant writes, replacement, or deletion;
- operational instructions that ask an operator to reconcile Bridge storage with a Jenkins run.

References to these concepts in Git history describe the retired design and must not be copied into
maintained documentation, configuration, tests, or deployment assets.

## Current replacement

An accepted upload is durably `PREFLIGHTING` and immediately eligible for PDF Bridge's internal
worker. Bridge uses `pypdf`, vLLM Markdown formatting, local MPNet, FastEmbed BM25, preflight review,
and direct writes to fixed pre-provisioned Qdrant collections. `READY` is based on exact point
verification, not an external success report.

Deletion is also service-owned: `DELETING` is queued at high priority, Qdrant active/screening
points are deleted and verified zero, and only then are the source and generated artifacts purged.
There is no Jenkins cleanup job.

## Retirement checklist for cutover

1. Stop and disable every Jenkins job, timer, webhook, and credential associated with PDF Bridge.
2. Prove no job or external process can read old handoff storage or write the fixed Qdrant
   collections.
3. Preserve approved source PDFs for the coordinated reset; do not preserve pipeline state as
   input to the new catalog.
4. Remove handoff volumes, service accounts, secrets, network rules, alerts, dashboards, and backup
   jobs after their retention obligations are satisfied.
5. Search deployed configuration and maintained docs for Jenkins/handoff/callback terms and require
   zero operational references other than this historical record.
6. Complete the [coordinated reingestion](historical-import.md) and verify API v2/Streamlit is the
   only operator path before closing the retirement change.

Git history is the sole archive for the implementation and documentation that preceded this
decision. Do not add a legacy documentation tree or keep retired contracts alongside target docs.
