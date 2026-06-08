import { spawn } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const root = process.cwd();
const nextCli = path.join(root, "node_modules", "next", "dist", "bin", "next");
const restoredFiles = [path.join(root, "next-env.d.ts"), path.join(root, "tsconfig.json")]
  .filter((filePath) => existsSync(filePath))
  .map((filePath) => ({ filePath, content: readFileSync(filePath, "utf8") }));
const env = {
  ...process.env,
  DUALITH_NEXT_DIST_DIR: process.env.DUALITH_NEXT_DIST_DIR ?? ".next-build",
};

const child = spawn(process.execPath, [nextCli, "build"], {
  cwd: root,
  stdio: "inherit",
  env,
});

child.on("exit", (code, signal) => {
  for (const file of restoredFiles) {
    writeFileSync(file.filePath, file.content);
  }
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});
