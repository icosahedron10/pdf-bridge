# Refactor gap

Status: Historical

This file records the implementation delta that existed before the refactor was completed. It is
not an alternate product contract and must not be used to infer the current runtime. Current
behavior is defined by the [service contract](service-contract.md); the completed sequence is
retained in the [implemented refactor plan](refactor-plan.md).

## Historical current-to-target delta

| Area | Transitional implementation | Required target |
|---|---|---|
| Service boundary | Bridge owns much of analysis/indexing but retains concepts from the prior semantic-intake design | Bridge is explicitly the storage facade and sole ingestion owner; no Jenkins/external pipeline contract |
| API | `/api/v1` upload/analysis/ingestion resources and current state names | Strict `/api/v2` document resources; v1 removed at coordinated cutover |
| Operator UI | Integrated Jinja UI is primary and Streamlit is an additional client | Streamlit is the sole canonical UI; Jinja routes/templates/assets are retired |
| Storage facade | Collection browsing exists, but generated content inspection is analysis-oriented | Collection store exposes immutable document metadata, canonical Markdown, chunks/provenance, operations, and history |
| Parsing | `pypdf` page text becomes plain-text chunks | `pypdf` layout text is page-scoped input to required vLLM Markdown formatting |
| Formatting | No canonical strict Markdown formatter revision | Bounded page groups/slices, strict page-scoped JSON, correlation validation, no raw fallback |
| Chunking | Plain-text paragraph/sentence chunks use the existing lexical limits | Markdown-structure chunking targets 320 MPNet wordpieces, 48 overlap, hard maximum 384 |
| Dense vectors | Configured external OpenAI-compatible embeddings endpoint and variable model/dimension | Local pinned `all-mpnet-base-v2`, normalized 768 dimensions, one serialized lane |
| Sparse vectors | Hand-authored BM25-like vectors can be reused in query position | FastEmbed `Qdrant/bm25`; `embed` for documents and `query_embed` for queries |
| Profiles | One pipeline fingerprint couples content, index, thresholds, and LLM models | Separate content, index, and preflight-policy profile hashes |
| Qdrant ownership | Bridge can create physical epoch collections, payload indexes, and aliases with an admin key | Platform pre-provisions fixed active/screening names and schema; Bridge validates and mutates points only |
| Publication schema | Current named vectors and epoch/alias payload assumptions | Named vectors `dense` and `bm25`, schema v2, fixed collection mapping, immutable revision payload |
| Lifecycle | `ANALYZING`, `INGESTING`, `INGESTED`, replacement and cleanup states | `PREFLIGHTING`, `REVIEW_REQUIRED`, `PUBLISHING`, `READY`, explicit phase failures, `DELETING`, terminal tombstones |
| Scheduling | Two-slot internal worker exists without the final delete-priority/model-lane contract | Best-effort immediate queue, delete priority, two general slots, one embedding lane, peak queue near five |
| Deletion | Qdrant-first intent exists but is tied to current states/epoch collections | v2 high-priority delete, active and screening zero verification, then files/artifacts, content-free tombstone |
| Retrieval | Bridge exposes a v1/Jinja-oriented external search proxy | End-user retrieval stays outside PDF Bridge; v2 retains only a strict optional operator proxy used by Streamlit |
| Migration | Current manifest/import and empty-reset material describes the old schema | Coordinated reset, strict preserved-source reingestion through v2, no live-state or API compatibility |

## What can be retained

The refactor should preserve proven concepts where their contracts still fit: streamed bounded
uploads, SHA-256 identity evidence, malware scanning, opaque UUID storage, durable operations and
leases, collection-scoped duplicate handling, advisory LLM review, idempotency, deterministic point
IDs, audit events, Qdrant-first deletion, and content-free tombstones.

Retention means adapting behavior to the target types and profiles, not preserving v1 names or
compatibility shims. In particular, existing parsing, vector, Qdrant administration, and integrated
UI implementations are not target-compatible merely because adjacent lifecycle code is reusable.

## Closure

The code-level exit conditions were satisfied when this document became Historical:

- API v2 and Streamlit cover the complete collection/document workflow and v1/Jinja are absent;
- every prepared revision uses validated Markdown, MPNet 768 dense vectors, FastEmbed BM25 document
  vectors, and distinct BM25 query encoding;
- Bridge readiness fails unless every fixed active/screening Qdrant collection has the target
  schema, and no code path performs collection or alias administration;
- publication and deletion pass exact point-count/revision verification under retry and restart;
- all maintained docs use the target terms, states, vector names, API version, and UI boundary.

No Jenkins/handoff artifact is required by the runtime. A coordinated reset and reingestion is a
deployment event governed by [the current migration procedure](migration/historical-import.md), not
a fact asserted by this historical code-gap record.

Git is the only record of superseded documentation. Do not create a legacy-docs directory or keep
parallel active contracts.
