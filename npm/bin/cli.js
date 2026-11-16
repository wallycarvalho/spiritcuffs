#!/usr/bin/env node
"use strict";

// npm wrapper: delegates to the real Python CLI (spiritcuffs) via uvx or pipx.
// No dependencies, no build step — forwards argv, stdio, and the exit code.

const { spawn, spawnSync } = require("child_process");

function resolveRunner() {
  // Prefer uvx, then pipx. spawnSync probe with stdio ignore; status 0 = exists.
  const uvx = spawnSync("uvx", ["--version"], { stdio: "ignore" });
  if (uvx.status === 0) return { cmd: "uvx", args: ["spiritcuffs@latest"] };
  const pipx = spawnSync("pipx", ["--version"], { stdio: "ignore" });
  if (pipx.status === 0) return { cmd: "pipx", args: ["run", "spiritcuffs"] };
  return null;
}

const runner = resolveRunner();
if (!runner) {
  process.stderr.write(
    "spiritcuffs: needs either `uv` (https://docs.astral.sh/uv/) or `pipx` (https://pipx.pypa.io/).\n" +
      "  install uv:   curl -LsSf https://astral.sh/uv/install.sh | sh\n" +
      "  then retry:   npx spiritcuffs <command>\n"
  );
  process.exit(1);
}

const child = spawn(runner.cmd, [...runner.args, ...process.argv.slice(2)], {
  stdio: "inherit",
});
child.on("error", (err) => {
  process.stderr.write(`spiritcuffs: failed to launch ${runner.cmd}: ${err.message}\n`);
  process.exit(1);
});
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 1);
});
