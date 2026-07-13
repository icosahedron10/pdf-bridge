import assert from "node:assert/strict";
import test from "node:test";

let workerPromise;

async function worker() {
  if (!workerPromise) {
    const workerUrl = new URL("../dist/server/index.js", import.meta.url);
    workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
    workerPromise = import(workerUrl.href).then((module) => module.default);
  }
  return workerPromise;
}

async function render(path = "/") {
  const app = await worker();
  return app.fetch(
    new Request(`http://localhost${path}`, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("renders the internal wiki start page without sales copy", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>PDF Bridge documentation<\/title>/i);
  assert.match(html, /Choose your role/);
  assert.match(html, /responsibility views, not application roles/i);
  assert.match(html, /Library operator/);
  assert.match(html, /Semantic intake owner/);
  assert.match(html, /Internal worker/);
  assert.doesNotMatch(
    html,
    /Know exactly where every PDF stands|without guesswork|deliberately boring|Operator Field Guide/i,
  );
});

const routes = [
  ["/roles/library-operator", "Upload and analyze"],
  ["/roles/semantic-intake-owner", "two execution slots"],
  ["/roles/platform-operator", "single-process POC"],
  ["/roles/retrieval-integrator", "stable grouped search contract"],
  ["/roles/chatbot-integrator", "authenticated server-side collection policy"],
  ["/roles/security-reviewer", "complete upload-to-retrieval trust path"],
  ["/roles/code-maintainer", "one-way package layers"],
  ["/reference/lifecycle", "Document states"],
  ["/reference/intake-api", "Security and idempotency"],
  ["/reference/code-map", "Layer responsibilities"],
  ["/reference/configuration", "Runtime ownership and concurrency"],
  ["/reference/search-boundary", "Qdrant layout"],
  ["/reference/oss-review", "Monday priorities"],
];

test("server-renders every role and reference route", async () => {
  for (const [path, expected] of routes) {
    const response = await render(path);
    assert.equal(response.status, 200, path);
    const html = await response.text();
    assert.match(html, new RegExp(expected, "i"), path);
    assert.match(html, /On this page/i, path);
    assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i, path);
  }
});

test("returns not found for an unknown guide", async () => {
  const response = await render("/roles/not-a-real-role");
  assert.equal(response.status, 404);
});

test("does not expose retired handoff or document-routing workflows", async () => {
  for (const path of [
    "/roles/classification-reviewer",
    "/roles/jenkins-owner",
    "/roles/rag-pipeline-owner",
    "/reference/batch-contract",
  ]) {
    const response = await render(path);
    assert.equal(response.status, 404, path);
  }

  for (const path of ["/", ...routes.map(([path]) => path)]) {
    const response = await render(path);
    const html = await response.text();
    assert.doesNotMatch(
      html,
      /Jenkins|pdf-bridge-job|RAG pipeline owner|Batch contract|manifest v2|version 2|DELETE_CLAIMED|DELETE_CLEANUP|CANCEL_CLEANUP|\bCLAIMED\b|\bSTAGED\b/i,
      path,
    );
  }
});

test("documents the internal worker, protected storage, import, and empty reset", async () => {
  const configuration = await render("/reference/configuration");
  const configurationHtml = await configuration.text();
  assert.match(configurationHtml, /Runtime ownership and concurrency/);
  assert.match(configurationHtml, /PDF_BRIDGE_WORKER_LEASE_SECONDS/);
  assert.match(configurationHtml, /PDF_BRIDGE_QDRANT_API_KEY/);
  assert.match(configurationHtml, /compressed private analysis artifacts/);
  assert.match(configurationHtml, /manifest version 3/i);
  assert.match(configurationHtml, /Empty reset/);

  const maintainer = await render("/roles/code-maintainer");
  const maintainerHtml = await maintainer.text();
  assert.match(maintainerHtml, /sync_to_thread=True/);
  assert.match(maintainerHtml, /Persist intent before external mutation/i);
  assert.match(maintainerHtml, /resource-limited subprocess is not a complete sandbox/i);
});

test("documents every semantic intake endpoint and strict decision shape", async () => {
  const response = await render("/reference/intake-api");
  const html = await response.text();

  for (const expected of [
    "POST /api/v1/uploads/preflight",
    "POST /api/v1/uploads",
    "GET /uploads?open=true",
    "GET /uploads/&lt;upload_id&gt;",
    "GET /api/v1/uploads/6e6f07b7-7cdd-4c26-a83c-feb4329ca93a/analysis",
    "POST /api/v1/uploads/&lt;upload_id&gt;/decision",
    "POST /uploads/&lt;upload_id&gt;/retry",
    "DELETE /uploads/&lt;upload_id&gt;",
    "POST /documents/&lt;document_id&gt;/deletion",
  ]) {
    assert.match(html, new RegExp(expected.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"), expected);
  }
  assert.match(html, /analysis_revision/);
  assert.match(html, /target_document_id/);
  assert.match(html, /There is no rationale field/i);
});

test("documents active and screening isolation plus safe replacement ordering", async () => {
  const lifecycle = await render("/reference/lifecycle");
  const lifecycleHtml = await lifecycle.text();
  assert.match(lifecycleHtml, /DELETING_OLD/);
  assert.match(lifecycleHtml, /availability gap/i);
  assert.match(lifecycleHtml, /REJECTED · CANCELLED · DELETED/);

  const search = await render("/reference/search-boundary");
  const searchHtml = await search.text();
  assert.match(searchHtml, /pdf-bridge-screening-v1/);
  assert.match(searchHtml, /content_dense/);
  assert.match(searchHtml, /content_bm25/);
  assert.match(searchHtml, /published=true/);
  assert.match(searchHtml, /current schema_version/);
});
