# ADR 0002: Best-effort real-time lifecycle

Status: Accepted

## Context

Operators expect an upload or delete to take effect without a Jenkins schedule. Parsing, model
inference, and verified Qdrant mutation are too slow and failure-prone to hold an upload request
open. Expected demand is low—approximately five queued documents at peak—so a distributed queue is
not justified.

## Decision

“Real time” means immediate durable asynchronous eligibility, observable progress, and no batch
window; it is best effort, not a fixed latency SLA.

- Upload synchronously performs bounded safety intake, stores the UUID object, commits
  `PREFLIGHTING`, enqueues work, and returns `202`.
- One service process runs two priority worker slots. A process semaphore serializes all local MPNet
  inference to one embedding lane.
- Work is ordered delete first, replacement old-delete second, publication third, and preflight
  fourth. Queued work begins as soon as a slot/dependency is available.
- Streamlit polls API v2 operations and document state; refresh/restart never loses work.
- Completion is explicit: `READY` requires exact Qdrant verification and `DELETED` requires Qdrant
  zero followed by storage purge.

An accepted deletion immediately sets `DELETING`, hides Bridge content, and receives high priority.
The worker deletes and verifies Qdrant points before removing the PDF/artifacts. If Qdrant fails,
content remains retained but inaccessible for retry. If storage fails after point deletion, retry
resumes purge and never republishes.

## Consequences

- Users get prompt acknowledgment and a truthful progress/failure model instead of request
  timeouts.
- A short interval may exist between delete acceptance and external retrieval observing Qdrant
  zero; delete priority minimizes but cannot eliminate it.
- Queue age and phase duration must be monitored. A sustained queue materially above five is a
  capacity signal, not permission to drop or truncate work.
- One process is the supported topology until worker coordination and local-model memory are
  redesigned for horizontal scale.
- Periodic reconciliation is recovery/verification, not a substitute batch ingestion path.

## Rejected alternatives

- **Synchronous end-to-end upload/delete:** couples HTTP timeouts to providers and Qdrant and makes
  recovery ambiguous.
- **Periodic batch polling:** violates immediate eligibility and recreates the retired pipeline.
- **Unlimited concurrency:** risks local model memory exhaustion and makes five-document peak load
  less reliable.

Related contracts: [service](../service-contract.md) and [API v2](../contracts/intake-api.md).
