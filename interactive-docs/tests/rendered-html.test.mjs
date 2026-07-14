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

const retiredRoutes = [
  "/roles/library-operator",
  "/roles/semantic-intake-owner",
  "/roles/platform-operator",
  "/roles/retrieval-integrator",
  "/roles/chatbot-integrator",
  "/roles/security-reviewer",
  "/roles/code-maintainer",
  "/reference/lifecycle",
  "/reference/intake-api",
  "/reference/code-map",
  "/reference/configuration",
  "/reference/search-boundary",
  "/reference/oss-review",
];

const staleClaims =
  /Semantic intake owner|Qdrant layout|active alias|screening index|embedding provider|provider credentials|content_dense|content_bm25|pdf-bridge-screening|Jenkins|manifest version/i;

async function assertRetirementNotice(path) {
  const response = await render(path);
  assert.equal(response.status, 200, path);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i, path);

  const html = await response.text();
  assert.match(html, /Status: Retired/i, path);
  assert.match(html, /no longer an authoritative source/i, path);
  assert.match(html, /repository.*README.*docs\//is, path);
  assert.match(html, /Streamlit is the target operator facade/i, path);
  assert.doesNotMatch(html, staleClaims, path);
}

test("renders a clear retirement landing page", async () => {
  await assertRetirementNotice("/");

  const response = await render("/");
  const html = await response.text();
  assert.match(html, /<title>PDF Bridge documentation — retired<\/title>/i);
  assert.match(html, /content="noindex, nofollow"/i);
});

test("replaces every former role and reference page with the same retirement notice", async () => {
  for (const path of retiredRoutes) {
    await assertRetirementNotice(path);
  }
});

test("does not revive documentation for unknown role or reference slugs", async () => {
  await assertRetirementNotice("/roles/not-a-real-role");
  await assertRetirementNotice("/reference/not-a-real-reference");
});
