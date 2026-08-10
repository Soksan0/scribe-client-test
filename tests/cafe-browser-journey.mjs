import assert from "node:assert/strict";
import { access, mkdir } from "node:fs/promises";
import path from "node:path";

const fixture = process.env.SCRIBE_CAFE_FIXTURE || "/Users/soksanhay/Downloads/dirty_cafe_sales.csv";
try { await access(fixture); } catch { console.log(JSON.stringify({ status: "skipped", reason: "Cafe fixture unavailable" })); process.exit(0); }

const playwrightModule = await import("playwright");
const playwright = playwrightModule.chromium ? playwrightModule : playwrightModule.default;
const baseUrl = process.env.SCRIBE_UI_URL || "http://127.0.0.1:3000";
const chromePath = process.env.SCRIBE_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const downloadRoot = path.join(process.env.SCRIBE_DOWNLOAD_DIR || "/private/tmp/scribe-browser-downloads", "cafe");
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
  await page.getByLabel("Project name").fill(`Cafe arithmetic acceptance ${Date.now()}`);
  await page.getByLabel(/Description/).fill("Validate deterministic transaction cleaning and reproducibility");
  await Promise.all([page.waitForURL(/\/projects\/.+\/overview/), page.getByRole("button", { name: "Create project" }).click()]);
  await page.locator('input[type="file"]').setInputFiles(fixture);
  await page.getByText(/10,000 rows, 8 columns, 27 findings/).waitFor({ timeout: 30_000 });

  await page.locator('a[href$="/issues"]').first().click();
  await page.getByText("Derive missing value from verified arithmetic").first().click();
  await page.getByText(/Verified formula:/).waitFor();
  assert.equal(await page.getByRole("button", { name: "Edit correction" }).count(), 0, "Grouped formula results must not allow one arbitrary replacement");

  const batch = page.getByRole("button", { name: "Review 20 safe transformations" });
  await batch.click();
  await page.getByRole("heading", { name: "Apply 20 grouped transformations?" }).waitFor();
  await page.getByText(/3,887 referenced cell/).waitFor();
  await page.getByRole("button", { name: "Accept reviewed changes" }).click();
  await page.getByText(/20 findings accepted/).waitFor({ timeout: 30_000 });
  await page.getByRole("button", { name: "Run current scan" }).click();
  await page.getByText(/Current-engine scan completed/).waitFor({ timeout: 30_000 });
  await page.getByText(/7 pending findings/).waitFor({ timeout: 30_000 });

  await page.locator('a[href$="/issues"]').first().click();
  for (let index = 0; index < 7; index += 1) {
    const pendingItems = page.locator(".issue-list > button:has(.status-pill.pending)");
    const pendingFinding = pendingItems.first();
    await pendingFinding.waitFor();
    await pendingFinding.click();
    await page.getByText("Recommended cleaning strategies").waitFor();
    await page.getByRole("button", { name: /Acknowledge limitation/ }).click();
    await page.getByText("Click to save this review").waitFor();
    await page.getByRole("button", { name: /Acknowledge limitation/ }).click();
    await page.getByText("Limitation acknowledged with your rationale.").waitFor();
    await page.getByText(new RegExp(`${6 - index} pending findings`)).first().waitFor();
  }
  assert.equal(await page.locator(".issue-list > button:has(.status-pill.pending)").count(), 0);

  await page.locator('a[href$="/files"]').first().click();
  await page.getByRole("button", { name: "Reviewed", exact: true }).click();
  await page.getByText(/reviewed · 10,000 rows/i).waitFor();

  await page.locator('a[href$="/exports"]').first().click();
  await page.getByRole("button", { name: "Generate review package" }).click();
  await page.getByText("Provisional review package is ready.").waitFor({ timeout: 30_000 });
  await page.getByText("R reproduction not verified").first().waitFor();
  const csvPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: /dirty_cafe_sales_cleaned\.csv/ }).click();
  const csvDownload = await csvPromise;
  await csvDownload.saveAs(path.join(downloadRoot, "dirty_cafe_sales_cleaned.csv"));
  const rPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: /clean_.*dirty_cafe_sales\.R/ }).click();
  const rDownload = await rPromise;
  await rDownload.saveAs(path.join(downloadRoot, "clean_dirty_cafe_sales.R"));

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload({ waitUntil: "networkidle" });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true);
  assert.deepEqual(runtimeErrors, []);
  console.log(JSON.stringify({ status: "passed", finalUrl: page.url(), downloadRoot }, null, 2));
} finally {
  await browser.close();
}
