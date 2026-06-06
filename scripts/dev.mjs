import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const root = process.cwd();
const isWindows = process.platform === "win32";
const nextBin = path.join(root, "node_modules", ".bin", isWindows ? "next.cmd" : "next");
const env = { ...process.env, ...loadEnvLocal(path.join(root, ".env.local")) };

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

// Prefer .venv (local), fall back to PYTHON env var, then system python
const venvPython = path.join(root, ".venv", "Scripts", isWindows ? "python.exe" : "python");
const python = env.PYTHON ?? (existsSync(venvPython) ? venvPython : "python");

// On Windows, .cmd files require shell:true. Pass the full command as a string
// to avoid the Node deprecation warning about unescaped args with shell:true.
function spawnProc(cmd, args) {
  if (isWindows && cmd.endsWith(".cmd")) {
    const cmdStr = [cmd, ...args].map((a) => (a.includes(" ") ? `"${a}"` : a)).join(" ");
    return spawn(cmdStr, [], { cwd: root, stdio: "inherit", shell: true, env });
  }
  return spawn(cmd, args, { cwd: root, stdio: "inherit", shell: false, env });
}

const children = [
  spawnProc(python, ["-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "4001"]),
  spawnProc(nextBin, ["dev"]),
];

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
