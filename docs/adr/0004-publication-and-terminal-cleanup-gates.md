# ADR 0004: Publication visibility and terminal cleanup gates

Status: Accepted

## Context

Prepared content, Qdrant mutations, source-file cleanup, and operator decisions cannot share one
transaction. The lifecycle therefore needs durable states that remain truthful across crashes and
retries. In particular, terminal labels cannot be exposed while content still exists, and partially
written active points cannot become retrieval-visible.

## Decision

1. `REJECTED`, `CANCELLED`, and `DELETED` mean content-free. Cleanup first sets `DELETING` and
   records the intended `terminal_disposition`; the terminal state is committed only after active
   and screening point counts are both verified zero and source/generated content is purged.
2. Publication first writes the exact sealed revision to the fixed active collection with
   `published=false` and `visibility=publishing`. Bridge verifies deterministic IDs, revision
   payloads, both named vectors, and the exact document point count before atomically opening the
   payload visibility gate. It then removes and verifies screening points before committing
   `READY`.
3. A Replace decision creates one replacement-priority incoming `PUBLISH` operation. That operation
   owns the old document's exact-target deletion checkpoint and cannot publish the incoming
   revision until the old document has a content-free `DELETED` tombstone.
4. `clear_for_publication` is derived exactly: deterministic discovery and advisory checks are
   complete, there are no candidates, and there are no incomplete reasons. Any candidate or
   incomplete advisory evidence produces `REVIEW_REQUIRED`.
5. A preflight retry creates a new monotonic prepared revision and reruns preparation from the
   retained source. Failed revisions remain immutable failure evidence; the new operation retains
   the failed phase and attempt lineage for operator diagnostics. Publication retry, by contrast,
   always reconstructs points from the same approved sealed revision.
6. Direct deletion is accepted from `READY` and stable unpublished failure/review states. Repeated
   requests during deletion or after tombstoning return the existing durable operation and never
   create a contradictory disposition.

## Consequences

- Qdrant or filesystem outages leave an explicit retryable checkpoint without exposing content.
- External retrieval never sees a partial prepared revision through the required publication
  filter.
- Terminal history is honest and content-free, while audit, decision, manifest, publication, and
  tombstone headers retain correlation.
- Retrying failed preparation costs recomputation but avoids mutating or guessing the completeness
  of a partial revision.

Related contracts: [service lifecycle](../service-contract.md),
[API v2](../contracts/intake-api.md), and [chunks/Qdrant](../contracts/chunks-qdrant.md).
