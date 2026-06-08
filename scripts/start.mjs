import { spawn } from "node:child_process";
import path from "node:path";

const root = process.cwd();
const nextCli = path.join(root, "node_modules", "next", "dist", "bin", "next");
const env = {
  ...process.env,
  DUALITH_NEXT_DIST_DIR: process.env.DUALITH_NEXT_DIST_DIR ?? ".next-build",
};

const child = spawn(process.execPath, [nextCli, "start", ...process.argv.slice(2)], {
  cwd: root,
  stdio: "inherit",
  env,
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});
