import type { ReactNode } from "react";

import { Callout, CodeBlock, DocumentationTable, ModuleReferences } from "./components";
import type { TocItem } from "./DocsShell";

export type GuidePage = {
  category: string;
  title: string;
  summary: string;
  facts: Array<{ term: string; detail: string }>;
  toc: TocItem[];
  content: ReactNode;
};

const libraryOperator: GuidePage = {
  category: "Role guide",
  title: "Library operator",
  summary:
    "Use the browser workspace to upload and analyze PDFs, inspect advisory evidence, make document decisions, and follow durable work to completion.",
  facts: [
    { term: "Primary surface", detail: "Browser UI" },
    { term: "Owns", detail: "Collection choice and Keep, Replace, Cancel, or delete decisions" },
    { term: "Does not own", detail: "Worker internals, Qdrant administration, or chatbot authorization" },
  ],
  toc: [
    { id: "before", label: "Before you begin" },
    { id: "normal-path", label: "Normal path" },
    { id: "what-you-see", label: "What you see" },
    { id: "decisions", label: "Decision rules" },
    { id: "failure", label: "When something goes wrong" },
    { id: "ownership", label: "Where ownership ends" },
    { id: "implementation", label: "Implementation map" },
  ],
  content: (
    <>
      <section id="before">
        <h2>Before you begin</h2>
        <p>
          The root URL redirects to <code>/library</code>. Collections, Queue, and Upload are
          responsibility views, not permission roles: every trusted browser operator currently has
          the same capability set. Collection audience labels help place content but do not
          authorize chatbot users.
        </p>
        <p>
          Choose the collection deliberately. Exact bytes are blocked only when that selected
          collection already retains them; the same PDF may exist in a different collection by
          design.
        </p>
      </section>

      <section id="normal-path">
        <h2>Normal path</h2>
        <div className="journey">
          <div><span>1. Select</span><strong>Choose the destination collection, then choose one or more PDFs.</strong><p>The page checks up to three requests at once and keeps each file independent.</p></div>
          <div><span>2. Preflight</span><strong>Review typed filename-family warnings.</strong><p>These collection-scoped warnings are advisory. They do not block upload or ask for a duplicate confirmation.</p></div>
          <div><span>3. Upload</span><strong>Start “Upload and analyze.”</strong><p>The bridge streams, hashes, validates, and ClamAV-scans each file. Clean bytes are promoted atomically and accepted as durable analysis work.</p></div>
          <div><span>4. Follow</span><strong>Leave the workspace open or return later.</strong><p>All active rows poll together. Open work is restored from the server after a refresh or process restart.</p></div>
          <div><span>5. Review</span><strong>Inspect every qualifying candidate and its paginated evidence.</strong><p>A clear analysis ingests automatically. Filename, semantic, contradiction, overflow, or provider-outage findings wait for a decision.</p></div>
          <div><span>6. Decide</span><strong>Choose Keep, Replace, or Cancel.</strong><p>Keep accepts advisory evidence, Replace removes one eligible current document first, and Cancel purges retained unpublished content.</p></div>
        </div>
      </section>

      <section id="what-you-see">
        <h2>What you see</h2>
        <DocumentationTable
          headings={["Page", "Experience"]}
          rows={[
            [<code key="a">/library</code>, "Configured collections, authoritative catalog counts, and optional retrieval totals"],
            [<code key="b">/library/&lt;collection&gt;</code>, "Available documents or collection-scoped keyword, semantic, and hybrid results"],
            [<code key="c">/upload</code>, "Preflight warnings, scan/upload progress, extraction and comparison phases, evidence, decisions, retries, and restored open work"],
            [<code key="d">/queue</code>, "Current processing and failure states with the latest operation phase and bounded error"],
            [<code key="e">/documents/&lt;uuid&gt;</code>, "Collection, state, analysis summary, immutable decisions, operation attempts, and content-free audit evidence"],
          ]}
        />
      </section>

      <section id="decisions">
        <h2>Decision rules</h2>
        <ul>
          <li>Filename, normalized-text, dense, BM25, LLM, contradiction, and outage findings are advisory.</li>
          <li>Encrypted, malformed, image-only, insufficient-text, and over-budget PDFs are rejected without override. OCR is out of scope.</li>
          <li><strong>Keep</strong> records an immutable advisory override and queues publication. A provider outage may still leave publication retryable.</li>
          <li><strong>Replace</strong> requires exactly one eligible, current, same-collection ingested candidate.</li>
          <li>Replacement can create an availability gap: the old document is verified absent before the new document is published. It never permits old/new overlap.</li>
          <li><strong>Cancel</strong> removes canonical bytes, private analysis artifacts, and screening points. It does not leave retrievable content.</li>
          <li>Every decision names the displayed analysis revision. Reload and review again if the server reports that revision or collection data is stale.</li>
        </ul>
      </section>

      <section id="failure">
        <h2>When something goes wrong</h2>
        <DocumentationTable
          headings={["What you see", "Meaning", "Correct response"]}
          rows={[
            ["Scanner unavailable", "The synchronous malware gate could not complete", "Wait for ClamAV readiness; do not request a bypass"],
            [<code key="r">REJECTED</code>, "The PDF failed a non-overridable parser or text-quality gate", "Obtain a clean text-bearing PDF; do not force its state"],
            [<code key="if">INGEST_FAILED</code>, "Publication did not complete, but retained work can be retried", "Repair the named embedding or Qdrant cause, then Retry work"],
            [<code key="rf">REPLACE_FAILED</code>, "Safe replacement stopped at a durable phase", "Confirm whether the old document is still active, repair the cause, then Retry work"],
            [<code key="df">DELETE_FAILED</code>, "Active removal or verification failed", "Repair Qdrant or storage and retry the document deletion"],
            [<code key="cf">CLEANUP_FAILED</code>, "A purge step failed after content became unavailable", "Repair storage or the index and retry cleanup"],
            ["Search error", "Retrieval was unavailable or failed strict correlation", "Use catalog pages while the complete result remains suppressed"],
          ]}
        />
      </section>

      <section id="ownership">
        <h2>Where ownership ends</h2>
        <p>
          You own collection placement and explicit lifecycle decisions. You do not edit SQL,
          change operation leases, infer candidate eligibility, mutate Qdrant, or decide which
          chatbot users may retrieve a collection. Correct a placement mistake through supported
          cancellation or verified deletion, then upload again.
        </p>
      </section>

      <section id="implementation">
        <h2>Implementation map</h2>
        <ModuleReferences
          items={[
            { path: "pdf_bridge/templates/upload.html", purpose: "Upload, evidence, decision, replacement, and retry controls" },
            { path: "pdf_bridge/static/upload.js", purpose: "Three-request pool, grouped polling, safe text rendering, and restored work" },
            { path: "pdf_bridge/controllers/api.py", purpose: "Preflight, upload, polling, analysis, decision, retry, cancellation, and deletion routes" },
            { path: "pdf_bridge/managers/document.py", purpose: "Mutation transaction and compensation boundaries" },
            { path: "pdf_bridge/services/intake.py", purpose: "Duplicate, decision, replacement eligibility, retry, and deletion rules" },
            { path: "pdf_bridge/services/catalog.py", purpose: "Upload workspace, evidence pagination, and catalog eligibility" },
            { path: "tests/test_browser.py", purpose: "Browser workflow and accessibility coverage" },
          ]}
        />
      </section>
    </>
  ),
};

const semanticIntakeOwner: GuidePage = {
  category: "Role guide",
  title: "Semantic intake owner",
  summary:
    "Own the internal worker’s extraction, comparison, provider, Qdrant, recovery, and replacement behavior without bypassing durable lifecycle state.",
  facts: [
    { term: "Primary surface", detail: "Worker telemetry, SQL operations, provider health, and Qdrant" },
    { term: "Runtime", detail: "Two worker slots inside one Litestar process" },
    { term: "Safety boundary", detail: "No transaction spans parser, provider, or Qdrant calls" },
  ],
  toc: [
    { id: "runtime", label: "Worker runtime" },
    { id: "analysis", label: "Analysis path" },
    { id: "indexing", label: "Index publication" },
    { id: "replacement", label: "Safe replacement" },
    { id: "recovery", label: "Recovery" },
    { id: "parser", label: "Parser boundary" },
    { id: "implementation", label: "Implementation map" },
  ],
  content: (
    <>
      <section id="runtime">
        <h2>Worker runtime</h2>
        <p>
          Application lifespan starts and stops one <code>AnalysisWorker</code>. It has two execution
          slots, unique process-scoped ownership, SQL leases and heartbeats, and a process-local lock
          per collection. Expired <code>RUNNING</code> work is reclaimed during ordinary polling.
        </p>
        <p>
          Operations are <code>ANALYZE</code>, <code>INGEST</code>, <code>DELETE</code>, or
          <code>CLEANUP</code>. Each external step is bracketed by short transactions that record a
          visible phase, lease, attempt, audit event, or outbox mutation. Never hold SQLite open
          across parsing, model inference, or Qdrant I/O.
        </p>
      </section>

      <section id="analysis">
        <h2>Analysis path</h2>
        <div className="journey">
          <div><span>Extract</span><strong>Run pinned pypdf in a child process under hard budgets.</strong><p>Preserve page mapping and reject encryption, malformed input, insufficient text, or any page, character, chunk, CPU, memory, or wall-time overrun.</p></div>
          <div><span>Chunk</span><strong>Create stable paragraph and sentence-aware chunks.</strong><p>Chunks target 400 lexical tokens with 60-token overlap and a 3,500-character hard cap; truncation is forbidden.</p></div>
          <div><span>Compare</span><strong>Search the selected collection’s active alias and private screening index.</strong><p>Deterministic text, filename, cosine, and repeated BM25 rules choose candidates; reciprocal rank fusion orders them.</p></div>
          <div><span>Explain</span><strong>Classify the top candidates twice.</strong><p>Independent temperature-zero classifier and skeptical-verifier calls add validated, page-referenced explanation only.</p></div>
          <div><span>Route</span><strong>Publish clear work or hold advisory evidence.</strong><p>Any candidate, filename warning, qualifying overflow, or incomplete semantic check enters review. Reviews do not expire.</p></div>
        </div>
      </section>

      <section id="indexing">
        <h2>Index publication</h2>
        <p>
          Each logical collection has an epoch-versioned physical collection behind its stable
          <code>collection_key</code> alias. Unresolved and not-yet-published chunks live in
          <code>pdf-bridge-screening-v1</code>. Every point uses a deterministic UUIDv5 ID and carries
          both <code>content_dense</code> and <code>content_bm25</code> vectors.
        </p>
        <ul>
          <li>Active preparation writes <code>published=false</code>; retrieval cannot see it.</li>
          <li>SQL outbox entries record ordered UPSERT, PUBLISH, and DELETE intent before mutation.</li>
          <li>Qdrant writes use strong ordering, wait for apply, and verify the exact document point count.</li>
          <li>Only a successful publication boundary flips complete active points to <code>published=true</code>.</li>
          <li>Screening queries require <code>screening=true</code>; active queries require <code>published=true</code> and the current schema version.</li>
        </ul>
      </section>

      <section id="replacement">
        <h2>Safe replacement</h2>
        <ol className="procedure">
          <li>Prepare or regenerate the new document’s complete dense and sparse vectors without publishing them.</li>
          <li>Delete the old document’s active points through the outbox and verify an exact count of zero.</li>
          <li>Purge the old source and analysis artifacts and commit its <code>DELETED</code> tombstone.</li>
          <li>Write and publish the new active points, verify their count, then remove its screening points.</li>
          <li>Mark the replacement and new document successful.</li>
        </ol>
        <Callout title="Availability gap is intentional" tone="warning">
          <p>
            An old-delete failure blocks new publication. A failure after verified old deletion
            leaves the new document retryable and the old document absent. Never restore the old
            content ad hoc and never allow old and new points to overlap.
          </p>
        </Callout>
      </section>

      <section id="recovery">
        <h2>Recovery</h2>
        <DocumentationTable
          headings={["Evidence", "Interpretation", "Action"]}
          rows={[
            ["Expired RUNNING lease", "The owning process or thread no longer heartbeats", "Confirm the prior owner is gone, restart one process, and let ordinary polling reclaim it"],
            ["Pending outbox entry", "SQL intent may be ahead of Qdrant, or Qdrant may have applied before acknowledgement", "Replay idempotently and trust exact point-count verification"],
            ["Incomplete analysis", "Embedding, Qdrant, or classifier checks did not all complete", "Expose the reason for review; do not convert it into a clear result"],
            ["Keep plus publication failure", "The semantic decision is durable but indexing is incomplete", "Repair the provider or index and retry without requesting another decision"],
            ["DELETING_EXISTING", "Replacement has not yet permitted new active points", "Repair old deletion and prove zero count before advancing"],
            ["INGESTING_NEW", "The old document is intentionally gone", "Repair and retry new publication; preserve the recorded ordering"],
          ]}
        />
      </section>

      <section id="parser">
        <h2>Parser boundary</h2>
        <Callout title="A limited subprocess is not a complete sandbox" tone="warning">
          <p>
            Linux CPU and address-space limits reduce blast radius, and the child has no provider
            responsibility, but it still shares the host kernel and is not a full syscall or
            filesystem isolation boundary. Treat unexpected parser crashes as security-relevant and
            require disposable least-privilege isolation before production use.
          </p>
        </Callout>
      </section>

      <section id="implementation">
        <h2>Implementation map</h2>
        <ModuleReferences
          items={[
            { path: "pdf_bridge/managers/worker.py", purpose: "Worker lifecycle, leases, phases, outbox sequencing, and replacement recovery" },
            { path: "pdf_bridge/services/extraction.py", purpose: "Parent-side parser process control and limits" },
            { path: "pdf_bridge/services/extraction_child.py", purpose: "Page-mapped pypdf extraction inside the child" },
            { path: "pdf_bridge/services/chunking.py", purpose: "Deterministic text normalization and chunking" },
            { path: "pdf_bridge/services/candidates.py", purpose: "Deterministic candidate thresholds and rank fusion" },
            { path: "pdf_bridge/services/classification.py", purpose: "Structured classifier/verifier calls and quote validation" },
            { path: "pdf_bridge/services/vector_index.py", purpose: "Active/screening collections, filters, strong writes, publication, and counts" },
            { path: "tests/test_worker_lifecycle.py", purpose: "Lease, crash, publication, ordering, and replacement regression coverage" },
          ]}
        />
      </section>
    </>
  ),
};

const platformOperator: GuidePage = {
  category: "Role guide",
  title: "Platform operator",
  summary:
    "Run the supported single-process POC, protect its storage and credentials, monitor worker dependencies, and execute recovery or the coordinated empty reset.",
  facts: [
    { term: "Supported topology", detail: "Linux, Docker Compose, SQLite, one Uvicorn process" },
    { term: "Worker", detail: "Lifespan-owned with two slots" },
    { term: "Recovery unit", detail: "SQLite, canonical PDFs, private analysis storage, and Qdrant snapshots" },
  ],
  toc: [
    { id: "topology", label: "Topology" },
    { id: "startup", label: "Startup and health" },
    { id: "storage", label: "Storage and backups" },
    { id: "recovery", label: "Operational recovery" },
    { id: "import", label: "Historical import" },
    { id: "reset", label: "Coordinated reset" },
    { id: "monitor", label: "Daily checks" },
    { id: "implementation", label: "Implementation map" },
  ],
  content: (
    <>
      <section id="topology">
        <h2>Topology</h2>
        <p>
          Run exactly one Uvicorn process. SQLite and process-local collection locks are deliberate
          POC constraints; a second application process can violate collection freshness and worker
          ownership. Horizontal scaling requires a different database and distributed coordination
          design.
        </p>
        <DocumentationTable
          headings={["Dependency", "Bridge access", "Exposure rule"]}
          rows={[
            ["ClamAV 1.5.3", "Private INSTREAM and PING", "Do not publish port 3310 or mount canonical storage"],
            ["Embedding endpoint", "Private OpenAI-compatible /embeddings", "Pin model ID and dimension; use a separate secret"],
            ["Classifier endpoint", "Private OpenAI-compatible /chat/completions", "Pin independent classifier and verifier IDs"],
            ["Qdrant 1.18.1", "Administrative API key", "Keep on a private network; retrieval receives active-only scoped JWTs"],
            ["Retrieval service", "Stable authenticated search request", "Use organization-managed TLS when traffic leaves the trusted host"],
          ]}
        />
      </section>

      <section id="startup">
        <h2>Startup and health</h2>
        <p>
          Startup validates settings, creates protected storage directories, applies the reviewed
          migration, validates configured collection references, then starts the worker during app
          lifespan. Shutdown stops the worker before closing provider clients and disposing the
          database engine.
        </p>
        <DocumentationTable
          headings={["Endpoint", "Meaning"]}
          rows={[
            [<code key="l">/api/v1/health/live</code>, "The process can answer"],
            [<code key="r">/api/v1/health/ready</code>, "SQLite, writable storage directories, and ClamAV PING are healthy"],
            [<code key="d">/api/v1/health/dependencies</code>, "The same detailed dependency body for restricted operator diagnosis"],
          ]}
        />
        <p>
          Readiness does not prove ClamAV signature freshness, worker progress, model availability,
          Qdrant integrity, or retrieval conformance. Monitor those separately.
        </p>
      </section>

      <section id="storage">
        <h2>Storage and backups</h2>
        <CodeBlock>{"<storage-root>/\n  catalog.sqlite3\n  objects/\n  analysis/\n  temporary/\n  quarantine/"}</CodeBlock>
        <p>
          UUID-derived object keys, not display filenames, determine paths. Back up SQLite,
          canonical objects, and compressed analysis artifacts as one consistent unit. Record the
          application version, migration revision, and pipeline fingerprint. Capture Qdrant through
          its supported snapshot procedure and record the active alias-to-epoch map.
        </p>
      </section>

      <section id="recovery">
        <h2>Operational recovery</h2>
        <ol className="procedure">
          <li>Confirm the prior process or worker thread is gone before allowing an expired lease to recover.</li>
          <li>Identify the document, operation, phase, attempt, replacement, and pending outbox IDs.</li>
          <li>Repair storage, ClamAV, provider, Qdrant, alias, or capacity faults without editing lifecycle rows.</li>
          <li>Use the supported retry endpoint for retained failed work.</li>
          <li>Reconcile exact active and screening counts, payload flags, aliases, artifacts, and catalog state.</li>
        </ol>
      </section>

      <section id="import">
        <h2>Historical import</h2>
        <p>
          <code>pdf-bridge import-manifest</code> accepts strict manifest version 3. Dry run bounds,
          hashes, validates, and scans every source without creating state. Reviewed apply promotes
          bytes in one compensated transaction and creates ordinary <code>ANALYZING</code> documents
          with <code>ANALYZE</code> operations.
        </p>
        <CodeBlock>{"pdf-bridge import-manifest historical-v3.json \\\n  --source-root /approved/source-pdfs \\\n  --dry-run \\\n  --actor-id change-1234\n\npdf-bridge import-manifest historical-v3.json \\\n  --source-root /approved/source-pdfs \\\n  --apply \\\n  --actor-id change-1234"}</CodeBlock>
        <p>
          Import does not synthesize active rows and is not a backup. Follow every imported item
          through the normal analysis, review, and publication path. On apply or commit failure,
          promoted objects are compensated and any failed removals are reported explicitly.
        </p>
      </section>

      <section id="reset">
        <h2>Coordinated reset</h2>
        <Callout title="This cutover is empty-only" tone="warning">
          <p>
            There is no dual API or compatible in-place migration. Preserve source PDFs externally,
            stop all traffic and index writers, wipe the disposable catalog, Bridge storage, and
            old active and screening collections, then deploy Bridge and retrieval together.
          </p>
        </Callout>
        <ol className="procedure">
          <li>Inventory, checksum, and externally preserve every source PDF and intended collection.</li>
          <li>Stop operators, Bridge, retrieval, imports, parser children, and every index writer.</li>
          <li>Wipe SQLite and migration state, Bridge canonical/private-analysis storage, and all old Qdrant collections.</li>
          <li>Deploy the empty migration, one Bridge process, pinned Qdrant, and the updated retrieval service.</li>
          <li>Issue active-only retrieval JWTs that deny screening, then reingest through upload or manifest version 3.</li>
          <li>Reconcile SQL, storage, aliases, point counts, retrieval, and the evaluation fingerprint before reopening traffic.</li>
        </ol>
      </section>

      <section id="monitor">
        <h2>Daily checks</h2>
        <ul className="check-list">
          <li>Readiness, ClamAV signature freshness, scan failures, and capacity.</li>
          <li>Old <code>RUNNING</code> leases, repeated retries, bounded operation errors, and stalled phases.</li>
          <li>Embedding and classifier latency, authentication, model IDs, dimensions, and invalid outputs.</li>
          <li>Qdrant authentication failures, alias or epoch drift, and exact active/screening counts.</li>
          <li>Retrieval filters, schema version, active-only scopes, and negative screening-access tests.</li>
          <li>Consistent backups, restore drills, secret rotation, and parser isolation alerts.</li>
        </ul>
      </section>

      <section id="implementation">
        <h2>Implementation map</h2>
        <ModuleReferences
          items={[
            { path: "pdf_bridge/core/config.py", purpose: "Strict environment settings and cross-field validation" },
            { path: "pdf_bridge/app.py", purpose: "Lifespan ownership of database, clients, and worker" },
            { path: "pdf_bridge/services/health.py", purpose: "Database, storage, and scanner readiness probes" },
            { path: "pdf_bridge/services/storage.py", purpose: "Private storage layout and atomic promotion" },
            { path: "pdf_bridge/services/artifacts.py", purpose: "Compressed analysis artifacts, purge, and canonical audit hash" },
            { path: "pdf_bridge/services/historical_import.py", purpose: "Version-3 validation, scanning, and compensation" },
            { path: "docs/runbook.md", purpose: "Detailed recovery, reset, smoke test, and incident procedures" },
          ]}
        />
      </section>
    </>
  ),
};

const retrievalIntegrator: GuidePage = {
  category: "Role guide",
  title: "Retrieval service integrator",
  summary:
    "Implement the stable grouped search contract over active Qdrant aliases while enforcing publication, schema, UUID, and collection correlation.",
  facts: [
    { term: "Primary surface", detail: "External search API and active Qdrant aliases" },
    { term: "Modes", detail: "keyword, semantic, hybrid" },
    { term: "Forbidden", detail: "Screening access and lifecycle mutation" },
  ],
  toc: [
    { id: "before", label: "Before you begin" },
    { id: "query", label: "Query behavior" },
    { id: "contract", label: "Request and response" },
    { id: "guardrails", label: "Index guardrails" },
    { id: "failure", label: "Failure behavior" },
    { id: "ownership", label: "Where ownership ends" },
    { id: "implementation", label: "Implementation map" },
  ],
  content: (
    <>
      <section id="before">
        <h2>Before you begin</h2>
        <p>
          Receive a collection-scoped, read-only Qdrant JWT for the active aliases you serve. Do
          not accept the Bridge administrative key, a global read-only key, collection management
          permissions, or access to <code>pdf-bridge-screening-v1</code>.
        </p>
      </section>

      <section id="query">
        <h2>Query behavior</h2>
        <DocumentationTable
          headings={["Mode", "Vector", "Ranking"]}
          rows={[
            [<code key="k">keyword</code>, <code key="kb">content_bm25</code>, "Native sparse BM25"],
            [<code key="s">semantic</code>, <code key="sd">content_dense</code>, "Dense similarity"],
            [<code key="h">hybrid</code>, "Both named vectors", "Reciprocal rank fusion; never mix raw scores"],
          ]}
        />
        <p>
          Every query targets only the requested stable collection alias and filters both
          <code>published=true</code> and the current <code>schema_version</code>. Return Bridge
          <code>document_id</code> and <code>collection_key</code> with each hit.
        </p>
      </section>

      <section id="contract">
        <h2>Request and response contract</h2>
        <CodeBlock>{`{
  "query": "retention policy",
  "mode": "hybrid",
  "collections": ["internal"],
  "include_hits": true,
  "page": 1,
  "page_size": 20
}`}</CodeBlock>
        <ul>
          <li>Echo the exact query and mode.</li>
          <li>Return exactly one unique group for each requested collection and no extra groups.</li>
          <li>When <code>include_hits=false</code>, return totals with empty hit arrays.</li>
          <li>When hits are requested, honor the exact page cardinality implied by total, page, and page size.</li>
          <li>Keep scores finite, snippets bounded, and document IDs unique inside each group.</li>
        </ul>
      </section>

      <section id="guardrails">
        <h2>Index guardrails</h2>
        <ul>
          <li>Never list, query, or infer content from the private screening collection.</li>
          <li>Do not treat a dense-only or sparse-only point as published content.</li>
          <li>During <code>DELETING</code> or <code>DELETE_FAILED</code>, active points remain eligible until verified removal succeeds.</li>
          <li>Reject points with a missing or mismatched collection, schema, publication flag, or Bridge UUID.</li>
          <li>Exercise positive and negative collection tests after every alias, token, schema, or model change.</li>
        </ul>
      </section>

      <section id="failure">
        <h2>Failure behavior</h2>
        <p>
          The Bridge rejects the complete retrieval response if it is malformed, uncorrelated,
          over its size bound, or contains unknown, inactive, pending, tombstoned, duplicate, or
          cross-collection UUIDs. Do not depend on partial acceptance or a catalog metadata fallback.
        </p>
      </section>

      <section id="ownership">
        <h2>Where ownership ends</h2>
        <p>
          Retrieval owns query execution and ranking over active aliases. PDF Bridge owns document
          state, collection placement, index mutation, and screening. The chatbot manager owns
          end-user authorization and must constrain the collections it sends to retrieval.
        </p>
      </section>

      <section id="implementation">
        <h2>Implementation map</h2>
        <ModuleReferences
          items={[
            { path: "pdf_bridge/contracts/schemas.py", purpose: "Stable search request, group, hit, and response shapes" },
            { path: "pdf_bridge/services/search.py", purpose: "Bounded transport and exact request/response correlation" },
            { path: "pdf_bridge/services/catalog.py", purpose: "Catalog eligibility and UUID/collection validation" },
            { path: "pdf_bridge/services/vector_index.py", purpose: "Active/screening layout, payload schema, and query filters" },
            { path: "docs/architecture.md", purpose: "Named vectors, aliases, outbox, and retrieval contract" },
          ]}
        />
      </section>
    </>
  ),
};

const chatbotIntegrator: GuidePage = {
  category: "Role guide",
  title: "Chatbot integrator",
  summary:
    "Apply authenticated server-side collection policy before retrieval and preserve Bridge correlation without exposing operator or screening capabilities.",
  facts: [
    { term: "Primary surface", detail: "Authenticated chatbot backend" },
    { term: "Owns", detail: "User policy and allowed-collection intersection" },
    { term: "Must not receive", detail: "Bridge admin or Qdrant screening credentials" },
  ],
  toc: [
    { id: "boundary", label: "Boundary" },
    { id: "normal-path", label: "Normal path" },
    { id: "guardrails", label: "Guardrails" },
    { id: "acceptance", label: "Acceptance evidence" },
    { id: "failure", label: "Failure behavior" },
    { id: "ownership", label: "Where ownership ends" },
  ],
  content: (
    <>
      <section id="boundary">
        <h2>Boundary</h2>
        <p>
          Collection audience labels are descriptive operator metadata, not authorization. Derive
          allowed collections from an authenticated user and server-side policy, intersect that set
          with the requested set, and pass only the result to retrieval. Never trust a browser-provided
          collection list by itself.
        </p>
      </section>

      <section id="normal-path">
        <h2>Normal path</h2>
        <ol className="procedure">
          <li>Authenticate the user and load policy from an authoritative server-side source.</li>
          <li>Intersect policy collections with the requested collections; reject an empty or unauthorized scope.</li>
          <li>Call retrieval with keyword, semantic, or hybrid mode and only the authorized active aliases.</li>
          <li>Preserve <code>document_id</code> and <code>collection_key</code> through citations and diagnostics.</li>
          <li>Apply answer-generation policy without weakening retrieval’s active-only filters.</li>
        </ol>
      </section>

      <section id="guardrails">
        <h2>Guardrails</h2>
        <ul>
          <li>Do not call Bridge operator endpoints on behalf of chatbot users.</li>
          <li>Do not infer authorization from filenames, document UUIDs, audience labels, or snippets.</li>
          <li>Do not grant the chatbot Bridge’s Qdrant administrative key or any screening permission.</li>
          <li>Keep user identity, query, authorized scope, response groups, and citations correlated in protected audit evidence.</li>
          <li>Fail closed if policy, retrieval, or correlation is unavailable.</li>
        </ul>
      </section>

      <section id="acceptance">
        <h2>Acceptance evidence</h2>
        <ul className="check-list">
          <li>An internal-only user can retrieve an internal positive test topic.</li>
          <li>A customer-only user receives explicit zero for the same internal topic.</li>
          <li>A mixed request is reduced to the server-authorized intersection.</li>
          <li>A forged cross-collection hit or unknown UUID fails the complete response.</li>
          <li>The retrieval credential cannot list or query screening content.</li>
          <li>Pending, replaced, cancelled, rejected, and deleted content never appears in answers.</li>
        </ul>
      </section>

      <section id="failure">
        <h2>Failure behavior</h2>
        <p>
          Policy failure is an authorization failure, not a reason to broaden scope. Retrieval
          failure is a dependency failure, not a reason to search private analysis or Bridge
          metadata. Preserve bounded identifiers and error categories without logging query content,
          snippets, provider secrets, or private PDF text beyond approved policy.
        </p>
      </section>

      <section id="ownership">
        <h2>Where ownership ends</h2>
        <p>
          The chatbot manager owns authenticated user policy and answer behavior. Retrieval owns
          active search. PDF Bridge owns intake, lifecycle, and catalog truth. Changes to any one of
          those boundaries require coordinated conformance tests; none may silently assume another’s
          authorization responsibility.
        </p>
      </section>
    </>
  ),
};

const securityReviewer: GuidePage = {
  category: "Role guide",
  title: "Security reviewer",
  summary:
    "Review the complete upload-to-retrieval trust path, including parser containment, model output, private screening data, credentials, and deletion evidence.",
  facts: [
    { term: "Scope", detail: "File, process, provider, vector, identity, and retrieval boundaries" },
    { term: "Current claim", detail: "Restricted-network proof of concept" },
    { term: "Primary artifact", detail: "docs/security.md enterprise gate" },
  ],
  toc: [
    { id: "review-path", label: "Review path" },
    { id: "implemented", label: "Implemented controls" },
    { id: "residual", label: "Residual risk" },
    { id: "gates", label: "Deployment gates" },
    { id: "oss", label: "OSS decisions" },
    { id: "incident", label: "Incident path" },
    { id: "implementation", label: "Evidence map" },
  ],
  content: (
    <>
      <section id="review-path">
        <h2>Review path</h2>
        <ol className="procedure">
          <li>Trace bytes through body limits, generated quarantine paths, signature validation, server-side SHA-256, ClamAV, and atomic promotion.</li>
          <li>Trace the parser child through Linux resource limits, page/text/chunk gates, protected artifacts, and terminal rejection cleanup.</li>
          <li>Trace untrusted PDF excerpts through embedding and two tool-free structured-output model calls, including citation validation.</li>
          <li>Trace screening and active points through Qdrant credentials, aliases, payload filters, outbox ordering, and exact-count verification.</li>
          <li>Trace Keep, Replace, Cancel, retry, deletion, purge, tombstones, and the content-free analysis hash.</li>
          <li>Trace trusted-header identity, CSRF/origin controls, operator capability, retrieval JWT scope, and chatbot authorization.</li>
        </ol>
      </section>

      <section id="implemented">
        <h2>Implemented controls</h2>
        <ul>
          <li>Uploads are bounded, streamed, server-hashed, format-allowlisted, and ClamAV-scanned before canonical promotion.</li>
          <li>Exact duplicate rejection is scoped to retained bytes in the selected collection; user filenames never determine storage paths.</li>
          <li>Parser and text-quality failures reject without override and purge retained content.</li>
          <li>Browser mutations require session, same-origin/CSRF checks, and actor attribution.</li>
          <li>Decisions are immutable, revision-bound, target-constrained, and idempotent.</li>
          <li>Model findings are explanation-only; invalid citations or structured output become visible advisory incompleteness.</li>
          <li>Screening is private, active points require publication/schema flags, and retrieval receives scoped read-only JWTs.</li>
          <li>Outbox mutations are idempotent, strongly ordered, waited, and verified by exact counts.</li>
          <li>Cancellation and deletion purge private content after a canonical content-free audit hash is recorded.</li>
        </ul>
      </section>

      <section id="residual">
        <h2>Residual risk</h2>
        <DocumentationTable
          headings={["Risk", "Current limit", "Required direction"]}
          rows={[
            ["PDF parser compromise", "A clean scan and resource-limited child are not a safety proof", "Disposable least-privilege sandbox with no network and an owned patch response"],
            ["Prompt injection", "Quoted untrusted text and citation checks do not classify prompt injection", "Threat review; never treat model prose as executable or mutation authority"],
            ["Anonymous operator access", "anonymous-poc attributes a session but proves no identity", "Network isolation or trusted-header SSO; add authorization if operators should differ"],
            ["Qdrant admin key", "Disclosure exposes active and screening data and alias mutation", "Secret manager, restricted network, monitored denial, coordinated rotation, and regenerated JWTs"],
            ["Provider outage override", "Keep may accept incomplete advisory analysis", "Publication still requires complete dense and BM25 points; alert on retained pending work"],
            ["Single process", "SQLite and local locks are not highly available", "Redesign database and coordination before multiple replicas"],
          ]}
        />
      </section>

      <section id="gates">
        <h2>Deployment gates</h2>
        <ul className="check-list">
          <li>Organization-managed TLS, trusted proxy CIDRs, enterprise SSO, host validation, and operator authorization decision.</li>
          <li>Separate secret ownership and rotation for session, Qdrant, embedding, LLM, and retrieval credentials.</li>
          <li>Active-only Qdrant JWT scopes, explicit screening denial, network restriction, audit, and alias reconciliation.</li>
          <li>Disposable parser sandbox, malicious-PDF testing, ClamAV freshness, and encrypted-document policy.</li>
          <li>Provider data-use, retention, residency, authentication, logging, and model-change policy.</li>
          <li>Encrypted storage, backup, legal hold, retention, verified deletion, and restore drills.</li>
          <li>External retrieval conformance for named vectors, hybrid RRF, publication/schema filters, and catalog correlation.</li>
          <li>Labeled same-collection evaluation corpus with candidate recall at least 0.98 and recorded fingerprints.</li>
        </ul>
      </section>

      <section id="oss">
        <h2>OSS decisions</h2>
        <p>
          Playwright 1.61.0 remains development-only browser tooling; inventory its downloaded
          browser separately and make the browser suite a required CI check. ClamAV 1.5.3 remains a
          fail-closed POC gate, not proof that arbitrary PDFs are safe. Track signature freshness,
          non-LTS lifecycle, image licensing, and live clean/EICAR acceptance.
        </p>
      </section>

      <section id="incident">
        <h2>Incident path</h2>
        <ol className="procedure">
          <li>Contain uploads, worker, provider, Qdrant, and retrieval traffic without deleting evidence.</li>
          <li>Preserve protected logs, document/analysis/operation/replacement/outbox IDs, timestamps, hashes, and fingerprints.</li>
          <li>Rotate affected credentials and regenerate Qdrant JWTs after an administrative-key change.</li>
          <li>Use SQL as catalog authority; quarantine inconsistent points and externally preserve verified source PDFs.</li>
          <li>Correct the cause, rebuild through supported workflows, and run positive, negative, replacement, purge, and access-denial tests.</li>
        </ol>
      </section>

      <section id="implementation">
        <h2>Evidence map</h2>
        <ModuleReferences
          items={[
            { path: "docs/security.md", purpose: "Implemented controls, residual risks, and mandatory enterprise gate" },
            { path: "pdf_bridge/http/security.py", purpose: "Session actor, CSRF, origin, trusted peer, and identity-header checks" },
            { path: "pdf_bridge/services/scanner.py", purpose: "Fail-closed ClamAV transport" },
            { path: "pdf_bridge/services/extraction.py", purpose: "Parser child limits and timeout enforcement" },
            { path: "pdf_bridge/services/classification.py", purpose: "Untrusted-text prompting, strict schema, and quote validation" },
            { path: "pdf_bridge/services/artifacts.py", purpose: "Private compressed artifacts, purge, and audit hash" },
            { path: "pdf_bridge/services/vector_index.py", purpose: "Qdrant access patterns, payload filters, and exact verification" },
          ]}
        />
      </section>
    </>
  ),
};

const codeMaintainer: GuidePage = {
  category: "Role guide",
  title: "Code maintainer",
  summary:
    "Change the one-way package layers, durable worker, strict contracts, and compensating workflows while preserving intake and retrieval invariants.",
  facts: [
    { term: "Architecture", detail: "One-way package layers with manager-owned transactions" },
    { term: "Concurrency", detail: "Sync handlers plus one lifespan-owned worker" },
    { term: "Rule", detail: "Persist intent before external mutation" },
  ],
  toc: [
    { id: "orientation", label: "Orient in the package" },
    { id: "flows", label: "Core flows" },
    { id: "change-map", label: "Choose the change point" },
    { id: "invariants", label: "Invariants" },
    { id: "verification", label: "Verification" },
    { id: "documentation", label: "Documentation standard" },
  ],
  content: (
    <>
      <section id="orientation">
        <h2>Orient in the package</h2>
        <CodeBlock>{"app → controllers → managers → services\n                      ↓          ↓\n                 http/contracts/presentation/persistence/core"}</CodeBlock>
        <ul>
          <li><code>app.py</code> composes settings, database, clients, scanner, worker, middleware, and shutdown ownership.</li>
          <li>Controllers bind Litestar or Typer inputs and translate safe errors; they do not construct SQL.</li>
          <li>Managers own locks, transaction commit/rollback, compensation, and multi-step workflow sequencing.</li>
          <li>Services own reusable domain rules and I/O without importing transport or managers.</li>
          <li>Contracts are strict public shapes; persistence models and migrations own durable representation.</li>
          <li>Presentation is stateless and never calls services or issues SQL.</li>
          <li>Blocking controller work declares <code>sync_to_thread=True</code>; worker I/O runs in its owned threads.</li>
        </ul>
      </section>

      <section id="flows">
        <h2>Core flows</h2>
        <DocumentationTable
          headings={["Experience", "Trace"]}
          rows={[
            ["Upload", "controllers/api.py → managers/document.py → services/document.py + intake.py + storage.py + scanner.py"],
            ["Worker operation", "app.py lifespan → managers/worker.py → extraction/analysis/providers/vector_index/artifacts services"],
            ["Review decision", "controllers/api.py → managers/document.py → services/intake.py → durable operation wakeup"],
            ["Catalog and evidence", "controllers/api.py → managers/catalog.py → services/catalog.py → presentation serializers"],
            ["Search", "controllers/api.py → managers/search.py → services/search.py + catalog.py"],
            ["Historical import", "controllers/admin_cli.py → managers/importing.py → services/historical_import.py"],
          ]}
        />
      </section>

      <section id="change-map">
        <h2>Choose the change point</h2>
        <p>
          Put a rule in the lowest layer that owns it. Transport parsing stays in a controller;
          transaction, lock, compensation, and workflow sequencing stay in a manager; reusable
          validation, lifecycle rules, provider calls, and filesystem/index I/O stay in services;
          resource lifecycle stays in the composition root. Persisted fields require a reviewed
          model and Alembic change together.
        </p>
      </section>

      <section id="invariants">
        <h2>Invariants</h2>
        <ul>
          <li>Only exact same-collection bytes hard-fail as a duplicate; every semantic finding remains advisory.</li>
          <li>Parser and text-quality rejection is terminal and non-overridable; cleanup removes all retained content.</li>
          <li>No database transaction spans parser, HTTP provider, or Qdrant work.</li>
          <li>Only complete dense plus BM25 points may be published; screening is never retrieval-eligible.</li>
          <li>Decisions bind an exact analysis revision and replacement target eligibility is checked live.</li>
          <li>Verified old deletion precedes every new active replacement write.</li>
          <li>External mutations are idempotent, outbox-backed, strongly ordered, waited, and exactly counted.</li>
          <li>Tombstones retain content-free hashes and metadata, never source, excerpts, vectors, prompts, or raw output.</li>
          <li>One Uvicorn process remains the only supported runtime.</li>
        </ul>
      </section>

      <section id="verification">
        <h2>Verification</h2>
        <ul className="check-list">
          <li>Run Ruff, architecture checks, unit tests, and the full pytest suite.</li>
          <li>Exercise same- versus cross-collection duplicates, filename families, chunk stability, thresholds, rank fusion, and quote validation.</li>
          <li>Exercise lease expiry, provider outages, stale decisions, retry idempotency, and every outbox crash boundary.</li>
          <li>Prove active/screening filters, named vectors, strong waits, exact counts, and denial of screening access.</li>
          <li>Prove replacement call ordering and retryability after old deletion.</li>
          <li>Prove cancellation/deletion purge while content-free audit hashes remain.</li>
          <li>Run browser coverage for restoration, evidence pagination, decisions, multiple files, accessibility, and narrow layouts.</li>
        </ul>
      </section>

      <section id="documentation">
        <h2>Documentation standard</h2>
        <p>
          Update architecture, configuration, operations, security, import, README, and this site
          whenever a lifecycle, endpoint, provider, Qdrant payload, credential, or recovery rule
          changes. Remove superseded contracts instead of documenting parallel paths. State the
          parser limitation plainly: a resource-limited subprocess is not a complete sandbox.
        </p>
      </section>
    </>
  ),
};

export const roleGuides: Record<string, GuidePage> = {
  "library-operator": libraryOperator,
  "semantic-intake-owner": semanticIntakeOwner,
  "platform-operator": platformOperator,
  "retrieval-integrator": retrievalIntegrator,
  "chatbot-integrator": chatbotIntegrator,
  "security-reviewer": securityReviewer,
  "code-maintainer": codeMaintainer,
};
