import { spawn } from "node:child_process";
import { existsSync, readFileSync, rmSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

const root = process.cwd();
const isWindows = process.platform === "win32";
const nextCli = path.join(root, "node_modules", "next", "dist", "bin", "next");
const env = { ...process.env, ...loadEnvLocal(path.join(root, ".env.local")) };
const lanMode = process.argv.includes("--lan");

function loadEnvLocal(filePath) {
  if (!existsSync(filePath)) {
    return {};
  }

  const entries = {};
  for (const line of readFileSync(filePath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }

    const equalsIndex = trimmed.indexOf("=");
    if (equalsIndex < 1) {
      continue;
    }

    const key = trimmed.slice(0, equalsIndex).trim();
    const rawValue = trimmed.slice(equalsIndex + 1).trim();
    entries[key] = rawValue.replace(/^["']|["']$/g, "");
  }

  return entries;
}

function localLanIp() {
  if (env.DUALITH_LAN_IP) {
    return env.DUALITH_LAN_IP;
  }
  const privateCandidates = [];
  const fallbackCandidates = [];
  for (const entries of Object.values(os.networkInterfaces())) {
    for (const entry of entries ?? []) {
      if (entry.family === "IPv4" && !entry.internal) {
        if (/^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(entry.address)) {
          privateCandidates.push(entry.address);
        } else {
          fallbackCandidates.push(entry.address);
        }
      }
    }
  }
  return privateCandidates[0] ?? fallbackCandidates[0] ?? "127.0.0.1";
}

const apiPort = env.DUALITH_API_PORT ?? "4000";
const webPort = env.DUALITH_WEB_PORT ?? "3000";
const lanIp = localLanIp();

if (lanMode) {
  env.DUALITH_LAN_MODE = "1";
  env.DUALITH_API_HOST = "0.0.0.0";
  env.DUALITH_WEB_HOST = "0.0.0.0";
  env.NEXT_PUBLIC_API_BASE_URL = env.DUALITH_LAN_API_BASE_URL ?? `http://${lanIp}:${apiPort}`;
}

const apiHost = env.DUALITH_API_HOST ?? "127.0.0.1";
const webHost = env.DUALITH_WEB_HOST ?? "127.0.0.1";
const apiBaseUrl = env.NEXT_PUBLIC_API_BASE_URL ?? `http://${apiHost === "0.0.0.0" ? "127.0.0.1" : apiHost}:${apiPort}`;

function clearStaleNextDevOutput() {
  const nextDir = path.join(root, ".next");
  const resolvedNextDir = path.resolve(nextDir);
  const resolvedRoot = path.resolve(root);
  const appDir = path.join(nextDir, "server", "app");
  const appPage = path.join(appDir, "page.js");
  const appManifest = path.join(appDir, "page_client-reference-manifest.js");
  const pagesDir = path.join(nextDir, "server", "pages");
  const documentPage = path.join(pagesDir, "_document.js");
  const errorPage = path.join(pagesDir, "_error.js");

  if (!existsSync(nextDir)) {
    return;
  }

  const missingAppEntrypoint = existsSync(appManifest) && !existsSync(appPage);
  const missingPagesEntrypoints = existsSync(pagesDir) && (!existsSync(documentPage) || !existsSync(errorPage));

  if (missingAppEntrypoint || missingPagesEntrypoints) {
    if (path.basename(resolvedNextDir) !== ".next" || !resolvedNextDir.startsWith(resolvedRoot + path.sep)) {
      throw new Error(`Refusing to remove unexpected Next output path: ${resolvedNextDir}`);
    }
    rmSync(resolvedNextDir, { recursive: true, force: true });
    console.log("[dualith] Cleared stale Next output before starting dev server.");
  }
}

// Prefer .venv (local), fall back to PYTHON env var, then system python.
// Some shells provide a global PYTHON that cannot import this app's FastAPI deps.
const venvPython = path.join(root, ".venv", "Scripts", isWindows ? "python.exe" : "python");
const python = existsSync(venvPython) ? venvPython : env.PYTHON ?? "python";

// On Windows, .cmd files require shell:true. Pass the full command as a string
// to avoid the Node deprecation warning about unescaped args with shell:true.
function spawnProc(cmd, args) {
  if (isWindows && cmd.endsWith(".cmd")) {
    const cmdStr = [cmd, ...args].map((a) => (a.includes(" ") ? `"${a}"` : a)).join(" ");
    return spawn(cmdStr, [], { cwd: root, stdio: "inherit", shell: true, env });
  }
  return spawn(cmd, args, { cwd: root, stdio: "inherit", shell: false, env });
}

function apiState() {
  const url = new URL("/api/health", apiBaseUrl);

  return new Promise((resolve) => {
    const req = http.request(url, { method: "GET", timeout: 1000 }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        body += chunk;
      });
      res.on("end", () => {
        try {
          const payload = JSON.parse(body);
          const features = Array.isArray(payload.features) ? payload.features : [];
          resolve(payload.app === "dualith" && features.includes("status-refresh") ? "current" : "occupied");
        } catch {
          resolve("occupied");
        }
      });
    });

    req.on("timeout", () => {
      req.destroy();
      resolve("free");
    });
    req.on("error", () => resolve("free"));
    req.end();
  });
}

const children = [];
const apiStatus = await apiState();

if (apiStatus === "current") {
  console.log(`[dualith] API already running at ${apiBaseUrl}; reusing it.`);
} else if (apiStatus === "occupied") {
  const url = new URL(apiBaseUrl);
  console.error(`[dualith] ${apiBaseUrl} is already in use by an older or incompatible server.`);
  console.error(`[dualith] Stop the port owner, then run npm run dev again:`);
  console.error(`[dualith]   Get-NetTCPConnection -LocalPort ${url.port || 4000} | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }`);
  process.exit(1);
} else {
  children.push(spawnProc(python, ["backend/run_server.py"]));
}

if (lanMode) {
  console.log(`[dualith] LAN mode enabled.`);
  console.log(`[dualith] Open on phone: http://${lanIp}:${webPort}`);
  console.log(`[dualith] API URL: ${env.NEXT_PUBLIC_API_BASE_URL}`);
}

clearStaleNextDevOutput();
children.push(spawnProc(process.execPath, [nextCli, "dev", "--hostname", webHost, "--port", webPort]));

let shuttingDown = false;

function shutdown(code = 0) {
  if (shuttingDown) {
    return;
  }

  shuttingDown = true;
  for (const child of children) {
    if (!child.killed) {
      child.kill();
    }
  }
  process.exit(code);
}

for (const child of children) {
  child.on("exit", (code, signal) => {
    if (shuttingDown) {
      return;
    }

    console.log(`[dualith] child exited${signal ? ` by ${signal}` : ` with ${code ?? 0}`}`);
    shutdown(code ?? 0);
  });
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));
