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
  assert.match(html, /RAG pipeline owner/);
  assert.doesNotMatch(
    html,
    /Know exactly where every PDF stands|without guesswork|deliberately boring|Operator Field Guide/i,
  );
});

const routes = [
  ["/roles/library-operator", "Use the browser workspace"],
  ["/roles/jenkins-owner", "claim work, verify every downloaded byte"],
  ["/roles/rag-pipeline-owner", "produce one strict result"],
  ["/roles/platform-operator", "single-process POC"],
  ["/roles/retrieval-integrator", "grouped search contract"],
  ["/roles/chatbot-integrator", "authenticated server-side policy"],
  ["/roles/security-reviewer", "complete upload-to-retrieval trust path"],
  ["/roles/code-maintainer", "one-way package layers"],
  ["/reference/lifecycle", "Document states"],
  ["/reference/batch-contract", "Local stage and acknowledgement"],
  ["/reference/code-map", "Layer responsibilities"],
  ["/reference/configuration", "Startup and health"],
  ["/reference/search-boundary", "Response correlation"],
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

test("does not expose the removed document-routing workflow", async () => {
  const removedRoute = await render("/roles/classification-reviewer");
  assert.equal(removedRoute.status, 404);

  for (const path of ["/", "/roles/library-operator", "/reference/lifecycle"]) {
    const response = await render(path);
    const html = await response.text();
    assert.doesNotMatch(html, /Needs review|Classification reviewer|review_required/i, path);
  }
});

test("documents the current runtime, quarantine, and compensation model", async () => {
  const configuration = await render("/reference/configuration");
  const configurationHtml = await configuration.text();
  assert.match(configurationHtml, /Runtime ownership and concurrency/);
  assert.match(configurationHtml, /PDF_BRIDGE_UPLOAD_CHUNK_BYTES/);
  assert.match(configurationHtml, /root\/objects\/temporary\/quarantine/);
  assert.match(configurationHtml, /session-scope commit fails/);

  const maintainer = await render("/roles/code-maintainer");
  const maintainerHtml = await maintainer.text();
  assert.match(maintainerHtml, /sync_to_thread=True/);
  assert.match(maintainerHtml, /startup\/shutdown resource ownership/i);
});
