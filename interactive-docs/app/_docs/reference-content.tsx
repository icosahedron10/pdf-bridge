import { Callout, CodeBlock, DocumentationTable } from "./components";
import type { GuidePage } from "./role-content";

const lifecycle: GuidePage = {
  category: "Reference",
  title: "Lifecycle states",
  summary:
    "Canonical document, worker operation, visible phase, decision, replacement, and terminal cleanup states for durable semantic intake.",
  facts: [
    { term: "Catalog authority", detail: "SQLite" },
    { term: "Work owner", detail: "Internal two-slot worker" },
    { term: "Terminal tombstones", detail: "REJECTED, CANCELLED, DELETED" },
  ],
  toc: [
    { id: "documents", label: "Document states" },
    { id: "operations", label: "Operation states" },
    { id: "phases", label: "Visible phases" },
    { id: "decisions", label: "Decisions" },
    { id: "replacement", label: "Replacement states" },
    { id: "cleanup", label: "Cleanup and tombstones" },
  ],
  content: (
    <>
      <section id="documents">
        <h2>Document states</h2>
        <DocumentationTable
          headings={["State", "Meaning", "Retrieval"]}
          rows={[
            [<code key="a">ANALYZING</code>, "A durable analysis operation is extracting or comparing", "Blocked"],
            [<code key="r">REVIEW_REQUIRED</code>, "Advisory evidence or analysis incompleteness awaits Keep, Replace, or Cancel", "Blocked"],
            [<code key="i">INGESTING</code>, "Complete dense and BM25 points are being prepared and published", "Blocked until verified publication"],
            [<code key="if">INGEST_FAILED</code>, "Publication failed with retained retryable work", "Blocked"],
            [<code key="id">INGESTED</code>, "Active points are complete, published, and count-verified", "Eligible"],
            [<code key="rp">REPLACING</code>, "Safe replacement is preparing new content, deleting old, or publishing new", "New document blocked"],
            [<code key="rf">REPLACE_FAILED</code>, "A replacement step failed and remains retryable", "New document blocked; old eligibility depends on durable phase"],
            [<code key="dg">DELETING</code>, "Active deletion is running", "Eligible until verified active removal"],
            [<code key="df">DELETE_FAILED</code>, "Active removal failed and may be retried", "Eligible while old points remain"],
            [<code key="cp">CLEANUP_PENDING</code>, "Source, analysis, or private points still require purge", "Blocked"],
            [<code key="cf">CLEANUP_FAILED</code>, "A purge step failed and remains retryable", "Blocked"],
            [<code key="x">REJECTED · CANCELLED · DELETED</code>, "Content-free terminal audit tombstone", "Blocked"],
          ]}
        />
      </section>

      <section id="operations">
        <h2>Operation states</h2>
        <p>
          Work types are <code>ANALYZE</code>, <code>INGEST</code>, <code>DELETE</code>, and
          <code>CLEANUP</code>. Each attempt uses the same five-state operation machine.
        </p>
        <DocumentationTable
          headings={["State", "Meaning", "Recovery rule"]}
          rows={[
            [<code key="q">QUEUED</code>, "Durable and eligible for worker polling", "The worker may claim it under a lease"],
            [<code key="r">RUNNING</code>, "Owned by a worker ID with lease and heartbeat", "Ordinary polling reclaims an expired lease after the old owner is gone"],
            [<code key="s">SUCCEEDED</code>, "Every required durable and external step completed", "Terminal attempt"],
            [<code key="f">FAILED</code>, "The attempt ended with a bounded safe error", "A supported retry creates a new attempt when retained work permits it"],
            [<code key="c">CANCELLED</code>, "The attempt was superseded by a lifecycle mutation", "Terminal attempt; follow the current document operation"],
          ]}
        />
      </section>

      <section id="phases">
        <h2>Visible phases</h2>
        <DocumentationTable
          headings={["Phase", "Operator meaning"]}
          rows={[
            [<code key="q">QUEUED</code>, "Accepted durably and waiting for a worker slot"],
            [<code key="e">EXTRACTING</code>, "The parser child is extracting and enforcing content budgets"],
            [<code key="c">COMPARING</code>, "Chunks, providers, active/screening search, candidates, and explanations are running"],
            [<code key="a">AWAITING_DECISION</code>, "Paginated advisory evidence is ready for an operator"],
            [<code key="d">DELETING_EXISTING</code>, "Replacement is removing and verifying the old active document"],
            [<code key="i">INGESTING</code>, "Complete new points are being written, published, and verified"],
            [<code key="u">CLEANING_UP</code>, "Source, analysis artifacts, or index points are being purged"],
            [<code key="x">COMPLETE</code>, "The attempt reached its intended durable result"],
          ]}
        />
      </section>

      <section id="decisions">
        <h2>Decisions</h2>
        <DocumentationTable
          headings={["Action", "Target rule", "Next work"]}
          rows={[
            [<code key="k">KEEP</code>, "Target forbidden; exact analysis revision required", "Records advisory override and queues INGEST"],
            [<code key="r">REPLACE</code>, "Exactly one live same-collection INGESTED candidate required", "Creates replacement workflow and queues INGEST"],
            [<code key="c">CANCEL</code>, "Target forbidden; exact analysis revision required", "Queues CLEANUP and terminal CANCELLED tombstone"],
          ]}
        />
        <p>
          Decisions are immutable and idempotent. They carry no rationale. Reviews never expire,
          but a changed analysis revision or collection epoch makes a submitted view stale.
        </p>
      </section>

      <section id="replacement">
        <h2>Replacement states</h2>
        <DocumentationTable
          headings={["State", "Ordering guarantee"]}
          rows={[
            [<code key="p">PREPARING</code>, "New dense and sparse artifacts may be prepared, but the old document remains active"],
            [<code key="d">DELETING_OLD</code>, "No new active write is permitted until old active points count exactly zero and artifacts are purged"],
            [<code key="i">INGESTING_NEW</code>, "The old document is a DELETED tombstone; the availability gap persists until new publication succeeds"],
            [<code key="s">SUCCEEDED</code>, "New points are published and verified; new screening points are absent"],
            [<code key="f">FAILED</code>, "The durable phase records whether the old document remains or is already gone"],
          ]}
        />
      </section>

      <section id="cleanup">
        <h2>Cleanup and tombstones</h2>
        <p>
          Rejection, cancellation, replacement of the old document, and deletion converge on the
          same purge obligations: canonical PDF, compressed analysis artifacts, normalized analysis
          rows, active points, and screening points must be absent. Before purge, Bridge hashes a
          canonical analysis record containing content, pipeline, decision, actor, target, and time
          fingerprints.
        </p>
        <Callout title="Terminal does not mean forgotten">
          <p>
            Tombstones retain UUID, collection, lifecycle timestamps, actor metadata, and the
            content-free analysis hash. They retain no source bytes, excerpts, vectors, prompts, or
            raw model output.
          </p>
        </Callout>
      </section>
    </>
  ),
};

const intakeApi: GuidePage = {
  category: "Reference",
  title: "Intake API",
  summary:
    "The atomic /api/v1 contract for metadata preflight, accepted uploads, durable polling, paginated evidence, decisions, retries, cancellation, and active deletion.",
  facts: [
    { term: "Base path", detail: "/api/v1" },
    { term: "Upload result", detail: "202 Accepted with durable URLs" },
    { term: "Errors", detail: "RFC 9457-style problem details" },
  ],
  toc: [
    { id: "security", label: "Security and idempotency" },
    { id: "preflight", label: "Preflight" },
    { id: "upload", label: "Upload" },
    { id: "polling", label: "Polling and restoration" },
    { id: "analysis", label: "Analysis evidence" },
    { id: "decision", label: "Decision" },
    { id: "retry-delete", label: "Retry and delete" },
  ],
  content: (
    <>
      <section id="security">
        <h2>Security and idempotency</h2>
        <p>
          These routes use the browser session actor. Mutations require same-origin/CSRF checks;
          upload and decision requests also require an 8–128 character <code>Idempotency-Key</code>.
          Request models are strict: unknown fields fail validation. A reused key replays only the
          identical operation and conflicts if material input changes.
        </p>
        <p>
          Errors use a stable problem body with <code>status</code>, <code>code</code>,
          <code>title</code>, <code>detail</code>, and request correlation. Exact duplicate conflicts
          may include the matched same-collection document.
        </p>
      </section>

      <section id="preflight">
        <h2>Preflight</h2>
        <CodeBlock>{`POST /api/v1/uploads/preflight
Content-Type: application/json

{
  "filename": "Product guide June 2026.pdf",
  "size_bytes": 184320,
  "collection_key": "customer"
}`}</CodeBlock>
        <p>
          A successful response returns <code>normalized_filename</code> and zero or more typed
          filename-family, token-set, or Jaro-Winkler warnings. Each warning includes its similarity,
          shared tokens, and matched document snapshot. Warnings are advisory; the request does not
          reserve a name or approve file bytes.
        </p>
      </section>

      <section id="upload">
        <h2>Upload</h2>
        <CodeBlock>{`curl -X POST https://pdf-bridge.internal/api/v1/uploads \\
  -H "Idempotency-Key: upload-01K0EXAMPLE" \\
  -F "collection_key=customer" \\
  -F "file=@Product-guide.pdf;type=application/pdf"`}</CodeBlock>
        <p>
          The synchronous path bounds and streams the multipart file, validates its display name and
          PDF signature, calculates SHA-256, scans the quarantined copy, promotes clean bytes, and
          commits one <code>ANALYZE</code> operation. It does not parse or call a model or Qdrant.
        </p>
        <DocumentationTable
          headings={["202 response field", "Meaning"]}
          rows={[
            [<code key="u">upload.upload_id</code>, "Stable upload and document UUID"],
            [<code key="d">upload.document</code>, "Collection-scoped document summary, initially ANALYZING"],
            [<code key="o">upload.operation</code>, "ANALYZE attempt with state, phase, timestamps, retryability, and bounded error"],
            [<code key="r">upload.review_required</code>, "Whether an explicit decision is currently required"],
            [<code key="p">upload.open</code>, "Whether the row belongs in the restorable upload workspace"],
            [<code key="s">upload.status_url</code>, "Canonical resource URL for polling"],
            [<code key="a">upload.analysis_url</code>, "Evidence URL once an analysis revision exists"],
            [<code key="i">idempotent_replay</code>, "True only when the same accepted upload was safely replayed"],
          ]}
        />
      </section>

      <section id="polling">
        <h2>Polling and restoration</h2>
        <DocumentationTable
          headings={["Request", "Use"]}
          rows={[
            [<code key="l">GET /uploads?open=true&amp;page=1&amp;page_size=25</code>, "Restore durable open work after refresh, browser close, or process restart"],
            [<code key="g">GET /uploads/&lt;upload_id&gt;</code>, "Poll document, latest operation, visible phase, analysis summary, decision, and replacement state"],
          ]}
        />
        <p>
          Poll all active rows together rather than starting an independent timer per file. Stop
          polling a row when <code>open=false</code>, pause for a visible decision when
          <code>review_required=true</code>, and surface <code>operation.retryable</code> for supported
          failures. Never infer completion from HTTP connection lifetime.
        </p>
      </section>

      <section id="analysis">
        <h2>Analysis evidence</h2>
        <CodeBlock>{`GET /api/v1/uploads/6e6f07b7-7cdd-4c26-a83c-feb4329ca93a/analysis?page=1&page_size=10`}</CodeBlock>
        <p>
          The response binds an exact analysis revision and paginates every deterministic candidate.
          Summary fields expose semantic/classification completeness, incomplete reasons, page and
          chunk counts, pipeline fingerprint, filename warnings, candidate counts, classified count,
          overflow, and automatic-ingestion eligibility.
        </p>
        <p>
          Each candidate identifies its active or screening source, live replacement eligibility,
          deterministic reasons, cosine/BM25 evidence, fused rank, classifier and verifier findings,
          and page-referenced incoming/candidate excerpts. Overflow candidates remain visible even
          without model classification.
        </p>
      </section>

      <section id="decision">
        <h2>Decision</h2>
        <CodeBlock>{`POST /api/v1/uploads/<upload_id>/decision
Idempotency-Key: decision-01K0EXAMPLE
Content-Type: application/json

// Keep
{"analysis_revision": 2, "action": "keep"}

// Replace
{"analysis_revision": 2, "action": "replace", "target_document_id": "27d53796-6efe-4709-bd75-d490912592ca"}

// Cancel
{"analysis_revision": 2, "action": "cancel"}`}</CodeBlock>
        <p>
          Keep and Cancel forbid <code>target_document_id</code>. Replace requires it, and the server
          rechecks that the target is a current same-collection <code>INGESTED</code> candidate. There
          is no rationale field. A stale analysis revision or collection epoch returns a conflict;
          reload the evidence rather than replaying an obsolete target.
        </p>
      </section>

      <section id="retry-delete">
        <h2>Retry and delete</h2>
        <DocumentationTable
          headings={["Request", "Rule"]}
          rows={[
            [<code key="r">POST /uploads/&lt;upload_id&gt;/retry</code>, "Creates a new attempt only for retained ANALYZING, INGEST_FAILED, REPLACE_FAILED, DELETE_FAILED, or CLEANUP_FAILED work"],
            [<code key="c">DELETE /uploads/&lt;upload_id&gt;</code>, "Cancels eligible unpublished work and queues full source, analysis, active, and screening cleanup"],
            [<code key="d">POST /documents/&lt;document_id&gt;/deletion</code>, "Queues verified removal of an INGESTED document; an optional bounded reason may be supplied"],
            [<code key="g">GET /documents/&lt;document_id&gt;</code>, "Returns current state, analysis summary, decisions, operation attempts, replacement link, and audit ledger"],
          ]}
        />
        <p>
          A Keep decision survives an ingestion retry, so a provider outage does not require a
          second semantic choice. Deletion and cancellation are asynchronous: follow the returned
          operation ID and resource state until cleanup or an explicit retryable failure completes.
        </p>
      </section>
    </>
  ),
};

const codeMap: GuidePage = {
  category: "Reference",
  title: "Code map",
  summary:
    "Layer direction, package responsibilities, request and worker flows, and the correct change points for the semantic-intake codebase.",
  facts: [
    { term: "Composition root", detail: "pdf_bridge/app.py" },
    { term: "Transaction owner", detail: "Managers" },
    { term: "External intent", detail: "SQL outbox before Qdrant mutation" },
  ],
  toc: [
    { id: "layers", label: "Layer responsibilities" },
    { id: "direction", label: "Dependency direction" },
    { id: "flows", label: "Common flows" },
    { id: "worker", label: "Worker structure" },
    { id: "change-points", label: "Change points" },
    { id: "enforcement", label: "Architecture enforcement" },
  ],
  content: (
    <>
      <section id="layers">
        <h2>Layer responsibilities</h2>
        <DocumentationTable
          headings={["Layer", "Responsibility", "Examples"]}
          rows={[
            [<code key="app">app.py</code>, "Composition, lifespan ownership, middleware, and router assembly", "Engine, clients, worker, scanner, transition lock"],
            [<code key="controllers">controllers/</code>, "Litestar/Typer binding, auth dependencies, status codes, and safe error translation", "api.py, web.py, admin_cli.py"],
            [<code key="managers">managers/</code>, "Locks, transactions, commit/rollback, compensation, and multi-step workflows", "document.py, worker.py, importing.py, search.py"],
            [<code key="services">services/</code>, "Reusable domain rules and external I/O without transport dependencies", "intake, extraction, candidates, classification, vector_index, artifacts"],
            [<code key="contracts">contracts/</code>, "Strict public request and response shapes", "schemas.py"],
            [<code key="persistence">persistence/</code>, "SQLAlchemy models, engine/session construction, and portable enums", "models.py, db.py"],
            [<code key="presentation">presentation/</code>, "Stateless serializers, view models, and themes", "api_serializers.py, view_models.py"],
            [<code key="http">http/</code>, "Problems, request context, host checks, sessions, CSRF, and trusted identity", "middleware.py, security.py"],
            [<code key="core">core/</code>, "Validated settings and logging", "config.py, logging_config.py"],
          ]}
        />
      </section>

      <section id="direction">
        <h2>Dependency direction</h2>
        <CodeBlock>{"app → controllers → managers → services\n                      ↓          ↓\n                 http/contracts/presentation/persistence/core"}</CodeBlock>
        <ul>
          <li>Services do not import Litestar, controllers, managers, or the app.</li>
          <li>Controllers do not construct SQL or own commit/rollback.</li>
          <li>Presentation does not call services or issue SQL.</li>
          <li>Package initializers do not re-export implementation symbols.</li>
          <li>Use clear functions and procedural services; introduce stateful objects only for genuine lifecycle or reusable provider responsibility.</li>
        </ul>
      </section>

      <section id="flows">
        <h2>Common flows</h2>
        <DocumentationTable
          headings={["Experience", "Trace"]}
          rows={[
            ["Browser page", "controllers/web.py → managers/web.py → services/web_page.py → presentation/templates"],
            ["Upload", "controllers/api.py → managers/document.py → services/document.py + intake/storage/scanner"],
            ["Polling/evidence", "controllers/api.py → managers/catalog.py → services/catalog.py → api_serializers.py"],
            ["Decision/retry/delete", "controllers/api.py → managers/document.py → services/intake.py → worker wakeup"],
            ["Worker", "app.py lifespan → managers/worker.py → extraction/providers/analysis/vector_index/artifacts"],
            ["Search", "controllers/api.py → managers/search.py → services/search.py + catalog.py"],
            ["Historical import", "controllers/admin_cli.py → managers/importing.py → services/historical_import.py"],
          ]}
        />
      </section>

      <section id="worker">
        <h2>Worker structure</h2>
        <p>
          <code>AnalysisWorker</code> is intentionally stateful: it owns threads, stop/wake signals,
          worker identity, provider resources, and per-collection locks. The work it invokes remains
          procedural. Short SQL transactions claim, heartbeat, and checkpoint operations; blocking
          parser, model, and Qdrant calls happen outside those transactions.
        </p>
        <p>
          Index mutations are split into durable outbox records and idempotent service calls. This
          permits replay after a crash between Qdrant apply and SQL acknowledgement without guessing
          which system is authoritative.
        </p>
      </section>

      <section id="change-points">
        <h2>Change points</h2>
        <DocumentationTable
          headings={["Change", "Primary location", "Also review"]}
          rows={[
            ["HTTP shape", "contracts/schemas.py + controllers/api.py", "serializers, OpenAPI, browser client, rendered docs"],
            ["Lifecycle or decision", "services/intake.py", "models/migration, manager transaction, worker, browser states, audit"],
            ["Parser or chunking", "services/extraction*.py + chunking.py", "fingerprint, limits, evaluation corpus, security"],
            ["Candidate or model rule", "services/candidates.py + classification.py", "analysis persistence, evidence API, thresholds, evaluation"],
            ["Qdrant schema or order", "services/vector_index.py + managers/worker.py", "outbox, epochs, retrieval, reset plan, conformance tests"],
            ["Provider or process lifecycle", "app.py + managers/worker.py", "settings, shutdown, ownership tests, runbook"],
            ["Persisted field", "persistence/models.py + migration", "purge, tombstones, serialization, empty-reset constraints"],
          ]}
        />
      </section>

      <section id="enforcement">
        <h2>Architecture enforcement</h2>
        <p>
          <code>tests/test_architecture.py</code> checks package shape, dependency direction,
          transaction ownership, service transport independence, and controller SQL absence. Worker,
          intake, analysis, upload, retrieval, persistence, and browser suites enforce behavioral
          boundaries. A module move or new state requires an intentional architecture and test update.
        </p>
      </section>
    </>
  ),
};

const configuration: GuidePage = {
  category: "Reference",
  title: "Configuration & operations",
  summary:
    "Settings, startup, worker concurrency, provider and Qdrant ownership, protected storage, backups, recovery, manifest version 3, and the empty reset.",
  facts: [
    { term: "Supported host", detail: "Linux" },
    { term: "Application processes", detail: "Exactly one" },
    { term: "Index server", detail: "Qdrant 1.18.1" },
  ],
  toc: [
    { id: "settings", label: "Settings" },
    { id: "startup", label: "Startup and health" },
    { id: "runtime", label: "Runtime ownership" },
    { id: "storage", label: "Storage and backup" },
    { id: "recovery", label: "Recovery" },
    { id: "import", label: "Historical import" },
    { id: "cutover", label: "Empty reset" },
    { id: "daily", label: "Daily checks" },
  ],
  content: (
    <>
      <section id="settings">
        <h2>Settings</h2>
        <DocumentationTable
          headings={["Concern", "Main settings", "Guardrail"]}
          rows={[
            ["Collections", <code key="1">PDF_BRIDGE_COLLECTIONS</code>, "1–50 unique path-safe keys; immutable placement and stable active alias"],
            ["Storage/database", "PDF_BRIDGE_STORAGE_ROOT, PDF_BRIDGE_DATABASE_URL", "Absolute SQLite beneath a nonsynchronized root outside the source tree"],
            ["Identity", "PDF_BRIDGE_AUTH_MODE, PDF_BRIDGE_TRUSTED_PROXY_CIDRS, PDF_BRIDGE_TRUSTED_IDENTITY_HEADER", "Trusted identity only from configured immediate peers"],
            ["Upload/scanner", "PDF_BRIDGE_MAX_UPLOAD_BYTES, PDF_BRIDGE_MAX_UPLOAD_FILES, PDF_BRIDGE_UPLOAD_CHUNK_BYTES, PDF_BRIDGE_CLAMD_*", "Upload cap no larger than INSTREAM cap; every scanner error fails closed"],
            ["Worker", "PDF_BRIDGE_WORKER_ENABLED, PDF_BRIDGE_WORKER_POLL_SECONDS, PDF_BRIDGE_WORKER_LEASE_SECONDS, PDF_BRIDGE_WORKER_HEARTBEAT_SECONDS", "Heartbeat shorter than lease; disable only for isolated tests or maintenance"],
            ["Parser", "PDF_BRIDGE_PARSE_WALL_CLOCK_SECONDS, PDF_BRIDGE_PARSE_CPU_SECONDS, PDF_BRIDGE_PARSE_MEMORY_BYTES", "Linux limits are positive defense in depth, not a complete sandbox"],
            ["Analysis", "PDF_BRIDGE_ANALYSIS_MAX_PAGES, PDF_BRIDGE_ANALYSIS_MAX_CHARACTERS, PDF_BRIDGE_ANALYSIS_MAX_CHUNKS", "Reject over budget; never truncate silently"],
            ["Embedding", "PDF_BRIDGE_EMBEDDING_API_URL, PDF_BRIDGE_EMBEDDING_API_TOKEN, PDF_BRIDGE_EMBEDDING_MODEL_ID, PDF_BRIDGE_EMBEDDING_DIMENSION, PDF_BRIDGE_EMBEDDING_TIMEOUT", "Exact model/dimension and finite correlated vectors"],
            ["Classification", "PDF_BRIDGE_LLM_API_URL, PDF_BRIDGE_LLM_API_TOKEN, PDF_BRIDGE_LLM_CLASSIFIER_MODEL, PDF_BRIDGE_LLM_VERIFIER_MODEL, PDF_BRIDGE_LLM_TIMEOUT", "Independent pinned model IDs, temperature zero, strict output"],
            ["Qdrant", "PDF_BRIDGE_QDRANT_URL, PDF_BRIDGE_QDRANT_API_KEY, PDF_BRIDGE_QDRANT_TIMEOUT", "Bridge administrative key is separate and never given to retrieval"],
            ["Retrieval", "PDF_BRIDGE_SEARCH_API_URL, PDF_BRIDGE_SEARCH_API_TOKEN, PDF_BRIDGE_SEARCH_API_TIMEOUT", "Separate token; enterprise URL must use HTTPS"],
          ]}
        />
        <p>
          Parser/chunker versions, candidate thresholds, model IDs, and embedding dimension feed the
          pipeline fingerprint. Treat a change as an evaluated reindex event, not a transparent tweak.
        </p>
      </section>

      <section id="startup">
        <h2>Startup and health</h2>
        <p>
          Pydantic validates cross-field security rules before protected directories are used. The
          container entrypoint prepares the storage root and applies the reviewed migration. App
          lifespan creates the engine and clients, validates collection references, starts the
          worker, then reverses ownership cleanly on shutdown or startup failure.
        </p>
        <DocumentationTable
          headings={["Endpoint", "Coverage"]}
          rows={[
            [<code key="l">/api/v1/health/live</code>, "Process only"],
            [<code key="r">/api/v1/health/ready</code>, "SQLite SELECT, writable root/objects/temporary/quarantine, and ClamAV PING"],
            [<code key="d">/api/v1/health/dependencies</code>, "The same detailed checks for restricted diagnostics"],
          ]}
        />
        <p>
          These endpoints do not validate signature age, parser safety, provider inference, Qdrant
          aliases/counts, or retrieval behavior. Those require operational checks.
        </p>
      </section>

      <section id="runtime">
        <h2>Runtime ownership and concurrency</h2>
        <p>
          Run exactly one Uvicorn process. Its lifespan owns one SQLAlchemy engine/session factory,
          shared retrieval and provider clients, and one two-slot <code>AnalysisWorker</code>. The
          worker owns unique identity, wake/stop signals, threads, and per-collection locks; SQL
          leases and heartbeats make interrupted operations recoverable.
        </p>
        <p>
          Blocking upload, scanner, database-backed page, and retrieval handlers use
          <code>sync_to_thread=True</code>. Worker I/O runs outside request handling and outside long
          database transactions. Multiple app processes are unsupported because their local locks
          and collection epochs cannot coordinate safely.
        </p>
      </section>

      <section id="storage">
        <h2>Storage and backup</h2>
        <CodeBlock>{"<storage-root>/\n  catalog.sqlite3\n  objects/             # canonical PDFs\n  analysis/            # compressed private analysis artifacts\n  temporary/           # historical import staging\n  quarantine/          # streamed upload copies"}</CodeBlock>
        <p>
          Stop intake and worker mutation, then back up SQLite, canonical objects, and private
          analysis storage as one recovery unit. Preserve source PDFs externally until restore and
          reindex are proven. Use supported Qdrant snapshots and record the active alias/epoch map,
          application version, migration revision, and pipeline fingerprint.
        </p>
      </section>

      <section id="recovery">
        <h2>Recovery</h2>
        <DocumentationTable
          headings={["Condition", "Required response"]}
          rows={[
            ["Expired RUNNING lease", "Confirm the previous owner is gone, restart the same single-process topology, and let polling reclaim it"],
            ["Provider outage", "Keep analysis explicitly incomplete; repair the endpoint and retry retained ingestion without another decision"],
            ["Qdrant uncertainty", "Replay the pending outbox mutation and rely on deterministic IDs plus exact counts"],
            ["INGEST_FAILED", "Repair model, dimension, authentication, alias, or capacity; retry and verify both named vectors and publication"],
            ["REPLACE_FAILED", "Use durable replacement phase to determine old-document availability; preserve delete-before-publish ordering"],
            ["CLEANUP_FAILED", "Retry until source, analysis rows/artifacts, and active/screening points are all absent"],
          ]}
        />
        <p>Never edit leases, lifecycle state, decisions, audit rows, or outbox completion by hand.</p>
      </section>

      <section id="import">
        <h2>Historical import</h2>
        <p>
          Strict manifest version 3 contains only <code>version: 3</code> and document entries with a
          relative <code>path</code>, optional display <code>filename</code>, and configured
          <code>collection_key</code>. Paths resolve beneath an explicit source root; unknown fields,
          escapes, duplicate paths, and same-collection duplicate bytes fail.
        </p>
        <CodeBlock>{`{
  "version": 3,
  "documents": [
    {"path": "customer/product-guide.pdf", "filename": "Product guide.pdf", "collection_key": "customer"},
    {"path": "internal/benefits.pdf", "collection_key": "internal"}
  ]
}`}</CodeBlock>
        <p>
          Dry run still bounds, hashes, validates, and scans. Apply uses one catalog transaction,
          compensates every promoted object on failure, and creates normal <code>ANALYZING</code>
          rows. A successful import count does not prove retrieval publication and cannot reconstruct
          analysis, decisions, audit, replacements, outbox, or tombstones.
        </p>
      </section>

      <section id="cutover">
        <h2>Empty reset</h2>
        <Callout title="No in-place compatibility path" tone="warning">
          <p>
            The semantic-intake migration applies to an empty POC. Preserve source PDFs externally,
            stop every writer, wipe disposable SQL/storage/index state, deploy Bridge and retrieval
            together, then reingest through ordinary analysis and review.
          </p>
        </Callout>
        <ol className="procedure">
          <li>Checksum each source and record its intended collection outside Bridge.</li>
          <li>Stop operator, Bridge, retrieval, import, parser, worker, and index-writer traffic.</li>
          <li>Wipe SQLite/migration state, canonical and analysis storage, and every old active/screening Qdrant collection.</li>
          <li>Deploy the empty migration, pinned Qdrant, Bridge, and active-only retrieval contract together.</li>
          <li>Configure the Bridge admin key and issue scoped retrieval JWTs that deny screening.</li>
          <li>Reingest by normal upload or manifest version 3, including every ordinary review decision.</li>
          <li>Require 0.98 candidate recall, archive dataset/pipeline fingerprints, and reconcile all stores before reopening.</li>
        </ol>
      </section>

      <section id="daily">
        <h2>Daily checks</h2>
        <ul className="check-list">
          <li>Application readiness, scanner freshness, upload failures, disk, memory, and capacity.</li>
          <li>Worker lease age, heartbeat, failed attempts, stalled phases, and pending outbox entries.</li>
          <li>Embedding and classifier availability, model identity, latency, dimension, and invalid output rate.</li>
          <li>Qdrant authentication, alias/epoch drift, active/screening exact counts, and payload schema.</li>
          <li>Retrieval publication/schema filters, keyword/dense/hybrid behavior, and screening denial.</li>
          <li>Backup consistency, restore drills, credential rotations, and unexpected parser crashes.</li>
        </ul>
      </section>
    </>
  ),
};

const searchBoundary: GuidePage = {
  category: "Reference",
  title: "Search boundary",
  summary:
    "How active aliases, named vectors, publication filters, grouped response correlation, catalog validation, and chatbot authorization keep pending content private.",
  facts: [
    { term: "Active alias", detail: "Stable collection_key" },
    { term: "Private index", detail: "pdf-bridge-screening-v1" },
    { term: "Failure model", detail: "Reject the complete response" },
  ],
  toc: [
    { id: "layout", label: "Qdrant layout" },
    { id: "modes", label: "Search modes" },
    { id: "scope", label: "Request scope" },
    { id: "correlation", label: "Response correlation" },
    { id: "catalog", label: "Catalog validation" },
    { id: "deletion", label: "Deletion behavior" },
    { id: "authorization", label: "Authorization boundary" },
    { id: "acceptance", label: "Acceptance checks" },
  ],
  content: (
    <>
      <section id="layout">
        <h2>Qdrant layout</h2>
        <p>
          Each logical collection uses a physical <code>pdf-bridge-&lt;key&gt;-v&lt;epoch&gt;</code>
          collection behind a stable alias equal to its <code>collection_key</code>. Pending and
          unpublished points live in <code>pdf-bridge-screening-v1</code>. Deterministic UUIDv5 point
          IDs and SQL outbox mutations make upsert, publish, and delete replayable.
        </p>
        <p>
          Each point carries both <code>content_dense</code> and <code>content_bm25</code>, plus schema,
          document, analysis, chunk, collection, page, text-hash, bounded-text,
          <code>published</code>, and <code>screening</code> fields. A partial vector family is never
          a complete publication.
        </p>
      </section>

      <section id="modes">
        <h2>Search modes</h2>
        <DocumentationTable
          headings={["Mode", "Execution", "Required filter"]}
          rows={[
            [<code key="k">keyword</code>, "Native BM25 over content_bm25", "published=true and current schema_version"],
            [<code key="s">semantic</code>, "Dense similarity over content_dense", "published=true and current schema_version"],
            [<code key="h">hybrid</code>, "Independent dense and BM25 ranks fused with RRF", "published=true and current schema_version in both branches"],
          ]}
        />
      </section>

      <section id="scope">
        <h2>Request scope</h2>
        <DocumentationTable
          headings={["Caller experience", "Request"]}
          rows={[
            ["Root library search", "Every configured collection, include_hits=false, one explicit total per collection"],
            ["Collection search", "Exactly one configured collection, include_hits=true, requested page of ranked hits"],
          ]}
        />
        <p>Collection is the only corpus routing key; filenames and model output never reroute content.</p>
      </section>

      <section id="correlation">
        <h2>Response correlation</h2>
        <p>
          The response must echo the exact query and mode. Its unique group set must equal the
          requested collection set, count-only groups must contain no hits, and requested pages must
          return the exact possible hit count. Scores are finite, snippets are bounded, and document
          UUIDs are unique per group.
        </p>
      </section>

      <section id="catalog">
        <h2>Catalog validation</h2>
        <p>
          Each returned UUID must resolve to a retrieval-eligible document in the response
          collection. A group total cannot exceed the eligible catalog population. Unknown,
          pending, tombstoned, cross-collection, duplicate, pagination-inconsistent, or impossible
          data rejects the whole response.
        </p>
        <Callout title="No partial response and no metadata fallback">
          <p>
            Missing configuration or transport failure returns 503. An upstream non-success,
            malformed schema, or correlation failure returns 502. An unknown configured collection
            request returns 422. The browser receives an error and no mixed result set.
          </p>
        </Callout>
      </section>

      <section id="deletion">
        <h2>Deletion behavior</h2>
        <p>
          <code>INGESTED</code>, <code>DELETING</code>, and <code>DELETE_FAILED</code> remain catalog-
          eligible while active points still exist; removal is never optimistic. After verified
          active deletion, the document leaves retrieval before any later cleanup retry. Replaced,
          cancelled, rejected, and deleted tombstones are never eligible.
        </p>
      </section>

      <section id="authorization">
        <h2>Authorization boundary</h2>
        <p>
          Bridge uses an administrative Qdrant key because it owns collections, aliases, active
          writes, and screening. Retrieval receives granular read-only JWT claims for required active
          aliases and no screening or broad listing access. The chatbot manager separately derives
          allowed collections from authenticated server-side policy before calling retrieval.
        </p>
      </section>

      <section id="acceptance">
        <h2>Acceptance checks</h2>
        <ul className="check-list">
          <li>Keyword uses content_bm25, semantic uses content_dense, and hybrid uses RRF.</li>
          <li>Every active query filters published=true and the current schema version.</li>
          <li>Every hit retains matching document_id and collection_key and resolves to eligible SQL state.</li>
          <li>A retrieval JWT cannot list or query screening or mutate an active alias.</li>
          <li>Customer and internal positive topics remain isolated; a negative cross-collection topic returns zero.</li>
          <li>A forged unknown, pending, tombstoned, or cross-collection hit rejects the complete response.</li>
          <li>Replacement tests prove no old/new active overlap and deletion tests prove zero residual points.</li>
        </ul>
      </section>
    </>
  ),
};

const ossReview: GuidePage = {
  category: "Reference",
  title: "Playwright & ClamAV review",
  summary:
    "Point-in-time engineering decisions for browser-test tooling and the runtime malware gate, reviewed 2026-07-12 from official sources.",
  facts: [
    { term: "Playwright", detail: "1.61.0, development only" },
    { term: "ClamAV", detail: "1.5.3, runtime POC gate" },
  ],
  toc: [
    { id: "decision", label: "Decision" },
    { id: "playwright", label: "Playwright" },
    { id: "clamav", label: "ClamAV" },
    { id: "parser", label: "Parser relationship" },
    { id: "repository", label: "Repository OSS posture" },
    { id: "monday", label: "Monday priorities" },
    { id: "sources", label: "Official sources" },
  ],
  content: (
    <>
      <section id="decision">
        <h2>Decision</h2>
        <DocumentationTable
          headings={["Component", "Recommendation", "Main condition"]}
          rows={[
            ["Playwright 1.61.0", "Retain", "Approve and inventory the downloaded browser separately; make the full browser suite a required CI check"],
            ["ClamAV 1.5.3", "Retain for the POC", "Keep isolated, current, fail-closed, monitored, and treated as one control rather than proof of PDF safety"],
          ]}
        />
        <p>No immediate version change was recommended at the review date.</p>
      </section>

      <section id="playwright">
        <h2>Playwright</h2>
        <ul>
          <li>The Apache-2.0 Python package is pinned in the development extra and absent from the runtime image.</li>
          <li>Its Chrome for Testing or Headless Shell download is a separate executable and licensing inventory item.</li>
          <li>Package and browser binaries are version-coupled; reinstall the browser after upgrades.</li>
          <li>The test target stays local and trusted. Do not repurpose the suite as an untrusted crawler.</li>
          <li>A release gate should fail on unexpected skips or zero selected browser tests.</li>
        </ul>
      </section>

      <section id="clamav">
        <h2>ClamAV</h2>
        <ul>
          <li>The separate container receives bytes through INSTREAM and never mounts canonical storage.</li>
          <li>Port 3310 remains private; the app accepts only CLEAN and fails closed on every protocol or availability error.</li>
          <li>Version 1.5 is non-LTS and needs release and end-of-life ownership.</li>
          <li>Add signature-age rejection/monitoring, explicit scan-limit and encrypted-PDF policy, and stronger container isolation.</li>
          <li>A published modified image triggers GPLv2 source/notice and third-party-component diligence.</li>
        </ul>
        <Callout title="Outstanding verification">
          <p>
            The live clean/EICAR test was not run during the review because a daemon was unavailable.
            Complete it with current signatures before a controlled pilot.
          </p>
        </Callout>
      </section>

      <section id="parser">
        <h2>Parser relationship</h2>
        <p>
          ClamAV does not neutralize parser risk. Pinned pypdf runs after scanning in a child process
          with Linux CPU and address-space plus page, character, chunk, and wall-clock limits. Those
          controls reduce blast radius but do not provide a complete syscall, filesystem, kernel, or
          network sandbox. Production approval still requires disposable least-privilege isolation.
        </p>
      </section>

      <section id="repository">
        <h2>Repository OSS posture</h2>
        <p>
          The repository currently has no project LICENSE, third-party notice, or SBOM. Publicly
          visible source is not automatically open source. Select the project license deliberately,
          then inventory Python dependencies, the Playwright browser, the ClamAV/base image, Qdrant,
          and bundled third-party components.
        </p>
      </section>

      <section id="monday">
        <h2>Monday priorities</h2>
        <ol className="procedure">
          <li>Choose the project license and create a third-party/SBOM inventory.</li>
          <li>Decide whether the derived ClamAV image will be distributed and document GPL source/notice delivery.</li>
          <li>Record browser payload approval separately from Playwright’s package license.</li>
          <li>Run live ClamAV clean/EICAR acceptance with current signatures.</li>
          <li>Prove stale-signature, timeout, daemon-error, scan-limit, and encrypted-PDF behavior.</li>
          <li>Make the complete browser suite a required CI check where unexpected skips fail.</li>
          <li>Assign Playwright monthly refresh and ClamAV security/end-of-life owners.</li>
        </ol>
      </section>

      <section id="sources">
        <h2>Official sources</h2>
        <ul className="source-links">
          <li><a href="https://pypi.org/project/playwright/" target="_blank" rel="noreferrer">Playwright package and release history</a></li>
          <li><a href="https://playwright.dev/python/docs/browsers" target="_blank" rel="noreferrer">Playwright browser installation and version coupling</a></li>
          <li><a href="https://github.com/microsoft/playwright-python/blob/main/LICENSE" target="_blank" rel="noreferrer">Playwright Python license</a></li>
          <li><a href="https://blog.clamav.net/2026/07/clamav-153-and-145-security-patch.html" target="_blank" rel="noreferrer">ClamAV 1.5.3 security release</a></li>
          <li><a href="https://docs.clamav.net/faq/faq-eol.html" target="_blank" rel="noreferrer">ClamAV support and end-of-life matrix</a></li>
          <li><a href="https://docs.clamav.net/manual/Installing/Docker.html" target="_blank" rel="noreferrer">Official ClamAV Docker guidance</a></li>
          <li><a href="https://github.com/Cisco-Talos/clamav#licensing" target="_blank" rel="noreferrer">ClamAV licensing overview</a></li>
        </ul>
        <p>The repository file <code>docs/oss-review.md</code> contains the complete cited review.</p>
      </section>
    </>
  ),
};

export const referenceGuides: Record<string, GuidePage> = {
  lifecycle,
  "intake-api": intakeApi,
  "code-map": codeMap,
  configuration,
  "search-boundary": searchBoundary,
  "oss-review": ossReview,
};
