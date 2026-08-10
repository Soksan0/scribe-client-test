import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const playwrightModule = await import(process.env.SCRIBE_PLAYWRIGHT_MODULE || "playwright");
const playwright = playwrightModule.chromium ? playwrightModule : playwrightModule.default;
const baseUrl = process.env.SCRIBE_UI_URL || "http://127.0.0.1:3001";
const fixture = new URL("../healthcare_messy_data.csv", import.meta.url).pathname;
const downloadRoot = process.env.SCRIBE_DOWNLOAD_DIR || "/private/tmp/scribe-healthcare-downloads";
await mkdir(downloadRoot, { recursive: true });
const browser = await playwright.chromium.launch({ headless: true, executablePath: process.env.SCRIBE_CHROME_PATH || undefined });
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const runtimeErrors = [];
page.on("console", (message) => { if (message.type() === "error") runtimeErrors.push(`console: ${message.text()}`); });
page.on("pageerror", (error) => runtimeErrors.push(`page: ${error.message}`));
page.on("requestfailed", (request) => {
  const downloadAbort = /\/api\/exports\/.+\/download/.test(request.url()) && request.failure()?.errorText === "net::ERR_ABORTED";
  if (!downloadAbort) runtimeErrors.push(`request: ${request.url()} ${request.failure()?.errorText}`);
});

try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByLabel("Project name").fill(`Healthcare acceptance ${Date.now()}`);
  await Promise.all([page.waitForURL(/\/projects\/.+\/overview/), page.getByRole("button", { name: "Create project" }).click()]);
  const projectId = page.url().match(/projects\/(prj_[^/]+)/)?.[1];
  assert.ok(projectId);
  await page.locator('input[type="file"]').setInputFiles(fixture);
  await page.getByText(/1,000 rows, 10 columns, 29 findings/).waitFor({ timeout: 30_000 });

  await page.locator('a[href$="/rules"]').first().click();
  await page.getByLabel("Rule type").selectOption("range");
  await page.locator(".rule-form").first().locator("select").nth(2).selectOption("Age");
  await page.getByLabel("Minimum").fill("0");
  await page.getByLabel("Maximum").fill("120");
  await page.getByRole("button", { name: "Confirm and validate" }).click();
  await page.getByText(/Rule confirmed/).waitFor();
  const addRuleForm = page.locator(".rule-form").first();
  await addRuleForm.getByLabel("Rule type").selectOption("range");
  await addRuleForm.locator("select").nth(2).selectOption("Age");
  await addRuleForm.getByLabel("Minimum").fill("0");
  await addRuleForm.getByLabel("Maximum").fill("120");
  await page.getByRole("button", { name: "Confirm and validate" }).click();
  await page.getByText(/already exists/).waitFor();
  assert.equal(await page.locator(".rule-list strong", { hasText: "Age range" }).count(), 1);

  await page.locator('a[href$="/issues"]').first().click();
  await page.getByRole("button", { name: /Repeated participant and visit-date combinations/ }).waitFor();
  await page.getByRole("button", { name: /Value does not match mostly integer column/ }).click();
  await page.getByText("176 row(s)", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Edit correction" }).click();
  const saveButton = page.getByRole("button", { name: "Save and accept" });
  const colors = await saveButton.evaluate((element) => {
    const style = getComputedStyle(element);
    return { background: style.backgroundColor, color: style.color };
  });
  assert.notEqual(colors.background, "rgb(255, 255, 255)");
  assert.notEqual(colors.background, colors.color);
  await page.locator(".before-after input").fill("40");
  await saveButton.click();
  await page.getByText(/Correction saved and accepted for 176 affected row/).waitFor();

  const safeBatch = page.getByRole("button", { name: /Accept \d+ safe corrections/ });
  assert.equal(await safeBatch.isEnabled(), true);
  await safeBatch.click();
  await page.getByText(/findings accepted/).waitFor({ timeout: 30_000 });

  await page.locator('a[href$="/files"]').first().click();
  await page.getByRole("button", { name: /healthcare_messy_data\.csv/ }).click();
  await page.getByText(/reviewed v/i).waitFor();
  await page.locator(".data-preview").getByText("40", { exact: true }).first().waitFor();
  await page.locator(".data-preview").getByText("david lee", { exact: true }).first().waitFor();

  await page.locator('a[href$="/exports"]').first().click();
  await page.getByRole("button", { name: "Generate verified clean export" }).click();
  await page.getByText("Export is ready to download.").waitFor({ timeout: 30_000 });
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download ZIP" }).click();
  const download = await downloadPromise;
  const zipPath = `${downloadRoot}/scribe-healthcare-export.zip`;
  await download.saveAs(zipPath);
  assert.deepEqual(runtimeErrors, []);
  console.log(JSON.stringify({ status: "passed", projectId, zipPath, finalUrl: page.url(), colors }, null, 2));
} finally {
  await browser.close();
}
