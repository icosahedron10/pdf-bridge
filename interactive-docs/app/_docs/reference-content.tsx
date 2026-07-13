import { Callout, CodeBlock, DocumentationTable } from "./components";
import type { GuidePage } from "./role-content";

const lifecycle: GuidePage = {
  category: "Reference",
  title: "Lifecycle states",
  summary:
    "Canonical document, queue operation, and batch states used by the catalog and handoff protocol.",
  facts: [
    { term: "Source of truth", detail: "persistence/models.py and services/lifecycle.py" },
    { term: "Design", detail: "Recoverable transitions instead of optimistic completion" },
  ],
  toc: [
    { id: "documents", label: "Document states" },
    { id: "operations", label: "Operation states" },
    { id: "results", label: "Pipeline results" },
    { id: "batches", label: "Batch states" },
    { id: "cleanup", label: "Cleanup behavior" },
  ],
  content: (
    <>
      <section id="documents">
        <h2>Document states</h2>
        <DocumentationTable
          headings={["State", "Meaning and legal next action", "Retrieval / preview"]}
          rows={[
            [<code key="q">QUEUED</code>, "Clean canonical PDF awaits Jenkins; cancel or claim", "Not retrieval-eligible / preview allowed"],
            [<code key="c">CLAIMED</code>, "Jenkins holds an ingest lease; stage or recover to queued after expiry", "Not retrieval-eligible / preview allowed"],
            [<code key="s">STAGED</code>, "Verified batch is durable; pipeline reports ingest success or failure", "Not retrieval-eligible / preview allowed"],
            [<code key="i">INGESTED</code>, "Every downstream component succeeded; deletion may be requested", "Eligible / preview allowed"],
            [<code key="if">INGEST_FAILED</code>, "Parser or component failure; retry creates a new queued attempt", "Not eligible / preview blocked"],
            [<code key="dq">DELETE_QUEUED</code>, "Deletion requested but not begun", "Still eligible / preview allowed"],
            [<code key="dc">DELETE_CLAIMED</code>, "Jenkins owns the delete operation", "Still eligible / preview allowed"],
            [<code key="df">DELETE_FAILED</code>, "At least one downstream removal failed; retry", "Still eligible / preview blocked"],
            [<code key="dcl">DELETE_CLEANUP</code>, "Downstream removal succeeded; canonical unlink remains", "Not eligible / preview blocked"],
            [<code key="ccl">CANCEL_CLEANUP</code>, "Queue cancellation committed; canonical unlink remains", "Not eligible / preview blocked"],
            [<code key="d">DELETED</code>, "Terminal audit tombstone", "Not eligible / no preview"],
            [<code key="x">CANCELLED</code>, "Terminal audit tombstone", "Not eligible / no preview"],
          ]}
        />
        <p>
          Retrieval eligibility is the shared lifecycle-and-collection predicate used by API and
          web search. Preview additionally requires a clean scan and retained storage key.
        </p>
      </section>

      <section id="operations">
        <h2>Operation states</h2>
        <p>
          Queue operations are <code>INGEST</code> or <code>DELETE</code> and move through
          <code>QUEUED</code>, <code>CLAIMED</code>, <code>STAGED</code>, then
          <code>SUCCEEDED</code>, <code>FAILED</code>, or <code>CANCELLED</code>. Retries create new
          operations; old attempts remain in the ledger.
        </p>
        <Callout title="Document and operation states are related, not identical">
          <p>
            A staged DELETE operation has operation state <code>STAGED</code> while the document
            remains <code>DELETE_CLAIMED</code>. The document changes only when the result is applied.
          </p>
        </Callout>
      </section>

      <section id="results">
        <h2>Pipeline results</h2>
        <DocumentationTable
          headings={["Operation result", "Component rule", "Error rule", "Document result"]}
          rows={[
            ["successful ingest", "All four components succeed", "Forbidden", <code key="1">INGESTED</code>],
            ["failed ingest", "Report all four observed component states", "Nonblank bounded error required", <code key="2">INGEST_FAILED</code>],
            ["successful delete", "All four components succeed", "Forbidden", <code key="3">DELETE_CLEANUP</code>],
            ["failed delete", "Report all four observed component states", "Nonblank bounded error required", <code key="4">DELETE_FAILED</code>],
          ]}
        />
        <p>
          Encrypted, OCR-only, and no-text parser results use the ordinary failed-ingest row when
          they prevent complete component success.
        </p>
      </section>

      <section id="batches">
        <h2>Batch states</h2>
        <DocumentationTable
          headings={["State", "Meaning"]}
          rows={[
            [<code key="e">EMPTY</code>, "Claim found no work and completed immediately"],
            [<code key="cl">CLAIMED</code>, "Lease is active through download and staging"],
            [<code key="st">STAGED</code>, "Exact operation set was durably acknowledged"],
            [<code key="co">COMPLETED</code>, "Every operation succeeded"],
            [<code key="fa">FAILED</code>, "Every operation failed"],
            [<code key="pa">PARTIAL</code>, "A mixture of successful and failed operations"],
            [<code key="ex">EXPIRED</code>, "Lease elapsed before staging and operations returned to queue"],
          ]}
        />
      </section>

      <section id="cleanup">
        <h2>Cleanup behavior</h2>
        <p>
          Result transitions are committed before canonical unlink runs. If unlink fails, the
          catalog retains the storage key and an explicit cleanup state. Replaying the identical
          report or retrying cancellation resumes the same cleanup without repeating downstream
          work. Audit rows reject ORM update and delete operations.
        </p>
      </section>
    </>
  ),
};

const batchContract: GuidePage = {
  category: "Reference",
  title: "Batch contract",
  summary:
    "The claim, manifest, local staging, acknowledgement, pipeline report, and idempotency rules shared by PDF Bridge and Jenkins.",
  facts: [
    { term: "Wire version", detail: "Manifest v2; local report file v2; HTTP results body unversioned" },
    { term: "Maximum claim", detail: "500 operations" },
  ],
  toc: [
    { id: "claim", label: "Claim" },
    { id: "manifest", label: "Manifest" },
    { id: "stage", label: "Local stage and acknowledgement" },
    { id: "report", label: "Pipeline report" },
    { id: "idempotency", label: "Replay and conflict rules" },
    { id: "transport", label: "Transport protections" },
  ],
  content: (
    <>
      <section id="claim">
        <h2>Claim</h2>
        <p>
          <code>POST /api/v1/jobs/batches/claim</code> accepts a stable 8–128 character
          <code>request_id</code> and limit. The oldest queued operations are leased. The same
          non-expired request ID returns the same batch; an expired ID is retired and rejected.
          The no-work API claim returns HTTP 204 with no body; the CLI writes a zero-operation pull
          summary for Jenkins.
        </p>
      </section>

      <section id="manifest">
        <h2>Manifest</h2>
        <p>
          <code>GET /api/v1/jobs/batches/{`{batch_id}`}/manifest</code> returns version 2. Each item
          supplies operation/document IDs, type, display filename, size, checksum, collection,
          exact relative path, and optional batch-scoped download URL. DELETE items have no
          downloaded file.
        </p>
        <CodeBlock>{"pdfs/{collection_key}/{document_id}.pdf"}</CodeBlock>
        <p>
          The display filename never forms a path. The client independently verifies that the
          supplied path matches collection and UUID exactly.
        </p>
      </section>

      <section id="stage">
        <h2>Local stage and acknowledgement</h2>
        <ol className="procedure">
          <li>Create a temporary sibling directory under the approved destination root.</li>
          <li>Stream each ingest PDF with exclusive creation while checking declared length and SHA-256.</li>
          <li>Write the local versioned manifest durably.</li>
          <li>Atomically rename the complete directory to its batch UUID.</li>
          <li>Acknowledge the complete exact operation set before lease expiry.</li>
        </ol>
        <p>
          Exact staging acknowledgement replay succeeds. Missing, extra, or duplicate IDs fail.
          Once staged, pipeline execution is not constrained by the claim lease.
        </p>
      </section>

      <section id="report">
        <h2>Pipeline report</h2>
        <p>
          The local report carries version and batch ID. Its HTTP projection supplies a
          <code>pipeline_run_id</code> and exactly one typed result per operation. Each result has
          <code>operation_id</code>, <code>success</code>, optional <code>chunk_count</code>, all four
          component values, and optional <code>error</code>. Success requires every component to
          succeed and forbids an error; failure requires a nonblank error. A no-work pull cannot
          report, and strict models reject obsolete fields.
        </p>
      </section>

      <section id="idempotency">
        <h2>Replay and conflict rules</h2>
        <DocumentationTable
          headings={["Replay", "Accepted when", "Conflict when"]}
          rows={[
            ["Claim", "The stable request ID still identifies its original batch", "The ID is expired or invalid"],
            ["Existing local batch", "Manifest and every ingest file fully reverify", "Any path, byte, hash, or manifest differs"],
            ["Stage acknowledgement", "The same complete operation set is sent", "IDs are missing, extra, or duplicated"],
            ["Results", "Pipeline run ID and all material result fields are identical", "A different run or changed result tries to rewrite history"],
          ]}
        />
        <p>
          Upload idempotency is separate: a replay must match normalized filename, SHA-256, byte
          count, and collection. Reusing a key for different material fails.
        </p>
      </section>

      <section id="transport">
        <h2>Transport protections</h2>
        <ul>
          <li>Validate a separately configured exact hostname before reading or sending the token.</li>
          <li>Refuse non-loopback HTTP unless explicitly allowed for diagnosis.</li>
          <li>Do not follow redirects or accept cross-origin download URLs.</li>
          <li>Keep the durable destination outside the Jenkins workspace and synchronized folders.</li>
        </ul>
      </section>
    </>
  ),
};

const codeMap: GuidePage = {
  category: "Reference",
  title: "Code map",
  summary:
    "The enforced one-way package architecture and the modules that own transport, orchestration, rules, persistence, and presentation.",
  facts: [
    { term: "Composition root", detail: "pdf_bridge/app.py" },
    { term: "Executable rules", detail: "tests/test_architecture.py" },
  ],
  toc: [
    { id: "layers", label: "Layer responsibilities" },
    { id: "direction", label: "Dependency direction" },
    { id: "flows", label: "Common request flows" },
    { id: "change-points", label: "Change points" },
    { id: "enforcement", label: "Architecture enforcement" },
  ],
  content: (
    <>
      <section id="layers">
        <h2>Layer responsibilities</h2>
        <DocumentationTable
          headings={["Layer", "Owns", "Representative files"]}
          rows={[
            [<code key="app">app.py</code>, "Composition plus lifespan ownership of the engine, session factory, shared retrieval client, middleware, and routers", "pdf_bridge/app.py"],
            [<code key="controllers">controllers/</code>, "HTTP and Typer input binding, auth dependencies, public output, safe error translation", "api.py, jobs.py, web.py, job_cli.py, admin_cli.py"],
            [<code key="managers">managers/</code>, "Locks, transactions, commit/rollback, workflow and cleanup sequencing", "document.py, batch.py, job_client.py"],
            [<code key="services">services/</code>, "Rules, queries, storage, scanner, retrieval, staging, and page-data behavior", "lifecycle.py, document.py, storage.py, search.py"],
            [<code key="contracts">contracts/</code>, "Strict API, search, batch, and CLI wire shapes", "schemas.py, job_contracts.py"],
            [<code key="persist">persistence/</code>, "Engine/session setup, portable ORM models, constraints, and audit hooks", "db.py, models.py"],
            [<code key="http">http/</code>, "Security dependencies, outer middleware, and problem responses", "security.py, middleware.py, problems.py"],
            [<code key="presentation">presentation/</code>, "View models, serializers, and theme formatting", "view_models.py, api_serializers.py, theme.py"],
          ]}
        />
      </section>

      <section id="direction">
        <h2>Dependency direction</h2>
        <CodeBlock>{"app → controllers → managers → services\n                      ↓          ↓\n                 http/contracts/presentation/persistence/core"}</CodeBlock>
        <ul>
          <li>Services do not import Litestar, HTTP, managers, controllers, or the app.</li>
          <li>Controllers do not construct SQL.</li>
          <li>Managers own transaction commit and rollback.</li>
          <li>Blocking filesystem, scanner, retrieval, and database-backed page flows use synchronous handlers with <code>sync_to_thread=True</code>.</li>
          <li>Presentation is stateless and does not call services or issue SQL.</li>
          <li>Package initializers do not re-export implementations.</li>
        </ul>
      </section>

      <section id="flows">
        <h2>Common request flows</h2>
        <DocumentationTable
          headings={["Experience", "Trace"]}
          rows={[
            ["Browser page", "controllers/web.py → managers/web.py → services/web_page.py → presentation/templates"],
            ["Upload mutation", "controllers/api.py → managers/document.py → services/document.py + storage/scanner/lifecycle"],
            ["Jenkins claim/report", "controllers/jobs.py → managers/batch.py → services/job_batch.py/lifecycle.py"],
            ["CLI pull/report", "controllers/job_cli.py → managers/job_client.py → services/job_http.py/job_staging.py"],
            ["Search", "controllers/api.py → managers/search.py → services/search.py + catalog.py"],
          ]}
        />
      </section>

      <section id="change-points">
        <h2>Change points</h2>
        <p>
          Put the rule in the lowest layer that owns it. Transport-specific parsing stays in a
          controller; transaction and compensation sequences stay in a manager; reusable business
          rules and I/O stay in services; application-owned resource lifecycle stays in the
          composition root; persisted fields require models plus an Alembic migration.
        </p>
      </section>

      <section id="enforcement">
        <h2>Architecture enforcement</h2>
        <p>
          <code>tests/test_architecture.py</code> checks the exact module set, import direction,
          manager transaction ownership, service transport independence, controller SQL absence,
          root-package shape, and initializer behavior. Adding or moving a module requires an
          intentional architecture decision and test update.
        </p>
      </section>
    </>
  ),
};

const configuration: GuidePage = {
  category: "Reference",
  title: "Configuration & operations",
  summary:
    "Runtime settings, startup checks, health semantics, storage layout, backup, historical import, and upgrade constraints for the Linux POC.",
  facts: [
    { term: "Supported topology", detail: "Linux, Docker Compose, one application process" },
    { term: "Business dataset", detail: "Complete bridge_data volume" },
  ],
  toc: [
    { id: "settings", label: "Settings" },
    { id: "startup", label: "Startup and health" },
    { id: "runtime", label: "Runtime ownership" },
    { id: "storage", label: "Storage layout" },
    { id: "backup", label: "Backup and import" },
    { id: "upgrade", label: "Upgrade rules" },
    { id: "daily", label: "Daily checks" },
  ],
  content: (
    <>
      <section id="settings">
        <h2>Settings</h2>
        <DocumentationTable
          headings={["Concern", "Main settings", "Guardrail"]}
          rows={[
            ["Collections", <code key="1">PDF_BRIDGE_COLLECTIONS</code>, "1–50 unique path-safe lowercase keys shared with Qdrant and authorization policy"],
            ["Storage/database", "PDF_BRIDGE_STORAGE_ROOT and optional PDF_BRIDGE_DATABASE_URL", "External to source/synchronized folders; SQLite defaults below storage root"],
            ["Identity", "PDF_BRIDGE_AUTH_MODE, PDF_BRIDGE_TRUSTED_PROXY_CIDRS, PDF_BRIDGE_TRUSTED_IDENTITY_HEADER", "Identity headers accepted only from configured immediate peers"],
            ["Secrets", "PDF_BRIDGE_SESSION_SECRET and PDF_BRIDGE_JOB_TOKEN", "Required outside tests, at least 32 characters, distinct, and not placeholders"],
            ["Intake", "PDF_BRIDGE_MAX_UPLOAD_BYTES, PDF_BRIDGE_MAX_UPLOAD_FILES, and PDF_BRIDGE_UPLOAD_CHUNK_BYTES", "Upload limit cannot exceed the ClamAV stream ceiling; chunk size bounds each quarantine copy read"],
            ["Scanner", "PDF_BRIDGE_CLAMD_HOST, PDF_BRIDGE_CLAMD_PORT, PDF_BRIDGE_CLAMD_TIMEOUT, PDF_BRIDGE_CLAMD_STREAM_MAX_BYTES", "Any detection, timeout, error, or malformed reply fails closed"],
            ["Batch", "PDF_BRIDGE_CLAIM_LEASE_MINUTES", "Size for download/staging, not ingestion duration"],
            ["Retrieval", "PDF_BRIDGE_SEARCH_API_URL, PDF_BRIDGE_SEARCH_API_TOKEN, PDF_BRIDGE_SEARCH_API_TIMEOUT", "Token is separate; enterprise URL must be HTTPS"],
          ]}
        />
      </section>

      <section id="startup">
        <h2>Startup and health</h2>
        <p>
          Settings validate cross-field rules before subdirectories are created. The Compose
          entrypoint preflights or creates the storage root and runs Alembic before Uvicorn. During
          application lifespan, the bridge validates active collection references against the same
          settings-selected database that request sessions will use.
        </p>
        <DocumentationTable
          headings={["Endpoint", "Meaning"]}
          rows={[
            [<code key="l">/api/v1/health/live</code>, "Process only"],
            [<code key="r">/api/v1/health/ready</code>, "Database, writable root/objects/temporary/quarantine directories, and ClamAV PING; intended for traffic admission"],
            [<code key="d">/api/v1/health/dependencies</code>, "Currently the same detailed check body; intended for restricted operator diagnosis"],
          ]}
        />
        <p>Retrieval is deliberately absent from readiness. ClamAV PING does not prove signature freshness.</p>
      </section>

      <section id="runtime">
        <h2>Runtime ownership and concurrency</h2>
        <p>
          One application lifespan owns the SQLAlchemy engine and session factory plus one shared
          synchronous retrieval client. It closes what it creates on normal shutdown and on startup
          failure; injected test clients remain caller-owned, and custom database providers are
          rejected outside test mode.
        </p>
        <p>
          Upload, scanner-dependent pages, and retrieval use synchronous handlers with
          <code>sync_to_thread=True</code>. Blocking file, scanner, HTTP, or SQLite work therefore
          runs in Litestar worker threads instead of blocking the event loop; liveness remains
          responsive while an upload scan is waiting.
        </p>
      </section>

      <section id="storage">
        <h2>Storage layout</h2>
        <CodeBlock>{"<storage-root>/\n  catalog.sqlite3\n  objects/\n  temporary/\n  quarantine/"}</CodeBlock>
        <p>
          Canonical object keys are UUID-derived. The downstream
          <code>pdfs/{`{collection}`}/{`{document_id}`}.pdf</code> tree is not bridge
          canonical storage.
        </p>
        <p>
          Litestar first spools each multipart part. The bridge then copies it in configured chunks
          to a private file under <code>quarantine/</code>, hashes and validates it, scans that exact
          copy, and promotes clean bytes atomically into <code>objects/</code>.
          <code>temporary/</code> is reserved for historical import staging.
        </p>
      </section>

      <section id="backup">
        <h2>Backup and historical import</h2>
        <p>
          Stop Jenkins scheduling, wait for active uploads/imports, then stop only the application
          before backing up the entire bridge data volume. A live SQLite copy without canonical
          objects is not a valid backup. Restore periodically into an isolated network and test data
          plus migration.
        </p>
        <p>
          <code>pdf-bridge import-manifest</code> is a controlled one-time registration of already
          indexed PDFs. Use an explicit source root, dry-run first, then reviewed <code>--apply</code>.
          It does not reconstruct queue/batch/audit history and is not a backup mechanism. If its
          session-scope commit fails, every promoted canonical object is removed; all removals are
          attempted and any failures are reported with their storage keys.
        </p>
      </section>

      <section id="upgrade">
        <h2>Upgrade rules</h2>
        <ul>
          <li>Avoid the claim window and active uploads.</li>
          <li>Take and verify a whole-volume backup.</li>
          <li>Review exact application, Python, dependency, and ClamAV releases plus migrations.</li>
          <li>Start one process and run upload, claim, and search smoke checks.</li>
          <li>Treat collection routing or version 2 contract changes as a coordinated maintenance cutover across every store and integration.</li>
        </ul>
      </section>

      <section id="daily">
        <h2>Daily checks</h2>
        <ul className="check-list">
          <li>Application and scanner readiness.</li>
          <li>FreshClam success, signature age, memory, scan latency, and capacity.</li>
          <li>Unexpectedly old claimed or staged batches.</li>
          <li>Catalog counts reconciled with downstream PDF and Qdrant payloads.</li>
          <li>Every downstream payload has a known bridge UUID and matching collection.</li>
          <li>Internal-topic negative test still returns customer zero.</li>
          <li>Backups and credential rotations remain within policy.</li>
        </ul>
      </section>
    </>
  ),
};

const searchBoundary: GuidePage = {
  category: "Reference",
  title: "Search boundary",
  summary:
    "How PDF Bridge constrains retrieval requests, correlates grouped responses to its catalog, and separates operator search from chatbot authorization.",
  facts: [
    { term: "Failure model", detail: "Reject the complete response" },
    { term: "Fallback", detail: "None" },
  ],
  toc: [
    { id: "scope", label: "Request scope" },
    { id: "correlation", label: "Response correlation" },
    { id: "catalog", label: "Catalog validation" },
    { id: "deletion", label: "Deletion behavior" },
    { id: "authorization", label: "Authorization boundary" },
    { id: "acceptance", label: "Acceptance checks" },
  ],
  content: (
    <>
      <section id="scope">
        <h2>Request scope</h2>
        <DocumentationTable
          headings={["Caller experience", "Request"]}
          rows={[
            ["Root library search", "Every configured collection, include_hits=false, one explicit total per collection"],
            ["Collection search", "Exactly one configured collection, include_hits=true, requested page of ranked hits"],
          ]}
        />
        <p>Modes are keyword, semantic, and hybrid. Collection is the only corpus routing key.</p>
      </section>

      <section id="correlation">
        <h2>Response correlation</h2>
        <p>
          The response must echo query and mode. Its unique group set must equal the requested
          collection set. Pagination is correlated through the exact expected hit count. Count-only
          groups contain no hits.
        </p>
      </section>

      <section id="catalog">
        <h2>Catalog validation</h2>
        <p>
          Each hit UUID must resolve through the shared lifecycle-and-collection predicate in the
          response collection. A group total cannot exceed the corresponding eligible catalog
          population. Unknown, inactive, cross-collection, duplicate, pagination-inconsistent, or
          impossible data rejects the whole response.
        </p>
        <Callout title="No partial response and no metadata fallback">
          <p>
            Missing configuration or a network/request failure returns 503. Upstream non-2xx,
            malformed JSON/schema, or correlation failure returns 502. An unknown requested
            collection returns 422. The browser receives a visible error and no mixed result set.
          </p>
        </Callout>
      </section>

      <section id="deletion">
        <h2>Deletion behavior</h2>
        <p>
          <code>DELETE_QUEUED</code>, <code>DELETE_CLAIMED</code>, and
          <code>DELETE_FAILED</code> remain retrieval-eligible because removal is not optimistic.
          Once downstream success moves the document to <code>DELETE_CLEANUP</code>, retrieval is
          blocked even if canonical unlink still needs recovery.
        </p>
      </section>

      <section id="authorization">
        <h2>Authorization boundary</h2>
        <p>
          The bridge collection audience is descriptive. The external chatbot manager must derive
          <code>allowed_collections</code> from authenticated server-side policy and intersect it
          with the requested set before retrieval. This repository cannot enforce that external path.
        </p>
      </section>

      <section id="acceptance">
        <h2>Acceptance checks</h2>
        <ul className="check-list">
          <li>Every Qdrant payload retains matching <code>document_id</code> and <code>collection_key</code>.</li>
          <li>No unknown or lifecycle-ineligible document is returned.</li>
          <li>Customer and internal positive topics return only their intended collection.</li>
          <li>An internal topic returns an explicit customer zero.</li>
          <li>A forged cross-collection hit fails the complete response.</li>
          <li>The chatbot manager intersects an authenticated server-derived allowlist.</li>
        </ul>
      </section>
    </>
  ),
};

const ossReview: GuidePage = {
  category: "Reference",
  title: "Playwright & ClamAV review",
  summary:
    "Point-in-time engineering decisions for the browser-test dependency and runtime malware gate, reviewed 2026-07-12 from official sources.",
  facts: [
    { term: "Playwright", detail: "1.61.0, development only" },
    { term: "ClamAV", detail: "1.5.3, runtime POC gate" },
  ],
  toc: [
    { id: "decision", label: "Decision" },
    { id: "playwright", label: "Playwright" },
    { id: "clamav", label: "ClamAV" },
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
            ["Playwright 1.61.0", "Retain", "Approve/inventory the downloaded browser separately and make all five tests an explicit required job"],
            ["ClamAV 1.5.3", "Retain for the POC", "Keep isolated, current, fail-closed, monitored, and treated as one control rather than proof of PDF safety"],
          ]}
        />
        <p>No immediate version change was recommended at the review date.</p>
      </section>

      <section id="playwright">
        <h2>Playwright</h2>
        <ul>
          <li>The Apache-2.0 Python package is pinned in the dev extra and absent from the runtime image.</li>
          <li>Its Chrome for Testing/Headless Shell download is a separate executable and licensing inventory item.</li>
          <li>Package and browser binaries are version-coupled; reinstall the browser after upgrades.</li>
          <li>The test target stays local and trusted. Do not repurpose the job as an untrusted crawler.</li>
          <li>Browser tests are opt-in today; a release gate should fail on skips or zero selected tests.</li>
        </ul>
      </section>

      <section id="clamav">
        <h2>ClamAV</h2>
        <ul>
          <li>The separate container receives bytes through INSTREAM and never mounts canonical storage.</li>
          <li>Port 3310 remains private; the app accepts only CLEAN and fails closed on every protocol/availability error.</li>
          <li>Version 1.5 is non-LTS and needs release/EOL ownership.</li>
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

      <section id="repository">
        <h2>Repository OSS posture</h2>
        <p>
          The repository currently has no project LICENSE, third-party notice, or SBOM. Publicly
          visible source is not automatically open source. Select the project license deliberately,
          then inventory Python dependencies, the Playwright browser, the ClamAV/base image, and
          bundled third-party components.
        </p>
      </section>

      <section id="monday">
        <h2>Monday priorities</h2>
        <ol className="procedure">
          <li>Choose the project license and create a third-party/SBOM inventory.</li>
          <li>Decide whether the derived ClamAV image will be distributed and document GPL source/notice delivery.</li>
          <li>Record browser payload approval separately from Playwright’s package license.</li>
          <li>Run live ClamAV clean/EICAR acceptance with current signatures.</li>
          <li>Prove stale-signature, timeout, daemon-error, scan-limit, and encrypted-PDF policy behavior.</li>
          <li>Put all five browser tests in a required job where unexpected skips fail.</li>
          <li>Assign Playwright monthly refresh and ClamAV security/EOL owners.</li>
        </ol>
      </section>

      <section id="sources">
        <h2>Official sources</h2>
        <ul className="source-links">
          <li><a href="https://pypi.org/project/playwright/" target="_blank" rel="noreferrer">Playwright package and release history</a></li>
          <li><a href="https://playwright.dev/python/docs/browsers" target="_blank" rel="noreferrer">Playwright browser installation and version coupling</a></li>
          <li><a href="https://github.com/microsoft/playwright-python/blob/main/LICENSE" target="_blank" rel="noreferrer">Playwright Python license</a></li>
          <li><a href="https://blog.clamav.net/2026/07/clamav-153-and-145-security-patch.html" target="_blank" rel="noreferrer">ClamAV 1.5.3 security release</a></li>
          <li><a href="https://docs.clamav.net/faq/faq-eol.html" target="_blank" rel="noreferrer">ClamAV support and EOL matrix</a></li>
          <li><a href="https://docs.clamav.net/manual/Installing/Docker.html" target="_blank" rel="noreferrer">Official ClamAV Docker guidance</a></li>
          <li><a href="https://github.com/Cisco-Talos/clamav#licensing" target="_blank" rel="noreferrer">ClamAV licensing overview</a></li>
        </ul>
        <p>The repository file <code>docs/oss-review.md</code> contains the complete cited review.</p>
      </section>
    </>
  ),
};

// Reference pages are exported in the same order as the wiki navigation.

export const referenceGuides: Record<string, GuidePage> = {
  lifecycle,
  "batch-contract": batchContract,
  "code-map": codeMap,
  configuration,
  "search-boundary": searchBoundary,
  "oss-review": ossReview,
};
