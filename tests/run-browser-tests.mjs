import { spawn } from "node:child_process";
import { access, mkdtemp } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const root = path.resolve(new URL("..", import.meta.url).pathname);
const dataRoot = await mkdtemp(path.join(os.tmpdir(), "scribe-release-"));
const python = path.join(root, ".venv", "bin", "python");

async function availablePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

async function waitFor(url, process, output) {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (process.exitCode != null) throw new Error(`Service exited early (${process.exitCode})\n${output.join("")}`);
    try { const response = await fetch(url); if (response.ok) return; } catch {}
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Timed out waiting for ${url}\n${output.join("")}`);
}

const [apiPort, uiPort] = await Promise.all([availablePort(), availablePort()]);
const commonEnv = { ...process.env, SCRIBE_DATA_DIR: dataRoot, SCRIBE_SKIP_DOTENV: "1" };
const backendOutput = [];
const frontendOutput = [];
const backend = spawn(python, ["-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", String(apiPort)], { cwd: root, env: commonEnv });
backend.stdout.on("data", (chunk) => backendOutput.push(chunk.toString()));
backend.stderr.on("data", (chunk) => backendOutput.push(chunk.toString()));

let frontend;
try {
  await waitFor(`http://127.0.0.1:${apiPort}/api/health`, backend, backendOutput);
  frontend = spawn("npm", ["run", "dev", "--", "--host", "127.0.0.1", "--port", String(uiPort), "--strictPort"], { cwd: root, env: { ...commonEnv, SCRIBE_API_PROXY_TARGET: `http://127.0.0.1:${apiPort}` } });
  frontend.stdout.on("data", (chunk) => frontendOutput.push(chunk.toString()));
  frontend.stderr.on("data", (chunk) => frontendOutput.push(chunk.toString()));
  await waitFor(`http://localhost:${uiPort}`, frontend, frontendOutput);
  process.env.SCRIBE_UI_URL = `http://localhost:${uiPort}`;
  process.env.SCRIBE_DOWNLOAD_DIR = path.join(dataRoot, "downloads");
  await import(`./browser-journey.mjs?run=${Date.now()}`);
  const cafeFixture = process.env.SCRIBE_CAFE_FIXTURE || "/Users/soksanhay/Downloads/dirty_cafe_sales.csv";
  try {
    await access(cafeFixture);
    process.env.SCRIBE_CAFE_FIXTURE = cafeFixture;
    await import(`./cafe-browser-journey.mjs?run=${Date.now()}`);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
} finally {
  for (const child of [frontend, backend]) {
    if (child && child.exitCode == null) child.kill("SIGTERM");
  }
}
