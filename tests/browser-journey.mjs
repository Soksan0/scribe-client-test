import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const playwrightModule = await import("playwright");
const playwright = playwrightModule.chromium ? playwrightModule : playwrightModule.default;
const baseUrl = process.env.SCRIBE_UI_URL || "http://127.0.0.1:3000";
const chromePath = process.env.SCRIBE_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const fixture = new URL("../healthcare_messy_data.csv", import.meta.url).pathname;
const downloadRoot = process.env.SCRIBE_DOWNLOAD_DIR || "/private/tmp/scribe-browser-downloads";
await mkdir(downloadRoot, { recursive: true });
const browser = await playwright.chromium.launch({ headless: true, executablePath: chromePath });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const runtimeErrors = [];
page.on("console", (message) => { if (message.type() === "error") runtimeErrors.push(`console: ${message.text()}`); });
page.on("pageerror", (error) => runtimeErrors.push(`page: ${error.message}`));
page.on("requestfailed", (request) => {
  const downloadAbort = /\/api\/exports\/.+\/(download|artifacts\/)/.test(request.url()) && request.failure()?.errorText === "net::ERR_ABORTED";
  if (!downloadAbort) runtimeErrors.push(`request: ${request.method()} ${request.url()} ${request.failure()?.errorText}`);
});

try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  const projectName = `Healthcare acceptance ${Date.now()}`;
  await page.getByLabel("Project name").fill(projectName);
  await page.getByLabel(/Description/).fill("Research-grade healthcare fixture verification");
  await Promise.all([page.waitForURL(/\/projects\/.+\/overview/), page.getByRole("button", { name: "Create project" }).click()]);
  const projectId = page.url().match(/projects\/(prj_[^/]+)/)?.[1];
  assert.ok(projectId);

  await page.locator('input[type="file"]').setInputFiles(fixture);
  await page.getByText(/1,000 rows, 10 columns, 30 findings/).waitFor({ timeout: 30_000 });
  await page.getByText("CLEAN DATA CHECKLIST").waitFor();

  await page.locator('a[href$="/files"]').first().click();
  await page.waitForURL(/\/files$/);
  await page.getByText(/original · 1,000 rows/i).waitFor();
  await page.getByText("Original SHA-256").waitFor();
  assert.equal(await page.getByRole("button", { name: "Reviewed", exact: true }).isDisabled(), true);
  await page.reload({ waitUntil: "networkidle" });
  await page.goBack({ waitUntil: "networkidle" });
  await page.goForward({ waitUntil: "networkidle" });
  assert.match(page.url(), /\/files$/);

  await page.locator('a[href$="/issues"]').first().click();
  const batchButton = page.getByRole("button", { name: "Review 22 safe transformations" });
  await batchButton.waitFor();
  await batchButton.click();
  await page.getByRole("heading", { name: "Apply 22 grouped transformations?" }).waitFor();
  await page.getByText(/3,311 referenced cell/).waitFor();
  await page.getByRole("button", { name: "Accept reviewed changes" }).click();
  await page.getByText(/22 findings accepted/).waitFor({ timeout: 30_000 });
  await page.getByText("Current-engine validation required").waitFor();
  await page.getByRole("button", { name: "Run current scan" }).click();
  await page.getByText(/Current-engine scan completed/).waitFor({ timeout: 30_000 });

  await page.locator('a[href$="/files"]').first().click();
  await page.getByRole("button", { name: "Reviewed", exact: true }).click();
  await page.getByText(/reviewed · 1,000 rows/i).waitFor();
  const previewText = await page.locator(".data-preview").innerText();
  assert.doesNotMatch(previewText, /\bforty\b/i);
  assert.doesNotMatch(previewText, /\bnan\b/i);
  assert.match(previewText, /david lee/);

  await page.locator('a[href$="/rules"]').first().click();
  await page.getByRole("heading", { name: "Study-specific rules" }).waitFor();
  const connection = page.getByRole("button", { name: "Test connection" });
  if (await connection.count()) {
    await connection.click();
    await page.getByText(/Gemini connection verified/).waitFor({ timeout: 30_000 });
  }

  await page.locator('a[href$="/exports"]').first().click();
  assert.equal(await page.getByRole("button", { name: "Generate verified clean export" }).isDisabled(), true);
  await page.getByRole("button", { name: "Generate review package" }).click();
  await page.getByText("Provisional review package is ready.").waitFor({ timeout: 30_000 });
  await page.getByText("R reproduction not verified").first().waitFor();
  const zipPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download ZIP" }).first().click();
  const zipDownload = await zipPromise;
  await zipDownload.saveAs(`${downloadRoot}/scribe-review.zip`);

  await page.screenshot({ path: `${downloadRoot}/desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload({ waitUntil: "networkidle" });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true, "Mobile page must not overflow horizontally");
  await page.keyboard.press("Tab");
  assert.match(await page.evaluate(() => document.activeElement?.tagName || ""), /A|BUTTON|INPUT|SELECT/);

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${baseUrl}/projects/${projectId}/settings`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Move to Trash" }).click();
  await page.waitForURL(baseUrl + "/");
  await page.getByRole("button", { name: /Trash \(1\)/ }).click();
  await page.locator(".trash-project > div:first-child > strong").filter({ hasText: projectName }).waitFor();
  await page.getByRole("button", { name: "Restore" }).click();
  await page.getByRole("button", { name: /Trash \(0\)/ }).waitFor();
  await page.goto(`${baseUrl}/projects/${projectId}/settings`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Move to Trash" }).click();
  await page.getByRole("button", { name: /Trash \(1\)/ }).click();
  await page.getByLabel(`Confirm permanent deletion of ${projectName}`).fill(projectName);
  await page.getByRole("button", { name: "Delete permanently" }).click();
  try {
    await page.waitForFunction(() => document.body.innerText.includes("Trash is empty.") || document.body.innerText.includes("No projects yet."), { timeout: 10_000 });
  } catch (error) {
    throw new Error(`Permanent deletion did not return to an empty project view. Visible page:\n${await page.locator("body").innerText()}`, { cause: error });
  }
  const emptyTrashButton = page.getByRole("button", { name: /Trash \(0\)/ });
  if (await emptyTrashButton.count()) await emptyTrashButton.click();
  if (await page.getByText("Trash is empty.").count()) await page.getByText("Trash is empty.").waitFor();
  assert.deepEqual(runtimeErrors, []);
  console.log(JSON.stringify({ status: "passed", finalUrl: page.url(), downloadRoot }, null, 2));
} finally {
  await browser.close();
}
