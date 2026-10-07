import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { ExternalRunner, REQUEST_DIR } from "../external_runner.ts";
import type { Manifest, OrchestratorConfig } from "../types.ts";

const config = { sessionTimeoutMs: 1500 } as unknown as OrchestratorConfig;
const wf = { id: "wf_0001" } as unknown as Manifest;

async function waitForRequest(root: string): Promise<string> {
  const dir = path.join(root, REQUEST_DIR);
  for (let i = 0; i < 100; i += 1) {
    try {
      const files = (await readdir(dir)).filter((name) => name.endsWith(".request.json"));
      if (files.length) return path.join(dir, files[0]);
    } catch { /* not created yet */ }
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error("no request file appeared");
}

test("an agent call becomes a request file carrying the exact task, and a done file completes it", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ext-runner-"));
  const runner = new ExternalRunner(root, config, { pollMs: 10 });
  const pending = runner.run("documenter", wf, "TASK TEXT with rules", { iteration: 2 });
  const requestFile = await waitForRequest(root);
  const request = JSON.parse(await readFile(requestFile, "utf8"));
  assert.equal(request.role, "documenter");
  assert.equal(request.workflow, "wf_0001");
  assert.equal(request.task, "TASK TEXT with rules");
  assert.equal(request.agentFile, ".github/agents/documenter.agent.md");
  assert.deepEqual(request.context, { iteration: 2 }, "a call's context is only its turn of the translate loop");
  assert.match(path.basename(requestFile), /-wf_0001-documenter-i2\.request\.json$/);
  // The role's output folder exists before the worker is asked, as CopilotRunner guarantees (and nothing
  // scoped to a segment, a batch or a dbt project does).
  assert.ok((await stat(path.join(root, "workflows", "wf_0001", "docs"))).isDirectory());
  await assert.rejects(() => stat(path.join(root, "workflows", "wf_0001", "segments")));
  await writeFile(requestFile.replace(/\.request\.json$/, ".done.json"), JSON.stringify({ toolCalls: 7 }), "utf8");
  const result = await pending;
  assert.equal(result.ok, true);
  assert.equal(result.toolCalls, 7);
});

test("a failed file becomes the named agent error; an unknown error name becomes 'error'", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ext-runner-"));
  const runner = new ExternalRunner(root, config, { pollMs: 10 });
  const pending = runner.run("analyzer", wf, "T");
  const requestFile = await waitForRequest(root);
  await writeFile(requestFile.replace(/\.request\.json$/, ".failed.json"),
    JSON.stringify({ error: "missing-output", detail: "wrote nothing" }), "utf8");
  const result = await pending;
  assert.equal(result.ok, false);
  assert.equal(result.error, "missing-output");
  assert.equal(result.detail, "wrote nothing");

  const second = runner.run("analyzer", wf, "T");
  let next = "";
  for (let i = 0; i < 100 && !next; i += 1) {
    const files = (await readdir(path.join(root, REQUEST_DIR))).filter((n) => n.endsWith(".request.json") && !n.startsWith(path.basename(requestFile).slice(0, 20)));
    next = files.find((n) => n.includes("-002-")) ?? "";
    if (!next) await new Promise((resolve) => setTimeout(resolve, 20));
  }
  await writeFile(path.join(root, REQUEST_DIR, next.replace(/\.request\.json$/, ".failed.json")),
    JSON.stringify({ error: "made-up" }), "utf8");
  assert.equal((await second).error, "error");
});

test("no answer within sessionTimeoutMs is a timeout", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "ext-runner-"));
  const runner = new ExternalRunner(root, { sessionTimeoutMs: 100 } as unknown as OrchestratorConfig, { pollMs: 10 });
  const result = await runner.run("documenter", wf, "T");
  assert.equal(result.ok, false);
  assert.equal(result.error, "timeout");
});
