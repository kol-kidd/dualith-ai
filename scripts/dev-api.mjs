import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const root = process.cwd();
const env = { ...process.env, ...loadEnvLocal(path.join(root, ".env.local")) };
const bundledPython = path.join(
  env.USERPROFILE ?? "",
  ".cache",
  "codex-runtimes",
  "codex-primary-runtime",
  "dependencies",
  "python",
  "python.exe"
);
const venvPython = path.join(root, ".venv", "Scripts", process.platform === "win32" ? "python.exe" : "python");

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

const python = existsSync(venvPython) ? venvPython : env.PYTHON ?? (existsSync(bundledPython) ? bundledPython : "python");
const api = spawn(python, ["backend/run_server.py"], {
  cwd: root,
  stdio: "inherit",
  shell: false,
  env
});

api.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }

  process.exit(code ?? 0);
});
