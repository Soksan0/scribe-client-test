import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render(path = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(new Request(`http://localhost${path}`, { headers: { accept: "text/html" } }), {
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  }, { waitUntil() {}, passThroughOnException() {} });
}

test("server-renders Scribe on root and refreshable project routes", async () => {
  for (const path of ["/", "/projects/example/overview", "/projects/example/files", "/projects/example/rules", "/projects/example/issues", "/projects/example/exports"]) {
    const response = await render(path);
    assert.equal(response.status, 200, path);
    const html = await response.text();
    assert.match(html, /<title>Scribe — Research data quality assurance<\/title>/i);
    assert.match(html, /Opening Scribe/);
    assert.match(html, /Loading local project data/);
    assert.doesNotMatch(html, /Your site is taking shape|DEMO_FINDINGS|PREVIEW_ROWS/);
  }
});

test("visible controls are connected and demo data is absent", async () => {
  const [client, main] = await Promise.all([
    readFile(new URL("../app/ScribeClient.tsx", import.meta.url), "utf8"),
    readFile(new URL("../backend/app/main.py", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(client, /DEMO_FINDINGS|PREVIEW_ROWS|demo fallback/i);
  assert.doesNotMatch(main, /DEMO_FINDINGS|PREVIEW_ROWS/);
  for (const tag of client.matchAll(/<button\b([^>]*)>/g)) {
    const formSubmit = client.slice(tag.index, tag.index + 100).includes("Confirm and validate");
    assert.ok(/onClick=|type="submit"|disabled=/.test(tag[1]) || formSubmit, `Unwired control: ${tag[0]}`);
  }
  for (const section of ["overview", "files", "rules", "issues", "exports"]) assert.match(client, new RegExp(`\\b${section}\\b`));
  assert.match(client, /Scribe’s local service is unavailable/);
  assert.match(client, /No sample data has been substituted/);
});
