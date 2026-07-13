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
    "Use the browser workspace to place documents in the right collection, follow processing, search available content, and request lifecycle changes.",
  facts: [
    { term: "Primary surface", detail: "Browser UI" },
    { term: "Owns", detail: "Collection choice and document-level decisions" },
    { term: "Does not own", detail: "Parsing, indexes, Jenkins, or chatbot authorization" },
  ],
  toc: [
    { id: "before", label: "Before you begin" },
    { id: "normal-path", label: "Normal path" },
    { id: "what-you-see", label: "What you see" },
    { id: "guardrails", label: "Actions and guardrails" },
    { id: "failure", label: "When something goes wrong" },
    { id: "ownership", label: "Where ownership ends" },
    { id: "implementation", label: "Implementation map" },
  ],
  content: (
    <>
      <section id="before">
        <h2>Before you begin</h2>
        <p>
          The root URL redirects to <code>/library</code>. The persistent application navigation
          links to Collections, Queue, and Upload. These are responsibility views, not permission
          roles: the POC gives every browser user the same capabilities.
        </p>
        <p>
          Collection definitions come from deployment configuration. Their audience labels help
          you place content, but they do not authorize chatbot users.
        </p>
      </section>

      <section id="normal-path">
        <h2>Normal path</h2>
        <div className="journey">
          <div><span>1. Your action</span><strong>Open Collections and choose the destination corpus.</strong><p>Review the audience label, description, and available and processing counts.</p></div>
          <div><span>2. Your action</span><strong>Open Upload, keep that collection selected, and choose PDFs.</strong><p>Preflight checks name and size. A possible duplicate requires your explicit confirmation.</p></div>
          <div><span>3. System response</span><strong>The bridge copies the spooled part into private quarantine, validates, hashes, scans, and promotes it atomically.</strong><p>A clean PDF enters Queue as <code>QUEUED</code> with its immutable collection. Rejected bytes never become available content.</p></div>
          <div><span>4. Waiting</span><strong>Jenkins claims and stages work for the downstream pipeline.</strong><p>Queue shows the latest attempt as queued, claimed, staged, failed, or in cleanup.</p></div>
          <div><span>5. Result</span><strong>The document becomes available or remains retryable.</strong><p>Use the document ledger for operation, batch, component, and audit evidence.</p></div>
        </div>
      </section>

      <section id="what-you-see">
        <h2>What you see</h2>
        <DocumentationTable
          headings={["Page", "Experience"]}
          rows={[
            [<code key="a">/library</code>, "Every configured collection plus authoritative catalog counts; root search adds one retrieval total per collection"],
            [<code key="b">/library/&lt;collection&gt;</code>, "Browse available documents or view collection-scoped search hits, snippets, and scores"],
            [<code key="c">/upload</code>, "Upload limits, scanner readiness, collection choice, per-file preflight, progress, and result"],
            [<code key="d">/queue</code>, "Latest active attempt per document, filters, batch context, bounded error, and last Jenkins claim"],
            [<code key="e">/documents/&lt;uuid&gt;</code>, "Collection, scan result, hash, uploader, operation history, components, and audit events"],
          ]}
        />
      </section>

      <section id="guardrails">
        <h2>Actions and guardrails</h2>
        <ul>
          <li>Choose a collection before upload; there is intentionally no default.</li>
          <li>Exact active-content duplicates are blocked; name-and-size matches require confirmation.</li>
          <li>Collection placement becomes immutable when the document is queued.</li>
          <li>Cancel only an unclaimed queued ingest.</li>
          <li>Retry only the current failed ingest or deletion attempt.</li>
          <li>For cancellation cleanup, use the browser Retry cleanup action after storage recovers.</li>
          <li>For deletion cleanup, have the Jenkins/platform side replay the identical result report.</li>
          <li>Preview only clean, retained PDFs in eligible lifecycle states.</li>
          <li>Deletion remains visible until all downstream components and canonical cleanup finish.</li>
        </ul>
      </section>

      <section id="failure">
        <h2>When something goes wrong</h2>
        <DocumentationTable
          headings={["What you see", "Meaning", "Correct response"]}
          rows={[
            ["Scanner unavailable", "The upload trust gate is down", "Wait for readiness; do not request a bypass"],
            ["Claimed item cannot be cancelled", "Jenkins owns the active attempt", "Let it complete, then request deletion if needed"],
            [<code key="if">INGEST_FAILED</code>, "Parser or dependency failed operationally", "Repair the named cause, then Retry"],
            [<code key="df">DELETE_FAILED</code>, "At least one downstream removal failed", "Repair that component, then Retry"],
            [<code key="cc">CANCEL_CLEANUP</code>, "Cancellation committed but canonical unlink failed", "Repair storage, then use Retry cleanup"],
            [<code key="dc">DELETE_CLEANUP</code>, "Downstream deletion succeeded but canonical unlink failed", "Repair storage, then replay the identical Jenkins result"],
            ["Search error", "Retrieval was unavailable or failed correlation", "Keep using catalog pages; no partial results are shown"],
          ]}
        />
      </section>

      <section id="ownership">
        <h2>Where ownership ends</h2>
        <p>
          You decide collection placement, duplicate intent, and document lifecycle actions. You do
          not parse PDFs, operate Jenkins, edit Qdrant, or decide which chatbot users may retrieve a
          collection. Correct a placement mistake by completing deletion and uploading again.
        </p>
      </section>

      <section id="implementation">
        <h2>Implementation map</h2>
        <ModuleReferences
          items={[
            { path: "pdf_bridge/controllers/web.py", purpose: "Browser routes and transport binding" },
            { path: "pdf_bridge/services/web_page.py", purpose: "Collection, queue, document, and search page data" },
            { path: "pdf_bridge/managers/document.py", purpose: "Upload and lifecycle transaction boundaries" },
            { path: "pdf_bridge/services/document.py", purpose: "Preflight, quarantine copy, scan, and atomic promotion workflow" },
            { path: "pdf_bridge/services/lifecycle.py", purpose: "Document and operation transition rules" },
            { path: "pdf_bridge/templates/", purpose: "Browser views" },
            { path: "tests/test_browser.py", purpose: "End-to-end coworker workflows" },
          ]}
        />
      </section>
    </>
  ),
};

const jenkinsOwner: GuidePage = {
  category: "Role guide",
  title: "Jenkins owner",
  summary:
    "Use the supported CLI to claim work, verify every downloaded byte and path, stage an immutable batch, and submit the pipeline report.",
  facts: [
    { term: "Primary surface", detail: "pdf-bridge-job CLI and Jenkinsfile" },
    { term: "Owns", detail: "Authenticated transport and durable staging" },
    { term: "Hands off to", detail: "The existing RAG pipeline" },
  ],
  toc: [
    { id: "before", label: "Before you begin" },
    { id: "normal-path", label: "Normal path" },
    { id: "what-you-see", label: "What you see" },
    { id: "guardrails", label: "Actions and guardrails" },
    { id: "failure", label: "When something goes wrong" },
    { id: "ownership", label: "Where ownership ends" },
    { id: "implementation", label: "Implementation map" },
  ],
  content: (
    <>
      <section id="before">
        <h2>Before you begin</h2>
        <p>
          Use a controlled Linux agent with Python 3.12 and an exact released wheel. Keep the
          handoff root outside the Jenkins workspace and synchronized directories. Inject the job
          token from Jenkins credentials; keep the bridge URL and a separate allowed-host pin as
          reviewed pipeline constants.
        </p>
        <CodeBlock>{"pdf-bridge-job pull \\\n  --base-url https://pdf-bridge.internal \\\n  --allowed-host pdf-bridge.internal \\\n  --destination /srv/rag/pdf-bridge-handoff \\\n  --request-id \"$BUILD_TAG\" \\\n  --result-file pull-result.json"}</CodeBlock>
      </section>

      <section id="normal-path">
        <h2>Normal path</h2>
        <div className="journey">
          <div><span>1. Your action</span><strong>Run pull with a stable Jenkins request ID.</strong><p>Reuse the same ID for retries of the same build. A no-work claim returns a zero-operation summary.</p></div>
          <div><span>2. System response</span><strong>The bridge leases up to the configured limit and returns a version 2 manifest.</strong><p>The manifest supplies operation/document IDs, type, collection, size, SHA-256, and the exact relative path.</p></div>
          <div><span>3. Client response</span><strong>The CLI writes a temporary batch directory and verifies it completely.</strong><p>Paths, byte counts, hashes, and the local manifest are checked before one atomic rename.</p></div>
          <div><span>4. Client response</span><strong>The CLI acknowledges the exact operation set as staged.</strong><p>The claim lease protects download through staging; it does not bound pipeline execution.</p></div>
          <div><span>5. Handoff</span><strong>Invoke the existing pipeline with the immutable staged manifest.</strong><p>Keep the batch directory read-only and write results elsewhere.</p></div>
          <div><span>6. Your action</span><strong>Submit the complete result with the matching pull summary.</strong><p>The CLI validates batch identity and contract shape before contacting the bridge.</p></div>
        </div>
      </section>

      <section id="what-you-see">
        <h2>What you see</h2>
        <DocumentationTable
          headings={["Artifact", "Important fields"]}
          rows={[
            ["Pull summary", "batch ID, request ID, operation count, batch directory, manifest SHA-256, replay flag"],
            ["manifest.json", "version 2, lease times, and one canonical operation record per item"],
            ["Report response", "batch aggregate state, successful/failed counts, and an idempotent-replay flag"],
            ["CLI error", "Safe detail plus bridge request ID when available"],
          ]}
        />
      </section>

      <section id="guardrails">
        <h2>Actions and guardrails</h2>
        <ul>
          <li>The allowed hostname is validated before the bearer token is read or sent.</li>
          <li>Redirects are disabled; non-loopback HTTP requires an explicit diagnostic override.</li>
          <li>Use the server-issued relative path; the display filename never determines a path.</li>
          <li>An existing final batch is accepted only after manifest and file revalidation.</li>
          <li>Stage acknowledgement must contain the complete exact operation set.</li>
          <li>A no-work pull cannot authorize a report.</li>
          <li>Disable concurrent builds for one bridge instance.</li>
        </ul>
      </section>

      <section id="failure">
        <h2>When something goes wrong</h2>
        <DocumentationTable
          headings={["Failure point", "Correct recovery"]}
          rows={[
            ["Bridge unavailable before claim", "Retry the same build and request ID"],
            ["Interrupted download", "Rerun pull with the same request ID; bytes are reverified"],
            ["Lease expired before staging", "After requeue, start a new claim with a new request ID"],
            ["Staged batch did not run", "Run the same staged directory; do not claim replacement work"],
            ["Lost result response", "Resubmit the identical report"],
            ["Unsafe path, checksum mismatch, or changed existing batch", "Stop and preserve evidence"],
          ]}
        />
      </section>

      <section id="ownership">
        <h2>Where ownership ends</h2>
        <p>
          Jenkins owns authenticated claim, verified download, staging, and report submission. It
          does not choose collection placement, parse PDFs, invent component results, or mutate
          catalog state directly.
        </p>
      </section>

      <section id="implementation">
        <h2>Implementation map</h2>
        <ModuleReferences
          items={[
            { path: "pdf_bridge/controllers/job_cli.py", purpose: "pull and report command declarations" },
            { path: "pdf_bridge/managers/job_client.py", purpose: "Client workflow orchestration" },
            { path: "pdf_bridge/services/job_http.py", purpose: "Host pinning, credentials, TLS, and API calls" },
            { path: "pdf_bridge/services/job_staging.py", purpose: "Canonical path checks, hash verification, atomic staging, and report parsing" },
            { path: "pdf_bridge/contracts/job_contracts.py", purpose: "Client options, pull summary, report file, and client error contracts" },
            { path: "pdf_bridge/services/job_staging.py", purpose: "Staged manifest models plus local verification and promotion" },
            { path: "Jenkinsfile.example", purpose: "Reference job lifecycle and credential handling" },
          ]}
        />
      </section>
    </>
  ),
};

const ragPipelineOwner: GuidePage = {
  category: "Role guide",
  title: "RAG pipeline owner",
  summary:
    "Consume every staged operation, preserve authoritative collection routing, and produce one strict result for every operation.",
  facts: [
    { term: "Primary surface", detail: "Version 2 staged manifest and report.json" },
    { term: "Owns", detail: "Parsing, derived files, BM25, dense indexes, and downstream deletion" },
    { term: "Does not own", detail: "Canonical bridge storage or collection placement" },
  ],
  toc: [
    { id: "inputs", label: "Inputs" },
    { id: "normal-path", label: "Normal path" },
    { id: "results", label: "Result contract" },
    { id: "deletion", label: "Deletion results" },
    { id: "failure", label: "When something goes wrong" },
    { id: "ownership", label: "Where ownership ends" },
    { id: "implementation", label: "Implementation map" },
  ],
  content: (
    <>
      <section id="inputs">
        <h2>Inputs</h2>
        <p>
          Read the staged <code>manifest.json</code>. Each item carries an operation ID, bridge
          document UUID, operation type, display filename, size and hash, immutable collection key,
          exact path <code>pdfs/{`{collection_key}`}/{`{document_id}`}.pdf</code>, and an optional
          download URL.
        </p>
        <p>
          An <code>INGEST</code> item has a verified PDF at that path. A <code>DELETE</code> item has
          routing metadata but no downloaded file.
        </p>
      </section>

      <section id="normal-path">
        <h2>Normal path</h2>
        <div className="journey">
          <div><span>1. Your action</span><strong>Process every manifest operation independently.</strong><p>Keep the staged directory read-only so a replay can be verified.</p></div>
          <div><span>2. Ingest</span><strong>Parse the PDF and durably create every downstream component.</strong><p>PDF source, Markdown, BM25, and dense must all succeed before the operation succeeds.</p></div>
          <div><span>3. Delete</span><strong>Remove PDF source, Markdown, BM25, and dense data.</strong><p>Use the manifest collection and bridge UUID; never infer routing from a filename.</p></div>
          <div><span>4. Your output</span><strong>Atomically write one complete version 2 report.</strong><p>Include exactly one typed result for every operation, even if the pipeline exits nonzero.</p></div>
        </div>
        <p>Keep the durable downstream PDF at the exact collection-only path supplied by the bridge.</p>
      </section>

      <section id="results">
        <h2>Result contract</h2>
        <DocumentationTable
          headings={["Result", "Component rule", "Error rule"]}
          rows={[
            [<code key="s">success: true</code>, "PDF source, Markdown, BM25, and dense all succeeded", "Error is forbidden"],
            [<code key="f">success: false</code>, "Report the actual state of all four components", "A nonblank bounded error is required"],
          ]}
        />
        <p>
          Encrypted content, required OCR, no extractable text, parser crashes, and component write
          failures are ordinary failed ingestion results when they prevent complete success.
        </p>
      </section>

      <section id="deletion">
        <h2>Deletion results</h2>
        <p>
          A successful delete requires <code>pdf_source</code>, <code>markdown</code>,
          <code>bm25</code>, and <code>dense</code> all to succeed. A failure in any component keeps
          the bridge document in a retryable delete state. Treat already-absent downstream targets
          as successful so replay is idempotent.
        </p>
      </section>

      <section id="failure">
        <h2>When something goes wrong</h2>
        <ul>
          <li>Write the complete report even if one operation fails, then allow the build to remain failed.</li>
          <li>A crash before a complete report leaves the batch staged for controlled replay.</li>
          <li>Never fabricate success rows to unblock reporting.</li>
          <li>Exclude stack dumps, secrets, document contents, and local paths from bounded errors.</li>
          <li>If a report response is lost, the Jenkins owner resubmits the identical report.</li>
        </ul>
      </section>

      <section id="ownership">
        <h2>Where ownership ends</h2>
        <p>
          The pipeline owns parsing, derived storage, BM25, dense/Qdrant, and downstream deletion.
          PDF Bridge owns canonical clean bytes, leasing, catalog truth, and audit. The pipeline
          must not change collection placement or omit <code>document_id</code> and
          <code>collection_key</code> from chunk payloads.
        </p>
      </section>

      <section id="implementation">
        <h2>Implementation map</h2>
        <ModuleReferences
          items={[
            { path: "docs/jenkins.md", purpose: "Manifest, result, component, and recovery contract" },
            { path: "pdf_bridge/contracts/schemas.py", purpose: "Pipeline components and result validation" },
            { path: "pdf_bridge/controllers/jobs.py", purpose: "Server-side manifest, download, staging, and results endpoints" },
            { path: "pdf_bridge/services/lifecycle.py", purpose: "Application of ingest, failure, and deletion results" },
            { path: "tests/test_pipeline_flow.py", purpose: "End-to-end batch and pipeline transition coverage" },
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
    "Configure and run the single-process POC, keep scanner and storage dependencies healthy, and preserve the catalog and canonical objects as one dataset.",
  facts: [
    { term: "Primary surface", detail: "Docker Compose, environment, health, and runbook" },
    { term: "Owns", detail: "Runtime, secrets, storage, backup, maintenance, and ClamAV availability" },
    { term: "Does not own", detail: "Parser behavior, ranking, or chatbot authorization" },
  ],
  toc: [
    { id: "before", label: "Before you begin" },
    { id: "normal-path", label: "Normal path" },
    { id: "what-you-see", label: "What you see" },
    { id: "guardrails", label: "Actions and guardrails" },
    { id: "failure", label: "When something goes wrong" },
    { id: "ownership", label: "Where ownership ends" },
    { id: "implementation", label: "Implementation map" },
  ],
  content: (
    <>
      <section id="before">
        <h2>Before you begin</h2>
        <p>
          The supported topology is Linux and Docker Compose. Configure independent session and job
          secrets, allowed hosts, identity mode, collection registry, external storage root,
          upload chunk size, ClamAV connection, claim lease, and optional retrieval connection and
          timeout. JSON list values must remain valid JSON.
        </p>
        <Callout title="One process while the catalog is SQLite" tone="warning">
          <p>
            The POC runs one Uvicorn worker. Move to managed PostgreSQL and test migrations,
            concurrency, locking, backup, and least-privilege roles before adding replicas or HA.
          </p>
        </Callout>
      </section>

      <section id="normal-path">
        <h2>Normal path</h2>
        <CodeBlock>{"docker compose up -d --build\ndocker compose ps\ndocker compose logs --tail=100 app clamav"}</CodeBlock>
        <div className="journey">
          <div><span>1. System response</span><strong>ClamAV initializes its persistent signature database.</strong><p>The first start may take minutes and requires the scanner memory budget.</p></div>
          <div><span>2. System response</span><strong>The entrypoint migrates, then the application lifespan creates its database and retrieval resources.</strong><p>Startup validates active collection references against the same database that will serve requests. Owned resources are released even if startup fails.</p></div>
          <div><span>3. Your action</span><strong>Wait for readiness before admitting users or Jenkins.</strong><p>Database, storage root, objects, temporary import staging, upload quarantine, and scanner must all be usable.</p></div>
          <div><span>4. Ongoing work</span><strong>Monitor signatures, capacity, queue age, reconciliation, backups, and credentials.</strong><p>Retrieval outages remain visible but do not make the upload/catalog service unready.</p></div>
        </div>
      </section>

      <section id="what-you-see">
        <h2>What you see</h2>
        <DocumentationTable
          headings={["Surface", "Meaning"]}
          rows={[
            [<code key="l">/api/v1/health/live</code>, "The process can answer HTTP; use for liveness/restart"],
            [<code key="r">/api/v1/health/ready</code>, "Database, every required storage directory, and ClamAV are usable; use for traffic admission"],
            [<code key="d">/api/v1/health/dependencies</code>, "Currently the same detailed check body as ready; intended for restricted operator diagnosis"],
            ["Compose logs", "Migration, application, FreshClam, clamd, and readiness evidence"],
            ["Queue and ledger", "Claim age, bounded pipeline errors, cleanup state, and correlation identifiers"],
          ]}
        />
      </section>

      <section id="guardrails">
        <h2>Actions and guardrails</h2>
        <ul>
          <li>Keep the service on loopback or a restricted internal network.</li>
          <li>Keep clamd TCP private; it is unauthenticated and unencrypted.</li>
          <li>Monitor signature age and FreshClam errors; a PONG does not prove currency.</li>
          <li>Back up the complete <code>bridge_data</code> volume, not a live SQLite file alone.</li>
          <li>Run historical import locally with an explicit source root, dry-run, and reviewed manifest.</li>
          <li>Reserve <code>temporary/</code> for historical imports; live uploads stage under private <code>quarantine/</code>.</li>
          <li>Treat collection-key changes as coordinated corpus migrations.</li>
          <li>Never rewrite lifecycle/audit rows or recursively delete computed paths.</li>
        </ul>
      </section>

      <section id="failure">
        <h2>When something goes wrong</h2>
        <DocumentationTable
          headings={["Symptom", "First interpretation", "Response"]}
          rows={[
            ["ClamAV startup is slow", "Signature initialization or memory pressure", "Inspect FreshClam/clamd logs and wait within the documented start window"],
            ["Scanner unavailable", "Upload trust gate outage", "Restore clamd/signatures/network; keep uploads closed"],
            ["SQLite locked", "Unsupported concurrency, stalled I/O, or live-file manipulation", "Return to one process and inspect volume health"],
            ["Startup rejects collections", "Active rows no longer match the deployment registry", "Correct configuration or run a coordinated migration"],
            ["Historical import commit fails", "The catalog transaction did not complete after one or more promotions", "Confirm every promoted object was removed; any failed storage keys are reported for repair"],
            ["Cleanup state persists", "Canonical unlink failed after catalog/downstream work", "Repair storage and replay the exact cleanup"],
          ]}
        />
      </section>

      <section id="ownership">
        <h2>Where ownership ends</h2>
        <p>
          Platform operations owns runtime exposure, secrets, database, canonical storage,
          migrations, ClamAV, monitoring, backup, and controlled maintenance. Parser behavior,
          search ranking, Qdrant data modeling, and chatbot policy require their respective owners.
        </p>
      </section>

      <section id="implementation">
        <h2>Implementation map</h2>
        <ModuleReferences
          items={[
            { path: "pdf_bridge/core/config.py", purpose: "Settings validation and enterprise startup guards" },
            { path: "pdf_bridge/app.py", purpose: "Composition, lifespan-owned resources, startup validation, and middleware" },
            { path: "pdf_bridge/services/health.py", purpose: "Database, complete storage-layout, and scanner checks" },
            { path: "pdf_bridge/managers/importing.py", purpose: "Historical import transaction and post-promotion compensation" },
            { path: "docker-compose.yml", purpose: "Topology, volumes, health, limits, and hardening" },
            { path: "docker-entrypoint.sh", purpose: "Migration-before-start sequence" },
            { path: "docs/runbook.md", purpose: "Troubleshooting, backup, upgrades, and cutover" },
            { path: "docs/importing.md", purpose: "Historical import and acceptance" },
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
    "Implement the grouped search contract and preserve catalog correlation so cross-collection, stale, duplicate, or impossible results fail as a whole.",
  facts: [
    { term: "Primary surface", detail: "POST {search-api}/search" },
    { term: "Owns", detail: "Query execution, ranking, and correlated grouped responses" },
    { term: "Does not own", detail: "Chatbot authorization or bridge catalog state" },
  ],
  toc: [
    { id: "before", label: "Before you begin" },
    { id: "normal-path", label: "Normal path" },
    { id: "contract", label: "Request and response contract" },
    { id: "guardrails", label: "Actions and guardrails" },
    { id: "failure", label: "When something goes wrong" },
    { id: "ownership", label: "Where ownership ends" },
    { id: "implementation", label: "Implementation map" },
  ],
  content: (
    <>
      <section id="before">
        <h2>Before you begin</h2>
        <p>
          Use the same stable lowercase collection keys as PDF Bridge and Qdrant. Every indexed
          chunk retains the bridge <code>document_id</code> and <code>collection_key</code>. Catalog
          lifecycle and collection determine retrieval eligibility.
        </p>
      </section>

      <section id="normal-path">
        <h2>Normal path</h2>
        <div className="journey">
          <div><span>1. Bridge request</span><strong>Root library search requests counts for every configured collection.</strong><p><code>include_hits=false</code>; return exactly one group per key, including explicit zero.</p></div>
          <div><span>2. Bridge request</span><strong>Collection search requests hits for exactly one collection.</strong><p><code>include_hits=true</code>; return the requested page of bridge document UUIDs, scores, and bounded snippets.</p></div>
          <div><span>3. Your response</span><strong>Echo query and mode exactly.</strong><p>The bridge validates pagination through the expected hit count for the requested page.</p></div>
          <div><span>4. Bridge validation</span><strong>Every total and hit is checked against the active eligible catalog.</strong><p>Only a completely valid response reaches the coworker.</p></div>
        </div>
      </section>

      <section id="contract">
        <h2>Request and response contract</h2>
        <DocumentationTable
          headings={["Rule", "Requirement"]}
          rows={[
            ["Modes", "keyword, semantic, or hybrid"],
            ["Groups", "Exactly the requested unique collection set"],
            ["Totals", "Strict nonnegative integer and no larger than eligible bridge catalog"],
            ["Hits", "Unique bridge UUID, finite score, bounded snippet, optional metadata"],
            ["Body", "Valid strict schema within the 2 MiB response cap"],
          ]}
        />
      </section>

      <section id="guardrails">
        <h2>Actions and guardrails</h2>
        <ul>
          <li>Return no hits for count-only requests.</li>
          <li>Return the exact expected hit count for a page, subject to total and final page.</li>
          <li>Never duplicate groups or document IDs.</li>
          <li>Never return inactive, unknown, duplicate, or cross-collection content.</li>
          <li>Do not use metadata fallback when the authoritative index is unavailable.</li>
        </ul>
      </section>

      <section id="failure">
        <h2>When something goes wrong</h2>
        <p>
          An unconfigured or unreachable service yields a visible 503. Non-success upstream
          responses, malformed JSON, correlation failures, impossible totals, or invalid hits yield
          a visible 502. The bridge discards the entire response and shows no partial results.
        </p>
        <p>
          Recovery is reconciliation: compare bridge eligibility counts to Qdrant payloads, remove
          stale/cross-routed chunks, then rerun positive and negative collection tests.
        </p>
      </section>

      <section id="ownership">
        <h2>Where ownership ends</h2>
        <p>
          The retrieval service owns ranking and search semantics. The pipeline owns chunk creation.
          PDF Bridge owns catalog validation. End-user authentication and collection authorization
          belong to the external chatbot manager.
        </p>
      </section>

      <section id="implementation">
        <h2>Implementation map</h2>
        <ModuleReferences
          items={[
            { path: "pdf_bridge/contracts/schemas.py", purpose: "Strict SearchRequest, SearchResponse, groups, and hits" },
            { path: "pdf_bridge/services/search.py", purpose: "HTTP call, response cap, and exact echo/group correlation" },
            { path: "pdf_bridge/services/catalog.py", purpose: "Catalog eligibility validation" },
            { path: "pdf_bridge/services/web_page.py", purpose: "Count and hit rendering with no partial fallback" },
            { path: "tests/test_search_failures.py", purpose: "Mismatched, malformed, and cross-boundary response coverage" },
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
    "Derive collection access from authenticated server-side policy and constrain every retrieval request before it reaches Qdrant.",
  facts: [
    { term: "Primary surface", detail: "External chatbot manager" },
    { term: "Owns", detail: "End-user identity, authorization, and allowed-collection intersection" },
    { term: "Repository status", detail: "Invariant documented here; implementation lives elsewhere" },
  ],
  toc: [
    { id: "boundary", label: "Boundary" },
    { id: "normal-path", label: "Normal path" },
    { id: "guardrails", label: "Actions and guardrails" },
    { id: "acceptance", label: "Acceptance evidence" },
    { id: "failure", label: "When something goes wrong" },
    { id: "ownership", label: "Where ownership ends" },
  ],
  content: (
    <>
      <section id="boundary">
        <h2>Boundary</h2>
        <p>
          PDF Bridge does not implement the chatbot manager. Its collection audience label is
          operator-facing placement metadata, not a grant. Direct Qdrant access also bypasses the
          bridge-side response checks.
        </p>
        <Callout title="Do not trust the browser">
          <p>
            A requested collection list or allowlist from a client is untrusted input. Authorization
            must originate from the authenticated server-side user and policy.
          </p>
        </Callout>
      </section>

      <section id="normal-path">
        <h2>Normal path</h2>
        <div className="journey">
          <div><span>1. Your action</span><strong>Authenticate the end user.</strong><p>Use the organization-approved identity boundary; the bridge browser session is unrelated.</p></div>
          <div><span>2. Your action</span><strong>Derive <code>allowed_collections</code> from server-side policy.</strong><p>Use the exact stable keys shared by the bridge registry and Qdrant.</p></div>
          <div><span>3. Your action</span><strong>Intersect the requested set with the allowed set.</strong><p>Reject or remove disallowed collections before retrieval; never broaden the request.</p></div>
          <div><span>4. Your action</span><strong>Query only the resulting authorized collections.</strong><p>Retain user, policy, collection, and request correlation in approved audit evidence.</p></div>
        </div>
      </section>

      <section id="guardrails">
        <h2>Actions and guardrails</h2>
        <ul>
          <li>Use collection keys, not mutable display names or audience labels, in policy.</li>
          <li>Apply authorization before any count, search, or direct vector query.</li>
          <li>Do not give the client unrestricted search credentials.</li>
          <li>Ensure caches include the authorized collection scope in their key.</li>
          <li>Preserve negative authorization decisions in test and audit evidence.</li>
        </ul>
      </section>

      <section id="acceptance">
        <h2>Acceptance evidence</h2>
        <ul className="check-list">
          <li>An internal-only user can retrieve an internal test topic.</li>
          <li>A customer-only user receives zero for that internal topic.</li>
          <li>Changing a client-provided list cannot add a disallowed collection.</li>
          <li>Unknown collection keys fail closed.</li>
          <li>Direct Qdrant and cache paths cannot bypass the server policy intersection.</li>
        </ul>
      </section>

      <section id="failure">
        <h2>When something goes wrong</h2>
        <p>
          Treat a cross-collection result, policy mismatch, unknown key, or cache-scope leak as a
          security incident. Restrict retrieval, preserve request/user/policy evidence, reconcile
          Qdrant and cache contents, and restore access only after the bypass is understood.
        </p>
      </section>

      <section id="ownership">
        <h2>Where ownership ends</h2>
        <p>
          The chatbot manager owns user authorization. PDF Bridge records corpus placement and
          validates its own human-facing search proxy, but it cannot prove that an external chatbot
          applied the correct allowlist.
        </p>
      </section>
    </>
  ),
};

const securityReviewer: GuidePage = {
  category: "Role guide",
  title: "Security reviewer",
  summary:
    "Trace the complete upload-to-retrieval trust path, separate implemented POC controls from residual risk, and define evidence required for broader deployment.",
  facts: [
    { term: "Primary surface", detail: "Threat boundaries, deployment topology, tests, and OSS review" },
    { term: "Owns", detail: "Risk acceptance and required controls" },
    { term: "Current claim", detail: "Restricted proof of concept, not enterprise-ready" },
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
          <li>Confirm the service is restricted and identify browser, bridge, scanner, Jenkins, parser, storage, retrieval, Qdrant, and chatbot trust boundaries.</li>
          <li>Trace bytes through upload limits/signature/hash, ClamAV INSTREAM, UUID promotion, batch-scoped download, parsing, and derived indexes.</li>
          <li>Trace browser identity, session, CSRF, same-origin, Host, and security-header behavior.</li>
          <li>Trace separate Jenkins/retrieval credentials, exact-host pinning, no redirects, and response correlation.</li>
          <li>Review backup, audit, incident, dependency, and enterprise-gate evidence.</li>
        </ol>
        <Callout title="These guides describe personas, not RBAC" tone="warning">
          <p>
            Browser users share one capability set. <code>trusted-header</code> improves identity
            attribution but does not authorize roles. Jenkins is the only separately enforced role
            boundary, through its bearer token.
          </p>
        </Callout>
      </section>

      <section id="implemented">
        <h2>Implemented controls</h2>
        <ul>
          <li>Bounded multipart spooling followed by a private quarantine copy, strict PDF name/signature checks, SHA-256, and duplicate gates.</li>
          <li>ClamAV before atomic canonical promotion; detection, timeout, malformed reply, and outage fail closed.</li>
          <li>UUID-derived paths outside the webroot; filenames remain display metadata.</li>
          <li>Upload and historical-import transaction failures roll back catalog state and compensate promoted objects; cleanup failures are surfaced.</li>
          <li>Encrypted/authenticated session state, same-origin mutations, CSRF, and trusted Host checks.</li>
          <li>Separate automation credentials, exact-host pinning, no redirects, and batch-scoped downloads.</li>
          <li>Canonical path, byte count, hash, operation-set, and result correlation checks.</li>
          <li>Audit rows are append-only through SQLAlchemy ORM listeners; direct database administration needs separate controls.</li>
          <li>Pipeline errors are length-bounded; producers remain responsible for excluding secrets, content, and local paths.</li>
          <li>Non-root application container with read-only root, dropped capabilities, and no-new-privileges.</li>
        </ul>
      </section>

      <section id="residual">
        <h2>Residual risk</h2>
        <DocumentationTable
          headings={["Risk", "Why it remains"]}
          rows={[
            ["Parser compromise", "A clean signature scan does not neutralize malformed, novel, encrypted, or resource-exhausting PDFs"],
            ["Anonymous access", "anonymous-poc labels a session but authenticates no person and grants every browser user the same actions"],
            ["Service-wide token", "Disclosure permits batch download and lifecycle mutation until rotation"],
            ["Single data volume", "Catalog metadata and canonical PDFs are one compromise/backup domain"],
            ["Sensitive metadata", "Filenames, actors, queries, snippets, and bounded errors may still be confidential"],
            ["Misrouting", "Wrong collection configuration or stale Qdrant payloads can bypass otherwise correct bridge checks"],
          ]}
        />
      </section>

      <section id="gates">
        <h2>Deployment gates</h2>
        <ul className="check-list">
          <li>Organization-managed TLS, SSO, trusted proxy enforcement, and explicit browser authorization.</li>
          <li>Server-derived chatbot collection policy applied before every retrieval path.</li>
          <li>Approved secret management, rotation, revocation, and incident owners.</li>
          <li>Disposable least-privilege parser sandbox with CPU, memory, time, process, and network limits.</li>
          <li>Encrypted-PDF, content-disarm, scan-limit, and signature-age policy.</li>
          <li>Managed PostgreSQL and approved durable encrypted object storage before HA.</li>
          <li>Cross-store reconciliation, negative isolation tests, SAST, image/dependency scanning, DAST, and focused penetration testing.</li>
        </ul>
      </section>

      <section id="oss">
        <h2>Playwright and ClamAV decisions</h2>
        <DocumentationTable
          headings={["Component", "Decision", "Open work"]}
          rows={[
            ["Playwright 1.61.0", "Accept as development-only browser tooling", "Inventory the downloaded browser separately; make five tests a required job where skips fail"],
            ["ClamAV 1.5.3", "Accept as the POC malware gate", "Run live acceptance; monitor/reject stale signatures; define limits/encrypted policy; harden container; own non-LTS updates"],
            ["Repository", "OSS posture is not complete", "Choose a project license and add third-party notices or an SBOM"],
          ]}
        />
      </section>

      <section id="incident">
        <h2>Incident path</h2>
        <p>
          Restrict traffic and stop new claims/uploads without destroying evidence. Rotate affected
          credentials, record request/document/operation/batch/pipeline IDs, preserve approved logs
          and storage evidence, and inspect downstream parser and indexes. Do not rewrite audit or
          lifecycle rows. Restore access only after root cause and scanner state are understood.
        </p>
      </section>

      <section id="implementation">
        <h2>Evidence map</h2>
        <ModuleReferences
          items={[
            { path: "docs/security.md", purpose: "Controls, residual risks, enterprise gates, ClamAV, and incident procedure" },
            { path: "docs/oss-review.md", purpose: "Official-source dependency due diligence and Monday checklist" },
            { path: "pdf_bridge/http/security.py", purpose: "Session actor, CSRF, origin, proxy, and Jenkins credential checks" },
            { path: "pdf_bridge/services/scanner.py", purpose: "Fail-closed clamd INSTREAM protocol" },
            { path: "pdf_bridge/services/storage.py", purpose: "Private quarantine/import staging, atomic promotion, canonical paths, and controlled unlink behavior" },
            { path: "tests/test_app_lifecycle.py", purpose: "Owned-resource validation, startup-failure cleanup, and injected-resource ownership" },
            { path: "tests/test_uploads.py", purpose: "Quarantine, private modes, responsiveness, and transaction compensation evidence" },
            { path: "tests/test_security_and_import.py", purpose: "Security and administrative import evidence" },
            { path: "tests/test_clamav_integration.py", purpose: "Opt-in clean/EICAR live-daemon check" },
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
    "Trace behavior through the one-way package layers, change the narrowest responsible module, and preserve lifecycle, correlation, and compensation invariants.",
  facts: [
    { term: "Primary surface", detail: "Python package, migrations, templates, and tests" },
    { term: "Owns", detail: "Bridge behavior and its public contracts" },
    { term: "Does not own", detail: "The external parser, Qdrant, or chatbot manager" },
  ],
  toc: [
    { id: "orientation", label: "Orient in the package" },
    { id: "normal-path", label: "Normal change path" },
    { id: "change-map", label: "Choose the change point" },
    { id: "invariants", label: "Invariants" },
    { id: "verification", label: "Verification" },
    { id: "documentation", label: "Documentation standard" },
  ],
  content: (
    <>
      <section id="orientation">
        <h2>Orient in the package</h2>
        <p>
          <code>pdf_bridge/app.py</code> is the composition root. Its lifespan builds the engine,
          session factory, and shared synchronous retrieval client from the active settings,
          validates the database it will serve, and releases owned resources on shutdown or startup
          failure. Test-injected clients remain caller-owned. Other package-root modules should not
          accumulate implementation behavior.
        </p>
        <DocumentationTable
          headings={["Layer", "Responsibility"]}
          rows={[
            [<code key="c">controllers/</code>, "HTTP/CLI binding, auth dependencies, public responses, and error translation"],
            [<code key="m">managers/</code>, "Locks, transaction commit/rollback, orchestration, and multi-step compensation"],
            [<code key="s">services/</code>, "Lifecycle rules, queries, files, scanner, retrieval, staging, and page-data behavior"],
            [<code key="co">contracts/</code>, "Strict transport-neutral Pydantic wire models"],
            [<code key="p">persistence/</code>, "SQLAlchemy setup, models, constraints, indexes, and audit hooks"],
            [<code key="h">http/</code>, "Security, middleware, and problem responses without routes"],
            [<code key="pr">presentation/</code>, "View models, serializers, and theme formatting"],
          ]}
        />
      </section>

      <section id="normal-path">
        <h2>Normal change path</h2>
        <ol className="procedure">
          <li>Start at the HTTP route or Typer command that exposes the behavior.</li>
          <li>Follow its manager to find transaction, lock, and cleanup sequencing.</li>
          <li>Change domain rules or I/O in the matching service.</li>
          <li>Update strict contracts and persistence/migrations when the boundary changes.</li>
          <li>Update presentation and repository/wiki documentation where the experience changes.</li>
          <li>Add focused tests, then run the full suite and relevant opt-in integration checks.</li>
        </ol>
      </section>

      <section id="change-map">
        <h2>Choose the change point</h2>
        <DocumentationTable
          headings={["Change", "Start in"]}
          rows={[
            ["Route, binding, credential dependency, public error", <code key="1">controllers/ or http/</code>],
            ["Transaction, lock, compensation, cleanup sequence", <code key="2">managers/</code>],
            ["Lifecycle eligibility or transition", <code key="3">services/lifecycle.py</code>],
            ["Upload, storage, scanner, search, staging behavior", <code key="4">matching services module</code>],
            ["Wire shape", <code key="5">contracts/ plus contract/transport tests</code>],
            ["Catalog field or constraint", <code key="6">persistence/models.py plus Alembic migration</code>],
            ["Browser wording/layout", <code key="7">services/web_page.py, presentation/, templates/, static/</code>],
            ["Runtime setting", <code key="8">core/config.py, .env.example, Compose, and configuration docs</code>],
          ]}
        />
      </section>

      <section id="invariants">
        <h2>Invariants</h2>
        <ul>
          <li>Dependencies point downward; services never import Litestar, HTTP, managers, controllers, or app.</li>
          <li>Controllers do not construct SQL; managers own commits and rollbacks.</li>
          <li>Blocking upload, scanner, page, and retrieval work uses <code>sync_to_thread=True</code> to run in Litestar worker threads so the event loop remains responsive.</li>
          <li>Scanner, path, staging, result, and search-correlation failures fail closed.</li>
          <li>User metadata never forms canonical storage or handoff paths.</li>
          <li>Collection is required for every document and immutable after queue.</li>
          <li>Filesystem promotion and database mutation have explicit compensation through audit flush and commit; cleanup failures surface.</li>
          <li>Idempotent replays must match material request/result data exactly.</li>
          <li>Cleanup-pending states preserve enough information for exact replay after failure.</li>
        </ul>
      </section>

      <section id="verification">
        <h2>Verification</h2>
        <DocumentationTable
          headings={["Check", "Purpose"]}
          rows={[
            [<code key="pt">python -m pytest</code>, "Application, contracts, lifecycle, storage, HTTP, CLI, and architecture"],
            [<code key="rf">python -m ruff check .</code>, "Static quality and imports"],
            ["Browser opt-in", "Chromium workflows across upload, queue, search, navigation, and deletion"],
            ["ClamAV opt-in", "Clean PDF-shaped fixture and EICAR against a real daemon"],
            [<code key="ar">tests/test_architecture.py</code>, "Exact modules, one-way imports, transaction ownership, and service transport independence"],
            [<code key="al">tests/test_app_lifecycle.py</code>, "Settings-owned database, shared search client, and startup/shutdown resource ownership"],
            [<code key="up">tests/test_uploads.py</code>, "Private quarantine, concurrent responsiveness, and upload compensation"],
          ]}
        />
      </section>

      <section id="documentation">
        <h2>Documentation standard</h2>
        <p>
          Public classes, functions, and methods have descriptive docstrings. Use section comments
          to orient readers around terse multi-step logic such as idempotent correlation,
          compensation, and eligibility queries; do not narrate individual statements.
        </p>
        <p>
          If a change alters what a role sees, decides, waits for, or recovers from, update the
          matching role guide as well as the closest repository Markdown reference and test.
        </p>
      </section>
    </>
  ),
};

// Role guides are exported in the same order as the wiki navigation.

export const roleGuides: Record<string, GuidePage> = {
  "library-operator": libraryOperator,
  "jenkins-owner": jenkinsOwner,
  "rag-pipeline-owner": ragPipelineOwner,
  "platform-operator": platformOperator,
  "retrieval-integrator": retrievalIntegrator,
  "chatbot-integrator": chatbotIntegrator,
  "security-reviewer": securityReviewer,
  "code-maintainer": codeMaintainer,
};
