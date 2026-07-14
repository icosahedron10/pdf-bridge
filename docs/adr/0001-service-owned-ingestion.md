# ADR 0001: Service-owned ingestion

Status: Accepted

## Context

The former design stored uploads and expected Jenkins or another ingestion pipeline to parse and
publish them later. That split could not provide one authoritative lifecycle, immediate visibility,
reliable deletion, or an operator answer to whether a stored PDF was actually searchable.

PDF Bridge must remain a collection-based storage facade, but storage and the derived Qdrant points
must behave as one managed document lifecycle.

## Decision

PDF Bridge owns preflight and direct publication for every accepted PDF. It stores source bytes,
prepares canonical Markdown/chunks/vectors, retains duplicate and LLM checks as preflight, records
approval, writes the approved immutable revision to the configured fixed Qdrant collection, verifies
it, and reports `READY`.

Jenkins, handoff directories, external job claims, completion callbacks, and ingestion reports are
removed from the runtime and public contract. The platform still pre-provisions Qdrant collections;
Bridge is a point writer/deleter, not a collection administrator. External end-user retrieval is a
reader outside this repository.

## Consequences

- One catalog UUID correlates the PDF, prepared revision, Qdrant points, UI status, and audit trail.
- Upload and delete can begin immediately without waiting for a schedule.
- Bridge must own durable operations, idempotent Qdrant mutations, model readiness, retries, and
  catalog/index reconciliation.
- `READY` and `DELETED` become verifiable cross-system outcomes rather than pipeline assertions.
- Deployment must supply vLLM, local model assets, ClamAV, storage, and pre-provisioned compatible
  Qdrant collections.
- A coordinated reset/reingestion is required; no old ingestion state is imported.

## Rejected alternatives

- **Keep Jenkins for publication:** preserves split authority and cannot meet the deletion or
  real-time contract.
- **Store only and let retrieval index opportunistically:** makes indexing and deletion
  non-auditable and leaves the facade unable to report truth.
- **Let Bridge administer Qdrant collections:** exceeds the agreed service boundary and conflicts
  with fixed platform-owned names.

Related contracts: [service](../service-contract.md), [architecture](../architecture.md), and
[Jenkins retirement](../migration/jenkins-retirement.md).
