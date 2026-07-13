# Security model and enterprise gates

PDF intake crosses file-upload, malware, parser, model, vector-database, and retrieval trust
boundaries. These controls make the POC fail closed for known unsafe states; they do not make
arbitrary PDFs or model output trustworthy.

## Implemented controls

- Display names must end in `.pdf`; separators, control characters, empty files, oversized bodies,
  and invalid leading signatures are rejected.
- Uploads stream to generated temporary names with byte limits and server-calculated SHA-256.
- ClamAV `INSTREAM` runs synchronously. Scanner error, protocol failure, or unclean verdict prevents
  canonical promotion.
- Exact bytes are blocked only within the selected configured collection. User filenames never
  influence canonical paths or collection assignment.
- Pinned pypdf runs in a child process with wall-clock, Linux CPU/address-space, page, character,
  and chunk limits. Encryption, malformed input, image-only/insufficient text, and over-budget
  content reject without override and are purged.
- Browser mutations require an authenticated/encrypted session, same-origin request, and CSRF token.
  Trusted-header mode accepts identity only from configured direct-peer proxy CIDRs. CORS is absent.
- Durable decisions name an exact analysis revision. Keep/Replace/Cancel records are immutable;
  replacement targets must be current, same-collection, ingested candidates.
- PDF text is sent to models as untrusted quoted data. Tools are disabled, output uses a strict
  schema at temperature zero, malformed output is retried once, and every cited reference and quote
  is checked against retained source.
- Model findings are explanation-only. They cannot suppress deterministic candidates, publish,
  delete, or select a replacement.
- Pending points use a separate screening collection. Active point payloads require publication and
  schema markers, and Bridge validates external retrieval UUIDs against eligible catalog state.
- Qdrant requires an API key and enables JWT RBAC. Bridge has the administrative key; retrieval gets
  collection-scoped read JWTs for active aliases only and no screening permission.
- Qdrant mutations are durable in a SQL outbox, idempotent by deterministic point ID, applied with
  wait and strong ordering, and verified by exact count.
- Full analysis content is compressed in private storage and is purged on cancellation/deletion.
  Audit records keep a canonical manifest hash and metadata, not excerpts, vectors, prompts, raw
  model output, or credentials.
- The reference app container runs as non-root, drops capabilities, disallows privilege escalation,
  uses a read-only root filesystem, and mounts only explicit data/tmp paths writable. Qdrant is not
  published to the host and sits on an internal-only network.

## Residual risks

### Parser containment is not a sandbox

A clean malware result is not a safety proof, and pypdf may contain exploitable defects. Linux
resource limits constrain CPU and address space but do not provide a complete syscall, filesystem,
or kernel boundary. Production use requires a least-privilege disposable parser sandbox with no
network, minimal readable files, a patched image, and owned vulnerability response.

### Prompt injection classification is out of scope

The prompt marks PDF text untrusted and validates citations, but the system does not classify prompt
injection. A document may still influence explanatory labels or summaries. Deterministic candidate
retention and human decisions are the mutation boundary; do not reuse model prose as executable
instructions.

### Anonymous POC access is not human authentication

`anonymous-poc` creates auditable sessions but does not prove identity. Anyone who can reach the app
has the trusted-operator capability set, including Keep, Replace, Cancel, and delete. Network-isolate
the POC or deploy trusted-header SSO before handling sensitive collections.

### Collection labels are not end-user authorization

`customer` and `internal` audience labels help operators place content. PDF Bridge does not decide
which chatbot user may query a collection. The external application must derive allowed collections
from authenticated server-side policy and intersect them with every request.

### Administrative Qdrant key is high impact

The Bridge key can read and mutate active and screening collections and manage aliases. Disclosure
exposes pending documents and can corrupt retrieval. Store it in a secret manager, restrict network
access, monitor rejected authentication, and rotate it through an approved procedure. Changing the
admin key invalidates JWTs signed by the previous key.

A global Qdrant read-only key is not sufficient isolation because it can read screening. Retrieval
must use granular JWT claims scoped to required active collections. Test denial of collection-list
and screening queries after every token or topology change.

### Private-network HTTP still needs a threat decision

Reference Compose keeps Qdrant on an internal-only single-host network and does not expose its port.
API keys still cross that network without Qdrant-native TLS. Use TLS or an authenticated private
ingress whenever traffic crosses hosts, untrusted namespaces, or a network where packet capture is
credible. Provider and retrieval endpoints should use organization-managed TLS.

### SQLite and one process

The supported topology is one SQLite writer and one Uvicorn process. It is not highly available,
does not provide per-service database tenancy, and places catalog plus source/analysis data in one
recovery domain. A second process defeats in-process locks; horizontal scaling requires a redesigned
distributed coordination model.

### Sensitive metadata remains

Filenames, actors, UUIDs, collection keys, hashes, bounded errors, search queries, and returned
snippets may be confidential even when PDF content is excluded from logs. Apply classification,
retention, backup, deletion, and access controls to metadata as well as source bytes.

### Outage override is deliberate

Embedding, LLM, or Qdrant outages are advisory for the semantic decision, but publication still
requires complete dense and BM25 points. Keep can override analysis incompleteness; it cannot bypass
durable indexing. Monitor extended retained pending content and avoid treating an outage as an empty
candidate result.

## Mandatory enterprise gate

Do not call the deployment enterprise-ready until responsible owners approve and verify:

- [ ] Organization-managed TLS, reverse-proxy behavior, allowed hosts, and trusted direct-peer CIDRs.
- [ ] Enterprise SSO with `PDF_BRIDGE_AUTH_MODE=trusted-header`; direct app access blocked.
- [ ] Authorization for upload/review/delete if all authenticated operators should not be peers.
- [ ] Server-side chatbot policy that derives and enforces allowed collections.
- [ ] Qdrant TLS/private ingress, administrative-key custody, JWT expiry/rotation, active-only scopes,
      screening denial, audit logging, and network egress restrictions.
- [ ] Separate secret-manager entries, owners, rotation, and revocation for session, Qdrant,
      embedding, LLM, and retrieval credentials.
- [ ] Disposable parser sandbox, no parser network, least privilege, patch SLA, and malicious-PDF
      testing. A resource-limited subprocess alone does not satisfy this item.
- [ ] ClamAV signature freshness and the organization's malware/CDR/encrypted-document policy.
- [ ] Provider data-use, retention, residency, TLS, authentication, logging, and model-change policy.
- [ ] Prompt-injection and data-exfiltration threat review for quoted PDF text and LLM output.
- [ ] Approved encrypted storage, backup, legal-hold, restore, retention, and verified deletion.
- [ ] A database/coordination redesign before multiple replicas or high availability.
- [ ] Active/screening reconciliation, alias/epoch checks, outbox crash tests, and alerts on unknown
      IDs or publication/schema violations.
- [ ] External retrieval conformance for BM25, dense, hybrid RRF, filters, payloads, and screening
      denial.
- [ ] Rate limits, proxy body limits, capacity monitoring, SAST, dependency/container scanning,
      DAST, and focused penetration testing.
- [ ] A labeled same-collection evaluation corpus with candidate recall at least `0.98`, plus a
      recorded dataset hash and parser/model/threshold fingerprints.

Application enterprise-mode validation is only a backstop; it does not complete this checklist.

## ClamAV operations

Compose builds the exact `clamav/clamav:1.5.3` patch and persists signatures. The daemon stream limit
is 64 MiB, above the default 50 MiB upload cap, and its port is not published.

- Monitor FreshClam logs, signature timestamps, update failures, and daemon readiness.
- Keep ClamAV storage separate from canonical PDFs; the app streams bytes to the daemon.
- Treat scanner errors as a security outage, never as permission to skip scanning.
- Review the official image and vulnerability data before changing the pin.

## Logging and incident evidence

Permitted structured fields include request ID, route/status, bounded actor identity, document,
analysis, operation, replacement and outbox IDs, event type, duration, provider category, and
content-free hashes. Exclude PDF text/bytes, snippets, vectors, prompts, raw model output, session or
CSRF data, bearer/API keys, JWTs, and full local paths.

On suspected malware escape, parser compromise, model/provider compromise, screening disclosure,
Qdrant credential leak, cross-collection result, or replacement overlap:

1. contain uploads, worker, provider, and retrieval access without deleting evidence;
2. preserve protected logs, audit rows, IDs, timestamps, fingerprints, and hashes;
3. revoke/rotate affected credentials and regenerate Qdrant JWTs when needed;
4. quarantine inconsistent index content and validate the authoritative SQL collection mapping;
5. rebuild from externally preserved verified sources;
6. run positive/negative retrieval, replacement-ordering, purge, and access-denial tests before
   reopening traffic.

Do not manually edit append-only audit/decision records or force lifecycle state. Use supported
operations or a reviewed repair migration that preserves original evidence.
