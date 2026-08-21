import assert from "node:assert/strict";

const playwrightModule = await import("playwright");
const playwright = playwrightModule.chromium ? playwrightModule : playwrightModule.default;
const baseUrl = process.env.SCRIBE_UI_URL || "http://127.0.0.1:3000";
const fixture = new URL("../healthcare_messy_data.csv", import.meta.url).pathname;
const defaultChrome = process.platform === "darwin" ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" : undefined;
const browser = await playwright.chromium.launch({ headless: true, executablePath: process.env.SCRIBE_CHROME_PATH || defaultChrome });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const runtimeErrors = [];
let expectedNetworkFailure = false;
page.on("console", (message) => { if (message.type() === "error" && !expectedNetworkFailure) runtimeErrors.push(`console: ${message.text()}`); });
page.on("pageerror", (error) => runtimeErrors.push(`page: ${error.message}`));
page.on("requestfailed", (request) => { if (!expectedNetworkFailure) runtimeErrors.push(`request: ${request.url()} ${request.failure()?.errorText}`); });

async function reviewedAgeAtRow(rowNumber) {
  return page.locator(".data-preview table").evaluate((table, targetRow) => {
    const headers = [...table.querySelectorAll("thead th")].map((cell) => cell.textContent?.trim());
    const ageIndex = headers.indexOf("Age");
    const row = [...table.querySelectorAll("tbody tr")].find((item) => item.querySelector("td")?.textContent?.trim() === String(targetRow));
    return row?.querySelectorAll("td")[ageIndex]?.textContent?.trim();
  }, rowNumber);
}

try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByLabel("Project name").fill(`Manual edit acceptance ${Date.now()}`);
  await Promise.all([page.waitForURL(/\/projects\/.+\/overview/), page.getByRole("button", { name: "Create project" }).click()]);
  await page.locator('input[type="file"]').setInputFiles(fixture);
  await page.getByText(/1,000 rows, 10 columns/).waitFor({ timeout: 30_000 });

  await page.locator('a[href$="/issues"]').first().click();
  await page.getByRole("button", { name: /Missing values/ }).first().click();
  await page.getByRole("button", { name: "Make a documented change" }).click();
  await page.getByLabel("New value").fill("41");
  await page.getByLabel("Why is this change justified?").fill("Signed source form confirms age 41 for this participant.");
  const apply = page.getByRole("button", { name: "Apply correction" });
  assert.equal(await apply.isEnabled(), true);
  const colors = await apply.evaluate((element) => {
    const style = getComputedStyle(element);
    return { background: style.backgroundColor, color: style.color };
  });
  assert.notEqual(colors.background, colors.color);
  assert.equal(colors.color, "rgb(255, 255, 255)");
  await apply.click();
  await page.getByText("Evidence-backed operation applied. The original remains unchanged and the decision can be undone.").waitFor({ timeout: 30_000 });

  await page.locator('a[href$="/files"]').first().click();
  await page.getByRole("button", { name: "Reviewed", exact: true }).click();
  await page.getByText(/reviewed · 1,000 rows/i).waitFor();
  assert.equal(await reviewedAgeAtRow(2), "41");

  await page.locator('a[href$="/issues"]').first().click();
  await page.getByRole("button", { name: /Evidence-backed manual cell correction/ }).click();
  await page.getByRole("button", { name: "Undo accepted decision" }).click();
  await page.getByText("Decision reversed and reviewed copy rebuilt.").waitFor({ timeout: 30_000 });
  await page.locator('a[href$="/files"]').first().click();
  await page.getByRole("button", { name: "Reviewed", exact: true }).click();
  await page.getByText(/reviewed · 1,000 rows/i).waitFor();
  assert.notEqual(await reviewedAgeAtRow(2), "41");

  expectedNetworkFailure = true;
  await page.route("**/api/health", (route) => route.abort("connectionrefused"));
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "Scribe’s local service is unavailable" }).waitFor();
  assert.doesNotMatch(await page.locator("body").innerText(), /failed to fetch/i);
  await page.unroute("**/api/health");
  expectedNetworkFailure = false;
  await page.getByRole("button", { name: "Retry connection" }).click();
  await page.getByRole("heading", { name: /Clean research data without losing control/ }).waitFor();

  assert.deepEqual(runtimeErrors, []);
  console.log(JSON.stringify({ status: "passed", colors, manualEdit: "applied-and-undone", networkRecovery: "passed" }, null, 2));
} finally {
  await browser.close();
}
