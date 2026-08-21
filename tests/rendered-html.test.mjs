import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render(path = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: handleRequest } = await import(workerUrl.href);
  return handleRequest(new Request(`http://127.0.0.1${path}`, { headers: { accept: "text/html" } }));
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
  const [client, main, uploadValidation] = await Promise.all([
    readFile(new URL("../app/ScribeClient.tsx", import.meta.url), "utf8"),
    readFile(new URL("../backend/app/main.py", import.meta.url), "utf8"),
    readFile(new URL("../app/lib/dataset-upload.ts", import.meta.url), "utf8"),
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
  assert.match(client, /Drop a dataset here/);
  assert.match(uploadValidation, /is not supported\. Choose a/);
  assert.match(client, /could not reach its local data service/);
});
