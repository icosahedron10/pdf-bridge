# Large-scale refactor plan

Status: Implemented

This plan was used to convert the repository to the architecture described by the
[service contract](service-contract.md). The software changes in sections 1 through 5 are
implemented, including the API-v2-only cutover and removal of v1/Jinja runtime surfaces. Section 6
and the release acceptance matrix remain an operational procedure: this status does not claim that
any environment has completed its reset, source reingestion, security approval, or release sign-off.

## 1. Lock contracts and test fixtures

- Treat the target docs, API v2 schemas, state enum, vector names `dense`/`bm25`, and fixed
  collection mapping as change-controlled inputs.
- Build a checked-in evaluation corpus of licensed English native-text PDFs covering headings,
  multi-page tables, wide tables, lists, page boundaries, duplicate families, and malformed or
  image-only rejection. Record expected page/slice coverage and table semantics, not model prose
  verbatim where harmless formatting variance is allowed.
- Pin Python dependencies, `pypdf`, the vLLM formatter/classifier/verifier model revisions and prompt
  schemas, `all-mpnet-base-v2` revision, tokenizer, FastEmbed, and `Qdrant/bm25` assets. Make startup
  fail when local model assets are unavailable or MPNet output is not 768-dimensional.

Gate: contracts have schema tests and the corpus can be used offline except for the controlled
vLLM endpoint.

## 2. Replace lifecycle, persistence, and configuration

- Replace v1 document states with the target enum and operation phases. Because cutover is empty,
  create a clean target migration instead of translating live rows or retaining old enum aliases.
- Persist prepared revisions separately from documents: page extraction, formatter batches,
  Markdown, chunks, vectors, candidate evidence, decisions, profile hashes, manifest hash, and
  publication/deletion checkpoints. Persist the resolved active Qdrant collection in the prepared
  revision, successful publication record, and deletion checkpoint. Make completed revisions
  immutable at the persistence layer.
- Change collection configuration to require logical key/display metadata and one explicit fixed
  active Qdrant name; require one fixed private screening name globally. Reject duplicate names,
  aliases/epochs, unknown collections, and runtime mutation.
- Replace the Qdrant admin bootstrap path with readiness-only schema validation and point-scoped
  credentials. Remove collection creation/deletion, alias, epoch, and payload-index creation code.

Gate: a clean database boots with target states; startup fails against missing or incompatible fixed
collections and performs no collection-level Qdrant mutation.

## 3. Implement immutable preparation

- Keep bounded streaming, hashing, PDF validation, ClamAV, UUID promotion, parser subprocess limits,
  and hard input rejection.
- Extract `pypdf` layout text per page; enforce English/native-text gates. Pack bounded consecutive
  pages and deterministically slice oversize pages for vLLM. Validate strict page/slice JSON,
  hashes, completeness, order, non-empty Markdown, fences, and tables. Compare normalized source and
  Markdown projections as exact ordered Unicode word/number sequences to reject omission,
  invention, or reordering. Retry boundedly and fail hard with no raw-text fallback.
- Assemble canonical page Markdown and implement heading/table-aware chunking at 320 target, 48
  overlap, and 384 hard maximum MPNet wordpieces. Retain page/heading provenance and stable hashes.
- Load local MPNet and FastEmbed BM25 once per process. Serialize dense batches, store normalized
  768-dimensional vectors, use BM25 document encoding for points, and expose a separate query
  encoding function for searches.
- Generate distinct content, index, and preflight-policy hashes and seal a prepared manifest only
  after every artifact/vector correlation check passes.

Gate: fixtures prove exact page/slice coverage, table-preserving Markdown, deterministic chunks,
hard token bounds, stable hashes, correct vector dimensions, and no fallback on formatter failure.

## 4. Rebuild preflight, publication, and deletion

- Upsert pending prepared points to the fixed private screening collection and discover candidates
  only within the logical collection across active and screening points. Query sparse search with
  `query_embed`, fuse rankings, retain deterministic evidence, then run the existing independent LLM
  classifier/verifier as advisory policy.
- Send clear complete revisions to `PUBLISHING`; send candidates or incomplete advisory evidence to
  `REVIEW_REQUIRED`. Bind Keep/Replace/Cancel to the exact prepared revision and reject stale
  decisions.
- Publish deterministic point IDs with both `dense` and `bm25`, wait for apply, verify exact count,
  payload revision, and vector schema, delete/verify screening points, then set `READY`. Retries
  reuse artifacts and never re-embed.
- Make the worker a durable priority queue: two general slots, one dense semaphore, and delete ahead
  of replacement-delete, publication, and preflight. Add queue/phase metrics and restart recovery.
- Implement deletion checkpoints: immediately block Bridge reads, active filter-delete and zero
  verification, screening delete and zero verification, storage/artifact purge, content-bearing row
  purge, tombstone. Resume from the last durable checkpoint and never republish after active zero.
- Implement Replace as verified old deletion followed by new publication, accepting an explicit
  availability gap and prohibiting old/new overlap.

Gate: fault-injection tests at every external call and checkpoint converge after retry/restart;
partial upserts never become `READY`, and file purge never precedes verified Qdrant zero.

## 5. Cut over API and operator UI

- Implement strict API v2 resources, cursor lists, immutable metadata, operation progress, protected
  source/Markdown/chunk reads, revision-bound decisions, idempotent retries/deletes, history, and
  sanitized errors. Port the existing retrieval integration only as a strict optional operator
  search proxy; do not add end-user ranking/authorization or a collection-management API.
- Make Streamlit a pure v2 client and the sole operator experience. Provide collection-store views,
  per-state filters, document metadata, source access, rendered/raw Markdown, paged chunks with
  provenance, preflight evidence, review actions, retries, delete progress, and tombstone history.
- Remove the Jinja controllers/templates/static operator bundle and all v1 routes/schemas/tests in
  the same cutover change. Update health/readiness and observability for local models, vLLM, fixed
  Qdrant schemas, queue age, and deletion checkpoints.

Gate: end-to-end Streamlit tests cover clear upload, review Keep/Replace/Cancel, retry, document and
chunk inspection, refresh/restart recovery, and deletion; route enumeration proves v1/Jinja absent.

## 6. Coordinated reset and release (deployment procedure)

- Follow [coordinated reingestion](migration/historical-import.md): preserve and hash source PDFs,
  stop all writers/readers, have the platform clear and validate the fixed Qdrant collections, wipe
  disposable Bridge catalog/storage, deploy the target, and reingest through API v2 with at most
  five outstanding documents.
- Resolve all `REVIEW_REQUIRED` items, retry only understood failures, reconcile every `READY`
  document's expected points, and run collection-isolation/deletion smoke tests before reopening.
- Remove Jenkins jobs/secrets/webhooks/handoff storage and verify no runtime configuration or docs
  reference them. Keep superseded source/docs only in Git history; create no legacy archive.

Gate for each deployment: the release acceptance matrix below passes on the reset environment and
rollback artifacts are retained until sign-off. Record that evidence in the deployment change, not
in this repository status.

## Operational acceptance matrix

- A clear English native-text PDF reaches `READY` directly; a table remains valid Markdown and its
  chunks stay within 384 MPNet wordpieces.
- Encrypted, malformed, image-only, non-English, and over-limit files become content-free
  `REJECTED`; formatter invalid JSON or missing pages becomes explicit `PREFLIGHT_FAILED`.
- Exact and semantic candidates remain collection-scoped; invalid/unavailable classifier output
  cannot clear evidence and enters review.
- Keep publishes the inspected revision, Cancel purges it, and Replace proves old active count zero
  before any new active upsert.
- Five queued uploads make progress without model-memory overlap; a newly accepted delete overtakes
  queued preflight work.
- Publication interruption, duplicated delivery, and restart converge to one exact point per chunk
  with both vectors and the approved revision.
- Delete blocks reads immediately, retains files on Qdrant failure, resumes purge after point zero,
  and ends with no active/screening points, PDF, Markdown, chunks, vectors, prompts, or raw output.
- Streamlit can inspect every configured store/document and immutable metadata without direct
  storage/database/Qdrant/retrieval access; there are no v1, Jinja, collection-admin, or end-user
  retrieval endpoints, and the optional operator search uses only the Bridge proxy.

## Explicit assumptions

- Corpus: English native-text PDFs only; OCR remains out of scope.
- Capacity: one Bridge process, two general worker slots, one local dense-embedding lane, and about
  five queued documents at peak.
- Infrastructure: platform-owned, pre-provisioned fixed active and private-screening Qdrant
  collections; Bridge has point read/write/delete permission only.
- Migration: disposable catalog/index state, externally preserved source PDFs, coordinated reset
  and reingestion, no live compatibility or rollback merge.
