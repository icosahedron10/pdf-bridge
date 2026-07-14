# Security model and enterprise gates

**Status: Current**

This document describes the implemented application security boundaries, required deployment
controls, and residual risk. `Status: Current` does not attest that an environment has completed the
external controls or the mandatory enterprise gate at the end of this document.

PDF Bridge crosses browser, file-upload, malware scanner, PDF parser, local-model, remote-model,
filesystem, SQLite, and vector-database trust boundaries. The source PDF, extracted text,
LLM-produced Markdown, classifier output, Qdrant payloads, and filenames are all untrusted data.

## Security invariants

- Only configured logical collection keys are accepted. User input never supplies a filesystem
  path or physical Qdrant name.
- Clean upload bytes are stored under generated opaque UUID paths only after bounded streaming,
  server-side SHA-256, PDF-shape checks, and a successful ClamAV verdict.
- OCR is disabled. Only English native-text PDFs are supported. Encrypted, malformed, image-only,
  empty, text-insufficient, and over-budget inputs fail closed.
- pypdf layout extraction, the vLLM formatter, strict Markdown validation, chunking, local dense and
  sparse embedding, screening, duplicate checks, classifier, and verifier all run before
  ingestion. Formatting and deterministic screening must complete successfully; incomplete
  classifier/verifier evidence is explicit and requires operator review.
- Invalid or unavailable Markdown formatting has no plain-text fallback and cannot produce active
  Qdrant points.
- Published points use only the logical collection's fixed pre-provisioned Qdrant collection.
  Pending points use only the separately pre-provisioned private screening collection.
- PDF Bridge never creates, drops, renames, aliases, or changes the schema of a Qdrant collection.
- A deletion request blocks access immediately. Active and screening Qdrant points reach verified
  zero counts before the operation advances durably to <code>PURGE_STORAGE</code>; source and derived
  storage are purged afterward.
- Terminal deleted/cancelled/rejected records contain no PDF text, Markdown, vectors, prompts, or
  raw model output.

## Upload and object storage

The upload handler must:

- reject path separators, control characters, invalid PDF signatures, empty content, oversized
  bodies, and incomplete streams;
- calculate hashes and byte counts server-side while streaming to a generated temporary object;
- fail closed on scanner timeout, protocol error, or non-clean verdict;
- promote with an atomic operation into the configured storage root;
- compare resolved paths against the storage root before every open, move, or purge;
- reject exact-byte duplicates within the selected logical collection while allowing an
  intentional copy in a different collection;
- never log bytes, extracted content, Markdown, full paths, or malware samples.

The storage root must be access-controlled and encrypted according to the document classification.
The service account receives only the filesystem permissions required for its object and temporary
directories. Streamlit, Qdrant, vLLM, and retrieval services receive no direct filesystem access.
The reference container topology also isolates Streamlit on a dedicated internal operator network;
it does not join the separate ClamAV or Qdrant networks and receives no provider credentials.

## Parser boundary

pypdf is pinned and runs in a child process with hard parent wall-clock and child CPU,
address-space, page, and character limits. The child receives one staged PDF and returns bounded
page-scoped layout text. It receives no Qdrant, vLLM, session, scanner, or database credentials and
should have no network access. PyMuPDF is not an approved dependency because its licensing is
incompatible with this service.

### A parser subprocess is not a sandbox

ClamAV is not proof that a file is safe, and operating-system resource limits do not isolate
syscalls, kernel bugs, shared mounts, or every parser vulnerability. A production deployment
requires a disposable least-privilege parser boundary with:

- a minimal patched image and non-root identity;
- no outbound network and no ambient credentials;
- read access only to the staged input and write access only to bounded temporary output;
- syscall, process, filesystem, CPU, memory, and wall-time restrictions;
- vulnerability monitoring and a tested malicious-PDF response procedure.

Unexpected parser crashes, limit kills, or output-shape violations are security-relevant events.
Do not retry them without bounds or move parsing into the credential-bearing application process.

## Markdown formatter and prompt injection

pypdf layout text is untrusted even though it is native text. A PDF can contain instructions aimed
at the formatter, classifier, verifier, operator, or a later retrieval model. The formatter must
treat each delimited page as data and has one narrow authority: represent that content as
Markdown, including structurally faithful tables.

Required formatter controls:

- use the exact configured vLLM URL, token, formatter model ID, pinned prompt, and temperature-zero
  chat-completions request;
- provide no tools, filesystem, network, retrieval, or lifecycle capabilities to the model;
- enforce hard page, input-token, output-token, timeout, and attempt budgets;
- split an oversized page into deterministic ordered non-overlapping slices rather than truncate
  or reject it solely for exceeding one request budget;
- require a strict page/slice-scoped JSON response rather than accepting free-form Markdown;
- require every input page and slice exactly once, in order, with no unknown identifier;
- validate bounded expansion, normalized source coverage, preserved anchors/numbers, table shape,
  and a deterministic hash of the accepted page/slice map;
- reject raw HTML, executable links, remote-image references, and unsafe Markdown constructs before
  the content can be displayed;
- retain size-bounded raw provider exchanges only as mode-`0600`, UUID-addressed protected revision
  artifacts; never write them to logs or public resources, and purge them with document content.

Schema and fidelity checks reduce risk but cannot prove that an LLM preserved every semantic
relationship. A valid-looking formatter response may still omit, rearrange, or invent content.
The service therefore fails the document after its one bounded retry whenever validation is
inconclusive. It does not substitute pypdf text, partially accept pages, repair model output by
hand, or silently drop tables.

Canonical Markdown remains untrusted after acceptance. Streamlit must render a safe Markdown
subset with raw HTML disabled and must not automatically fetch external links or images. Markdown
is never interpreted as an instruction, prompt template, configuration value, storage path,
collection name, or executable content.

The formatter vLLM boundary receives complete extracted document content. Use a dedicated approved
endpoint and credential with organization-managed TLS, authentication, data-use and retention
guarantees, and content-free request logging. It must not fall back to the advisory endpoint.
Provider administrators and model-serving infrastructure are inside the document-data trust
boundary.

## Classifier and verifier boundary

Duplicate screening and LLM classification are preflight checks, not ingestion. Classifier and
skeptical-verifier calls use separate configured model IDs, strict structured output, hard token
and time limits, temperature zero, no tools, and source-backed chunk references.

Classifier and verifier calls use a separately configured advisory endpoint and credential. Bridge
must not send formatter requests to that endpoint or use an advisory model as a formatting
fallback. Separate credentials allow the formatter's full-document access and the advisory
boundary's narrower evidence access to be audited and revoked independently.

The advisory boundary receives only the bounded candidate/chunk evidence allowed by the preflight
policy, not an automatic copy of the complete source or formatter request.

Deterministic candidates cannot be suppressed by model output. Classifier/verifier text may explain
evidence but cannot:

- mark a document ready;
- select or delete a replacement;
- move a document between collections;
- modify canonical Markdown or vectors;
- bypass incomplete provider or Qdrant checks.

Every referenced document, chunk, page, and quote must resolve to the immutable preflight bundle.
Malformed, stale, unsupported, unavailable, or unverifiable advisory output is recorded as
incomplete and forces `REVIEW_REQUIRED`; it is never treated as “no duplicate.” Failure to build a
trustworthy deterministic candidate set is `PREFLIGHT_FAILED` and must be retried.

## Local embedding and model supply chain

<code>sentence-transformers/all-mpnet-base-v2</code> executes inside PDF Bridge and therefore
expands the application process's code, memory, and supply-chain boundary. The approved model
commit, tokenizer, Sentence Transformers, PyTorch/runtime, FastEmbed, and
<code>Qdrant/bm25</code> artifacts must be pinned and reviewed together.

Production controls must:

- prefetch approved artifacts through a controlled build/release process;
- resolve the dense commit only from the Sentence Transformers cache and place the sparse assets at
  `fastembed/{manifest_sha256}/` with an exact file-hash manifest and required non-empty
  `english.txt` asset;
- record upstream revision, file hashes, license, provenance, and vulnerability/SBOM results;
- mount the verified cache read-only and start with local-files-only behavior;
- disable remote model code and refuse floating revisions, symlinks outside the cache, hash drift,
  or startup downloads;
- verify the loaded dense model produces finite normalized 768-dimensional vectors;
- verify FastEmbed uses distinct document/query encodings and the Qdrant BM25 IDF modifier;
- treat cache replacement as a reviewed deployment and index-profile change.

Local inference creates denial-of-service risk through CPU, memory, thread, and model-load
pressure. Enforce the PDF/chunk/token limits, one embedding lane, bounded batch size, two worker
slots, operation leases, and queue/host-resource alerts. Do not increase concurrency merely to
drain a hostile or unexpectedly large queue. A process-level model crash may affect API
availability; horizontal replicas are not a supported mitigation while SQLite and process-local
coordination remain.

## Qdrant isolation and least privilege

The platform team owns collection creation and schema. Every active physical collection and the
private screening collection are fixed names in trusted configuration. Startup and readiness
validate named <code>dense</code> vectors (768 dimensions, Cosine), named <code>bm25</code> sparse
vectors with IDF, required payload indexes, and compatible point schema. Drift fails readiness;
Bridge must not repair it.

Qdrant alone receives the independently generated HS256 admin/signing key. Bridge receives only a
pre-generated granular JWT whose `access` claim contains exactly one `rw` rule for every enabled
active physical collection and the screening collection, plus a required future `exp`; it has no
global `r` or `m` grant. Bridge validates that untrusted structure and exact claim set at startup
without receiving the signing key. Qdrant verifies the signature during readiness collection
probes, so a structurally valid forgery still fails closed.

That scoped token permits collection-description, filtered alias-metadata-read, search/read,
upsert, count, and delete-point operations only for configured active and screening collections.
Alias metadata is required so readiness can reject alias participation. It must not permit:

- create, delete, or reconfigure collections;
- create, delete, or move aliases;
- access unrelated collections;
- change server configuration or issue snapshots.

Retrieval receives a separate read-only credential for active collections and no screening access.
Streamlit receives no Qdrant credential and reaches document/index state only through Bridge.
Network policy should admit Qdrant traffic only from Bridge and approved retrieval components.
Require TLS or an authenticated private ingress whenever traffic crosses a host or trust zone.

Qdrant payload text is a copy of document content and receives the same classification as the PDF.
Payload filters for document ID, logical collection, publication state, and index schema are
mandatory. A hit for pending, unknown, or cross-collection content—or any hit after a deletion has
advanced to `PURGE_STORAGE`—is an integrity incident, not a reason to loosen validation. A hit during
the acknowledged-delete interval before verified zero is the documented residual window and must
never be mistaken for completed deletion.

Self-hosted Qdrant must have authentication and JWT RBAC enabled; anonymous describe, list, query,
upsert, and delete attempts must fail. The admin signing key must be absent from the Bridge process,
logs, and support bundles. Run the opt-in live RBAC gate documented in the runbook after every
credential, collection mapping, or platform change; it proves the Bridge token can describe an
enabled collection but cannot see/write an unrelated collection or create one. Separately prove the
retrieval key cannot list/query screening.

## Authentication and canonical Streamlit UI

Streamlit is the canonical operator experience but not a trusted lifecycle authority. It uses only
authenticated Bridge APIs and never reads SQLite, object storage, or Qdrant directly.

The optional Streamlit Search page also calls only Bridge. Bridge forwards a bounded request to the
separately owned retrieval service with a Bridge-held credential, validates returned document IDs
and collection membership against the catalog, and fails closed on unknown, non-`READY`, or
cross-collection hits. It never exposes the upstream token, queries screening as a fallback, or
turns the operator proxy into an end-user authorization boundary.

Browser mutations require an authenticated session, same-origin request, CSRF protection, and
idempotency key. The Bridge URL is deployment-owned in Streamlit, is not operator-editable, and
redirects are refused to limit server-side request forgery and credential forwarding.

The session cookie is marked `Secure` only when `PDF_BRIDGE_APP_ENV=enterprise`. Any deployment
terminating TLS in front of Bridge must therefore run in the enterprise environment, which also
forces trusted-header authentication; the non-enterprise cookie is intended only for the isolated
localhost POC topology.

In trusted-header mode, the approved ingress injects the configured identity header into the
incoming Streamlit request. Streamlit requires it and forwards its value server-side; the browser
does not manufacture the header. Bridge accepts it only when Streamlit's direct-peer address is in
the configured trusted proxy CIDRs. Direct access to either app must be blocked. CORS is disabled
unless a reviewed deployment requirement defines exact origins and credentials behavior.

The isolated POC may use anonymous sessions for attribution, but anonymous mode does not prove
human identity. Anyone who can reach it can upload, review, replace, cancel, download, or delete.
Use trusted-header SSO and explicit authorization before handling sensitive collections or before
operators should have different privileges.

Logical collection labels are not end-user retrieval authorization. Any downstream retrieval
application must derive allowed physical/logical collections from authenticated server-side policy
and intersect that policy with every query.

## Deletion, tombstones, and privacy

On a durable deletion request, Bridge immediately removes the document from its active views and
blocks all Bridge content reads. It then deletes and verifies all applicable active and screening
points before persisting <code>PURGE_STORAGE</code> as the next phase. Only Qdrant zero ends the short
interval in which a separate retrieval client could still observe old active points. Only afterward
may Bridge purge the PDF, raw extraction, Markdown, chunks, vectors, prompts, raw model output, and
other protected artifacts.

If Qdrant deletion fails, source content remains for an idempotent retry. If storage purge fails at
or after <code>PURGE_STORAGE</code>, points remain absent, access remains blocked, and retries perform
only storage/content-row cleanup. Unexpected paths, permission failures, or residual artifacts fail
hard. No cleanup exception may be swallowed.

The terminal tombstone retains only the minimum content-free audit fields: document UUID, logical
collection, lifecycle timestamps, actor/change identity, operation IDs, content/manifest hashes,
and bounded reason/status codes. Even these fields can be personal or confidential metadata.
Apply retention, access, and legal-hold policy to tombstones and audit records.

Deletion from live storage does not automatically remove content from immutable logs, filesystem
snapshots, Qdrant snapshots, model-provider logs, or backups. Those systems require documented
retention and expiry. A restore must reconcile newer tombstones before exposing restored content;
never resurrect a deleted PDF merely because it exists in an older backup.

## Logging and incident evidence

Allowed structured fields include bounded actor identity, request/document/operation/preflight/
replacement IDs, logical collection key, phase, attempt, checkpoint, duration, model ID/revision,
profile hash, Qdrant collection identifier, point count, and content-free error category.

Exclude PDF bytes/text, canonical Markdown, snippets, search queries, prompts, raw model output,
vectors, session or CSRF data, API keys, bearer tokens, JWTs, full object paths, and user-supplied
error text. Hashes are identifiers and may still be sensitive.

For malware escape, parser compromise, prompt injection with unsafe output, model/cache
compromise, vLLM disclosure, screening exposure, cross-collection results, replacement overlap,
deletion resurrection, credential disclosure, or unexplained catalog/index drift:

1. contain uploads, worker dispatch, Streamlit mutations, vLLM, and retrieval without destroying
   evidence;
2. preserve protected content-free IDs, checkpoints, timestamps, fingerprints, hashes, and logs;
3. revoke affected credentials and have the platform owner quarantine inconsistent points;
4. verify the model cache and serving revisions against approved hashes;
5. rebuild from externally preserved verified PDFs when content or index integrity is uncertain;
6. rerun readiness, access-denial, reconciliation, replacement, and deletion-interruption tests
   before reopening service.

Do not manually rewrite lifecycle, decision, checkpoint, or audit rows.

## Residual risks

- ClamAV and PDF-shape validation cannot prove a PDF is harmless.
- A resource-limited parser subprocess is not a complete malicious-document sandbox.
- Structured output and deterministic fidelity checks cannot prove that LLM Markdown is fully
  faithful or immune to prompt injection.
- Local model execution increases application memory, dependency, and denial-of-service exposure.
- A compromised vLLM endpoint can observe source content and forge plausible structured responses.
- External retrieval can observe a document between delete acknowledgment and verified Qdrant zero;
  priority reduces this interval but does not make deletion synchronous.
- SQLite plus one process is not highly available and cannot safely scale horizontally.
- Filenames, UUIDs, hashes, collection membership, actors, search queries, and tombstones can
  disclose sensitive metadata.
- Qdrant and backup copies extend the time and systems over which deletion must be reconciled.

## Mandatory enterprise gate

Do not describe the deployment as enterprise-ready until owners verify:

- [ ] Enterprise SSO, direct-app blocking, trusted proxy CIDRs, allowed hosts, CSRF, session
      rotation, and role/collection authorization.
- [ ] Organization-managed TLS and network policy for Streamlit, Bridge, ClamAV, vLLM, Qdrant, and
      retrieval.
- [ ] Approved storage encryption, backup, restore, retention, legal hold, tombstone, and verified
      deletion procedures.
- [ ] A disposable no-network parser sandbox and malicious-PDF test program; a child process alone
      does not satisfy this item.
- [ ] ClamAV signature freshness, outage handling, and the organization's malware/CDR policy.
- [ ] vLLM data use, model provenance, authentication, retention, residency, logging, capacity,
      patching, and incident response.
- [ ] Formatter prompt-injection review, strict schema/fidelity corpus tests, safe Markdown
      rendering, and proof that every invalid path has no plain-text fallback.
- [ ] Read-only verified local model cache, exact revision/hash enforcement, remote-code disabled,
      SBOM/license review, dependency/container scanning, and host resource limits.
- [ ] Qdrant fixed-name/schema ownership, least-privilege Bridge credential, active-only retrieval
      credential, screening denial, TLS/private ingress, audit, snapshot, and drift response.
- [ ] Operator search proxy authentication, strict response correlation, upstream credential
      isolation, disabled-state behavior, and proof that it grants no end-user retrieval authority.
- [ ] Crash tests before and after the transition to <code>PURGE_STORAGE</code>, plus verified
      filesystem, SQLite, active-index, screening-index, snapshot, and backup deletion
      reconciliation.
- [ ] A database and distributed-coordination redesign before multiple processes, replicas, or high
      availability.
- [ ] Rate limits, proxy body limits, queue/capacity alerts, SAST, DAST, penetration testing, and
      incident exercises.
- [ ] A labeled same-collection evaluation corpus covering prose and tables, with recorded
      content/index/policy profile hashes and approved retrieval/preflight quality thresholds.

Application enterprise-mode validation is a startup guard, not completion of this checklist.
