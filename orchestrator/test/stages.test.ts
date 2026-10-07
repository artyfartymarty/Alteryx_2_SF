// The state machine: docs/spec/01-copilot-setup.md Part B §3-§5, as the objective-first design
// (docs/superpowers/specs/2026-10-06-objective-first-design.md §3) reshapes the stages. The analyze
// stage and its questions gate are tested in analyze.test.ts, the translate loop in translate.test.ts.
import { test } from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { makeEnv, readManifest } from "./fakes.ts";
import { migrateWorkflow, plannedStages, SESSION_RULE_LINES, SESSION_RULES, TERMINAL_GOOD } from "../stages.ts";
import { writeJson } from "../manifest.ts";
import { CopilotRunner } from "../runner.ts";
import type { CopilotClient, SessionHooks } from "@github/copilot-sdk";
import type { AgentCtx, AgentRunner, Env, Manifest, Role } from "../types.ts";

test("happy path reaches VALIDATED and documents", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.deepEqual([m.status.parse, m.status.intake, m.status.analyze, m.status.translate, m.status.document], ["PARSED", "READY", "DONE", "VALIDATED", "DONE"]);
  assert.equal(m.status.pr, undefined);                                   // gh is absent in the fake env
  // there is no validator agent: validate_build.py is a script the orchestrator runs
  assert.deepEqual(calls.roles, ["intake", "analyzer", "translator", "reviewer", "documenter"]);
});
test("a failing build is fixed on the second iteration", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "fix-loop" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED");
  assert.deepEqual(calls.roles.filter((r) => r === "translator" || r === "fixer"), ["translator", "fixer"]);
});
test("three failed iterations escalate to NEEDS_HUMAN and stop the workflow", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "never-fixed" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(calls.roles.filter((r) => r === "fixer").length, 2);
  assert.ok(!calls.roles.includes("documenter"));
});
test("two failed recoveries quarantine the workflow as T3", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0005", scenario: "recovery-fails" });
  const m = await migrateWorkflow(env, "wf_0005", {});
  assert.deepEqual([m.status.parse, m.tier], ["QUARANTINED", "T3"]);
  assert.equal(calls.roles.filter((r) => r === "parser-recovery").length, 2);
});
test("a recovered T3 workflow ends MANUAL without translating", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0005" });
  const m = await migrateWorkflow(env, "wf_0005", {});
  // Tier T3 short-circuits analyze straight to MANUAL: the analyzer still writes (and the checker still
  // passes) an objective, but no golden data, build file or review is ever asked for.
  assert.deepEqual(
    [m.status.parse, m.tier, m.status.analyze, m.status.translate],
    ["PARSED", "T3", "DONE", "MANUAL"],
  );
  assert.equal(m.reasons?.translate, "tier-T3");
  assert.deepEqual(calls.roles, ["parser-recovery", "intake", "analyzer"]);
  assert.ok(!calls.roles.includes("translator") && !calls.roles.includes("reviewer"));
});
test("re-running a T3 workflow already at MANUAL does nothing (golden is never applicable)", async () => {
  // docs/spec/01-copilot-setup.md's state diagram draws `analyze --> MANUAL: tier T3` as a dead
  // end with no outgoing edge at all — golden/document/pr never apply to a T3 workflow, on the
  // first pass or any later one. The first pass never even reaches golden's shouldRun check
  // (stageAnalyze's own "stop" breaks the loop first), so only a SECOND migrateWorkflow call,
  // restarting the stage loop from the top, can expose golden being attempted anyway.
  const { env, calls } = await makeEnv({ wf: "wf_0005" });
  const first = await migrateWorkflow(env, "wf_0005", {});
  assert.equal(first.status.translate, "MANUAL");
  assert.equal(first.status.golden, undefined);
  const n = calls.roles.length;
  const nPy = calls.py.length;

  const second = await migrateWorkflow(env, "wf_0005", {});
  assert.equal(second.status.golden, undefined, "golden must stay untouched, not BLOCKED");
  assert.equal(second.status.translate, "MANUAL");
  assert.equal(calls.roles.length, n, "no agent ran again");
  assert.equal(calls.py.filter((c) => c.script.includes("alteryx_sim")).length, 0, "the simulator was never invoked for a T3 workflow");
  assert.equal(calls.py.length, nPy, "no script ran again");
});
test("rate limits back off and retry; a denied tool escalates at once", async () => {
  const a = await makeEnv({ wf: "wf_0001", agentErrors: { analyzer: ["rate-limit", "rate-limit"] } });
  assert.equal((await migrateWorkflow(a.env, "wf_0001", {})).status.analyze, "DONE");
  assert.deepEqual(a.calls.sleeps, [1000, 2000]);
  const b = await makeEnv({ wf: "wf_0001", agentErrors: { analyzer: ["denied"] } });
  assert.equal((await migrateWorkflow(b.env, "wf_0001", {})).status.analyze, "NEEDS_HUMAN");
});
test("re-running a finished workflow does nothing; --from-stage reopens it", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001" });
  await migrateWorkflow(env, "wf_0001", {}); const n = calls.roles.length;
  await migrateWorkflow(env, "wf_0001", {}); assert.equal(calls.roles.length, n);
  await migrateWorkflow(env, "wf_0001", { fromStage: "translate" }); assert.ok(calls.roles.length > n);
});
test("analyze's WAITING_FOR_ANSWERS is neither terminal nor an escalation", async () => {
  assert.deepEqual(TERMINAL_GOOD.analyze, ["DONE"]);
  const { env } = await makeEnv({ wf: "wf_0001", scenario: "objective-questions" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.analyze, "WAITING_FOR_ANSWERS");
  assert.deepEqual(plannedStages(m, {}).slice(0, 1), ["analyze"], "the stage is planned again: it re-runs every pass");
});

// --- behaviour the brief states in prose but does not test ---

test("the happy path leaves the artifacts the next stage reads", async () => {
  const { env, files } = await makeEnv({ wf: "wf_0001" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.deepEqual(m.golden_sets, ["normal", "period_end", "empty", "edge"]);
  assert.equal(m.tier, "T1");
  assert.equal(m.language, "sql");
  for (const file of [
    ["intake", "plan.md"], ["digest.md"], ["objective.json"], ["objective.md"], ["build.sql"], ["translation_notes.md"],
    ["compile_check.json"], ["review.json"], ["validation.json"], ["docs", "mapping.md"], ["docs", "migration.md"],
    ["dbt", "dbt_project.yml"], ["dbt", "validation.json"], // a SQL workflow's dbt project (design D9)
  ]) {
    assert.equal(await files.exists("workflows", "wf_0001", ...file), true, file.join("/"));
  }
  for (const gone of [["segments"], ["procs"], ["analysis.md"], ["build.py"]]) {
    assert.equal(await files.exists("workflows", "wf_0001", ...gone), false, `${gone.join("/")} is not part of the flow any more`);
  }
  const onDisk = await readManifest(env.root, "wf_0001");
  assert.equal(onDisk.status.translate, "VALIDATED", "the manifest is saved, not just returned");
  assert.equal(onDisk.language, "sql");
});
test("a manifest written by the segment-based pipeline is read without complaint and its old fields are ignored", async () => {
  const { env, calls } = await makeEnv({
    wf: "wf_0001",
    manifest: {
      segments: ["seg_01"], segment_status: { seg_01: "NEEDS_HUMAN" }, segmentation: { max_prompt_chars: 1 },
      output_kind: "dbt", output_target: "dbt",
    },
  });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED", "a recorded NEEDS_HUMAN segment parks nothing any more");
  assert.ok(!calls.py.some((c) => /segment|target_check|plan_batches|check_seams|stitch/.test(c.script)));
});
test("a workflow the simulator cannot produce golden data for stops at golden", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", golden: "empty" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.golden, "BLOCKED");
  assert.equal(m.status.translate, undefined);
  assert.ok(!calls.roles.includes("translator"));
});
test("the alteryx producer stops at golden with instructions instead of guessing data", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", config: { golden: { producer: "alteryx" } } });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.golden, "BLOCKED");
  assert.ok(calls.logs.some((line) => line.includes("inject_outputs.py")));
  assert.equal(calls.py.filter((c) => c.script.includes("alteryx_sim")).length, 0);
});
test("intake BLOCKED stops the workflow before analyze, as it always did", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", intake: "BLOCKED" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.intake, "BLOCKED");
  assert.ok(!calls.roles.includes("analyzer"));
  assert.ok(calls.logs.some((line) => line.includes("intake BLOCKED") && line.includes("open_questions.md")));
});
test("a PR is opened only when gh is installed, titled with the tier and the language", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", hasGh: true, ghEnabled: true });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.pr, "OPEN");
  assert.deepEqual(calls.sh[0].args.slice(0, 2), ["pr", "create"]);
  assert.equal(calls.sh[0].args[calls.sh[0].args.indexOf("--title") + 1], "migrate(wf_0001): T1, sql");

  const python = await makeEnv({ wf: "wf_0001", hasGh: true, ghEnabled: true, language: "python" });
  await migrateWorkflow(python.env, "wf_0001", {});
  assert.equal(python.calls.sh[0].args[python.calls.sh[0].args.indexOf("--title") + 1], "migrate(wf_0001): T2, python");

  const offline = await makeEnv({ wf: "wf_0001", ghEnabled: true });
  const n = await migrateWorkflow(offline.env, "wf_0001", {});
  assert.equal(n.status.pr, undefined);
  assert.ok(offline.calls.logs.some((line) => /gh not installed/.test(line)));
});
// Task P4 fix round 1, B3: GitHub is opt-in. gh installed but GitHub integration off: no issue, no
// PR, no gh call of any kind -- the stages log `gh: disabled` and carry on exactly as without gh.
test("with GitHub integration off, an installed gh is never called and the stages log gh: disabled", async () => {
  const done = await makeEnv({ wf: "wf_0001", hasGh: true, ghEnabled: false });
  const m = await migrateWorkflow(done.env, "wf_0001", {});
  assert.equal(m.status.document, "DONE");
  assert.equal(m.status.pr, undefined);
  assert.equal(done.calls.sh.length, 0, "no gh pr create");
  assert.ok(done.calls.logs.some((line) => line.includes("gh: disabled")));
});
test("the fake env, like the orchestrator, has GitHub integration off by default", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", hasGh: true });
  await migrateWorkflow(env, "wf_0001", {});
  assert.equal(calls.sh.length, 0);
});
test("--stop-after returns before the next stage and --dry-run touches nothing", async () => {
  const stopped = await makeEnv({ wf: "wf_0001" });
  const m = await migrateWorkflow(stopped.env, "wf_0001", { stopAfter: "analyze" });
  assert.equal(m.status.analyze, "DONE");
  assert.equal(m.status.translate, undefined);
  assert.deepEqual(stopped.calls.roles, ["intake", "analyzer"]);

  const dry = await makeEnv({ wf: "wf_0001" });
  const d = await migrateWorkflow(dry.env, "wf_0001", { dryRun: true });
  assert.deepEqual(d.status, {});
  assert.equal(dry.calls.py.length, 0);
  assert.equal(dry.calls.roles.length, 0);
  assert.ok(dry.calls.logs.some((line) => line.includes("parse")));
});
test("--from-stage clears that stage and every later one", async () => {
  const { env } = await makeEnv({ wf: "wf_0001" });
  await migrateWorkflow(env, "wf_0001", {});
  const m = await migrateWorkflow(env, "wf_0001", { fromStage: "analyze", stopAfter: "analyze" });
  assert.equal(m.status.analyze, "DONE");
  assert.equal(m.status.parse, "PARSED", "earlier stages keep their status");
  assert.equal(m.status.translate, undefined, "later stages were cleared");
  assert.equal(m.status.document, undefined);
});
test("a missing objective makes the analyzer retry once, then NEEDS_HUMAN", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", agentErrors: { analyzer: ["missing-output"] } });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.analyze, "DONE", "one retry is enough when the second attempt writes the objective");
  assert.equal(calls.roles.filter((r) => r === "analyzer").length, 2);

  const twice = await makeEnv({ wf: "wf_0001", agentErrors: { analyzer: ["missing-output", "missing-output"] } });
  const n = await migrateWorkflow(twice.env, "wf_0001", {});
  assert.equal(n.status.analyze, "NEEDS_HUMAN");
  assert.equal(twice.calls.roles.filter((r) => r === "analyzer").length, 2);
});
test("four rate limits exhaust the retries and escalate", async () => {
  const { env, calls } = await makeEnv({
    wf: "wf_0001",
    agentErrors: { analyzer: ["rate-limit", "rate-limit", "rate-limit", "rate-limit"] },
  });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.analyze, "NEEDS_HUMAN");
  assert.deepEqual(calls.sleeps, [1000, 2000, 4000]);
  assert.equal(calls.roles.filter((r) => r === "analyzer").length, 4);
});
test("a tool-call budget overrun stops the workflow before the next agent", async () => {
  const { env, calls } = await makeEnv({
    wf: "wf_0001",
    manifest: { metrics: { translator: { toolCalls: 401 } } },
  });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.intake, "NEEDS_HUMAN");
  assert.equal(calls.roles.length, 0);
  assert.ok(calls.logs.some((line) => line.includes("budget")));
});
test("a script that exits 2 is an orchestrator error, not a domain verdict", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001" });
  const py = env.py;
  env.py = async (script, args, opts) => (script.endsWith("digest.py") ? { ok: false, code: 2, out: "", err: "usage: digest.py" } : py(script, args, opts));
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.analyze, "NEEDS_HUMAN");
  assert.equal(m.reasons!.analyze, "script-error");
  assert.ok(!calls.roles.includes("analyzer"));
});
// --- escalated statuses stay parked until --from-stage reopens them (fix round 1) -------------
// Coordinator ruling (task-15-int, fix round 1): NEEDS_HUMAN / QUARANTINED / golden's BLOCKED are
// not "nothing left to do" (TERMINAL_GOOD) and they are not "not started yet" either -- they are
// PARKED. A plain re-run must not re-invoke the stage's scripts or agents, and must not let any
// later stage run, until an operator explicitly reopens the workflow with --from-stage.

test("needs_human keeps a workflow parked: a second run makes no new calls and logs the resume line", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "needs-human" });
  const first = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(first.status.translate, "NEEDS_HUMAN");
  const rolesAfterFirst = calls.roles.length;
  const pyAfterFirst = calls.py.length;

  const second = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(second.status.translate, "NEEDS_HUMAN");
  assert.equal(calls.roles.length, rolesAfterFirst, "no new agent calls on the second run");
  assert.equal(calls.py.length, pyAfterFirst, "no new script calls on the second run");
  assert.ok(
    calls.logs.some((line) => line.includes("--from-stage translate --only wf_0001")),
    "the resume line is logged",
  );
});

test("a quarantined workflow stays parked: a second run makes no new parse/parser-recovery calls", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0005", scenario: "recovery-fails" });
  const first = await migrateWorkflow(env, "wf_0005", {});
  assert.deepEqual([first.status.parse, first.tier], ["QUARANTINED", "T3"]);
  const rolesAfterFirst = calls.roles.length;
  const parseCallsAfterFirst = calls.py.filter((c) => c.script.endsWith("parse.py")).length;

  const second = await migrateWorkflow(env, "wf_0005", {});
  assert.equal(second.status.parse, "QUARANTINED");
  assert.equal(calls.roles.length, rolesAfterFirst, "no new parser-recovery calls on the second run");
  assert.equal(
    calls.py.filter((c) => c.script.endsWith("parse.py")).length,
    parseCallsAfterFirst,
    "no new parse.py calls on the second run",
  );
  assert.ok(
    calls.logs.some((line) => line.includes("--from-stage parse --only wf_0005")),
    "the resume line is logged",
  );
});

test("a golden BLOCKED workflow stays parked on a second run", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", golden: "empty" });
  const first = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(first.status.golden, "BLOCKED");
  const simCallsAfterFirst = calls.py.filter((c) => c.script.includes("alteryx_sim")).length;

  const second = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(second.status.golden, "BLOCKED");
  assert.equal(
    calls.py.filter((c) => c.script.includes("alteryx_sim")).length,
    simCallsAfterFirst,
    "the simulator is not re-run",
  );
  assert.ok(!calls.roles.includes("translator"));
  assert.ok(calls.logs.some((line) => line.includes("--from-stage golden --only wf_0001")));
});

test("--from-stage translate reopens a NEEDS_HUMAN workflow and re-invokes the translator", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "needs-human" });
  await migrateWorkflow(env, "wf_0001", {});
  assert.equal(calls.roles.filter((r) => r === "translator").length, 1);

  const m = await migrateWorkflow(env, "wf_0001", { fromStage: "translate" });
  assert.equal(calls.roles.filter((r) => r === "translator").length, 2, "the translator ran again");
  assert.notEqual(m.status.translate, undefined);
});

test("--from-stage on a later stage does not skip past an earlier parked stage", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", golden: "empty" });
  const first = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(first.status.golden, "BLOCKED");
  const rolesAfterFirst = calls.roles.length;
  const simCallsAfterFirst = calls.py.filter((c) => c.script.includes("alteryx_sim")).length;

  // "document" comes after "golden" in STAGES order; --from-stage document clears document/pr
  // but must not silently reopen, or step over, the still-parked golden stage.
  const second = await migrateWorkflow(env, "wf_0001", { fromStage: "document" });
  assert.equal(second.status.golden, "BLOCKED", "golden was not touched by clearFromStage");
  assert.equal(second.status.document, undefined, "document was cleared but never reached");
  assert.equal(calls.roles.length, rolesAfterFirst, "no agent ran -- the parked golden stage still blocks");
  assert.equal(
    calls.py.filter((c) => c.script.includes("alteryx_sim")).length,
    simCallsAfterFirst,
    "golden's own script did not re-run either",
  );
});

test("plannedStages for a parked workflow is empty", async () => {
  const { env } = await makeEnv({ wf: "wf_0001", scenario: "needs-human" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.deepEqual(plannedStages(m, {}), []);
});

test("intake's WAITING_FOR_ANSWERS is not an escalation: intake scripts still re-run every pass", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", intake: "WAITING_FOR_ANSWERS" });
  await migrateWorkflow(env, "wf_0001", {});
  const touchpointsAfterFirst = calls.py.filter((c) => c.script.endsWith("intake_touchpoints.py")).length;
  const promptAfterFirst = calls.py.filter((c) => c.script.endsWith("intake_prompt.py")).length;

  await migrateWorkflow(env, "wf_0001", {});
  assert.equal(
    calls.py.filter((c) => c.script.endsWith("intake_touchpoints.py")).length,
    touchpointsAfterFirst + 1,
    "intake_touchpoints.py ran again",
  );
  assert.equal(
    calls.py.filter((c) => c.script.endsWith("intake_prompt.py")).length,
    promptAfterFirst + 1,
    "intake_prompt.py ran again",
  );
});

// --- F8 (ruling): --from-stage grants a fresh tool-call budget --------------------------------

test("F8: a plain re-run of an over-budget workflow still parks; --from-stage grants a fresh budget and lets agents run again", async () => {
  const stuck = await makeEnv({ wf: "wf_0001", manifest: { metrics: { translator: { toolCalls: 401 } } } });
  const first = await migrateWorkflow(stuck.env, "wf_0001", {});
  assert.equal(first.status.intake, "NEEDS_HUMAN");
  assert.equal(stuck.calls.roles.length, 0);

  // A plain re-run (no --from-stage) must still park -- the budget is not reset for free.
  const second = await migrateWorkflow(stuck.env, "wf_0001", {});
  assert.equal(second.status.intake, "NEEDS_HUMAN");
  assert.equal(stuck.calls.roles.length, 0);

  const reopened = await migrateWorkflow(stuck.env, "wf_0001", { fromStage: "intake" });
  assert.notEqual(reopened.status.intake, "NEEDS_HUMAN", "the reopened workflow gets past intake");
  assert.ok(stuck.calls.roles.length > 0, "agents ran again after the budget reset");
  assert.ok(
    stuck.calls.logs.some((line) => line.includes("reset the tool-call budget")),
    "the reset is logged",
  );
});

test("F8: metrics after the reopened run count only the NEW calls, not the stale spend --from-stage reset", async () => {
  const { env } = await makeEnv({ wf: "wf_0001", manifest: { metrics: { translator: { toolCalls: 401, lastMs: 999 } } } });
  await migrateWorkflow(env, "wf_0001", {}); // parks; budget untouched
  const reopened = await migrateWorkflow(env, "wf_0001", { fromStage: "intake" });
  assert.equal(reopened.metrics.translator?.toolCalls, 0, "the stale role's toolCalls was reset to 0");
  assert.equal(reopened.metrics.translator?.lastMs, 999, "lastMs (a duration, not spend) is kept, per the ruling");
  // The real workflow's own agent calls (MockRunner records 0 toolCalls per call) must not have
  // silently re-inflated the budget back past where it started.
  const total = Object.values(reopened.metrics as Record<string, { toolCalls?: number }>).reduce(
    (sum, entry) => sum + (entry.toolCalls ?? 0),
    0,
  );
  assert.ok(total < 401, "the workflow's total spend after the reset is nowhere near the old budget overrun");
});

test("F8: a --from-stage on a workflow that was never over budget logs nothing about a reset", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001" });
  await migrateWorkflow(env, "wf_0001", {});
  await migrateWorkflow(env, "wf_0001", { fromStage: "translate" });
  assert.ok(!calls.logs.some((line) => line.includes("reset the tool-call budget")));
});

// --- F10: the real-Alteryx golden path prints a command inject_outputs.py's argparse accepts ---

test("F10: the alteryx golden instructions name both required steps with flags the script's parser accepts", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", config: { golden: { producer: "alteryx" } } });
  await migrateWorkflow(env, "wf_0001", {});
  const message = calls.logs.find((line) => line.includes("inject_outputs.py"));
  assert.ok(message, "an alteryx-golden instruction line was logged");
  // scripts/inject_outputs.py's argparse: `--capture-dir` is REQUIRED on every invocation, and
  // only a SECOND pass with `--import-set <name>` turns captures into golden CSVs -- neither flag
  // was in the old one-line message.
  assert.match(message!, /--capture-dir/);
  assert.match(message!, /--import-set/);
  assert.match(message!, new RegExp(`inject_outputs\\.py wf_0001 --capture-dir \\S+`));
  assert.match(message!, new RegExp(`inject_outputs\\.py wf_0001 --capture-dir \\S+ --import-set \\S+`));
});

// ---------- notes files and inline context ----------

test("intake and the analyzer get their inputs inline: prompt_context.py for intake, the digest for the analyzer", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001" });
  await migrateWorkflow(env, "wf_0001", {});

  const intakeCall = calls.py.find((c) => c.script === "scripts/prompt_context.py");
  assert.deepEqual(intakeCall!.args, ["wf_0001", "--role", "intake", "--budget-chars", "16000"]);
  assert.equal(calls.py.filter((c) => c.script === "scripts/prompt_context.py").length, 1, "the analyzer has no prompt_context.py call");
  assert.ok(
    calls.order.indexOf("py:scripts/prompt_context.py") < calls.order.indexOf("agent:intake"),
    "the intake context is rendered before the intake agent runs",
  );
  assert.ok(calls.tasks.find((t) => t.role === "intake")!.task.endsWith("## Inline context for intake (fake)\n- tool 1 input"));

  assert.ok(calls.order.indexOf("py:scripts/digest.py") < calls.order.indexOf("agent:analyzer"), "digest.py runs before the analyzer");
  const analyzerTask = calls.tasks.find((t) => t.role === "analyzer")!.task;
  assert.match(analyzerTask, /## Inline context for analyzer \(workflows\/wf_0001\/digest\.md/);
  assert.match(analyzerTask, /T2 Filter {2}\[REGION\] != "WEST"/, "the digest's own lines are in the task");
});

test("a prompt_context failure is logged and the intake agent still runs without the block", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "context-fails" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED", "a broken renderer never stops the workflow");
  assert.ok(calls.roles.includes("intake") && calls.roles.includes("analyzer"));

  const intakeTask = calls.tasks.find((t) => t.role === "intake")!.task;
  assert.doesNotMatch(intakeTask, /Inline context/);
  assert.equal(calls.logs.filter((line) => line.includes("prompt_context.py")).length, 1);
  // Task W4: the notes sentence is part of the INSTRUCTIONS, not the fenced context block -- it
  // survives even when that block failed to render.
  assert.match(intakeTask, /workflows\/wf_0001\/notes\/intake\.md/);
});

test("the intake, analyzer and fixer tasks name their notes file", async () => {
  const { calls, env } = await makeEnv({ wf: "wf_0001", scenario: "fix-loop" });
  await migrateWorkflow(env, "wf_0001", {});
  const intakeTask = calls.tasks.find((t) => t.role === "intake")!.task;
  const analyzerTask = calls.tasks.find((t) => t.role === "analyzer")!.task;
  const fixerTask = calls.tasks.find((t) => t.role === "fixer")!.task;
  const durable = "the durable record stays in the files you write";
  // Task N1: the fixed sentence telling the agent the directory already exists and not to create
  // one -- live evidence (docs/live-smoke-test.md "Third live test") showed every intake session
  // trying to create workflows/<wf>/notes/ itself before writing its notes file.
  const exists = "The directory already exists; write the file with your file-writing tool; do not create directories.";
  assert.match(intakeTask, /workflows\/wf_0001\/notes\/intake\.md/);
  assert.match(intakeTask, new RegExp(durable));
  assert.ok(intakeTask.includes(exists), intakeTask);
  assert.match(analyzerTask, /workflows\/wf_0001\/notes\/analyzer\.md/);
  assert.match(analyzerTask, new RegExp(durable));
  assert.ok(analyzerTask.includes(exists), analyzerTask);
  assert.match(fixerTask, /workflows\/wf_0001\/notes\/fixer\.md/);
  assert.match(fixerTask, new RegExp(durable));
  assert.ok(fixerTask.includes(exists), fixerTask);
  // Part of the INSTRUCTIONS, before any fenced inline context (Task F: the data fence never
  // carries an instruction) -- intake and analyzer both get one appended after it.
  assert.ok(intakeTask.indexOf(exists) < intakeTask.indexOf("## Inline context"), intakeTask);
  assert.ok(analyzerTask.indexOf(exists) < analyzerTask.indexOf("## Inline context"), analyzerTask);
});

// ---------- live hardening, Task L1 (R4) and later: the fixed session rules ----------
// Live evidence (docs/live-smoke-test.md "Third live test"): nothing told the model how to navigate. Task L7
// (R3) added a fifth rule on a translator that read 54 files of the pipeline's own source to debug a diff; Task
// L8 (R3) a sixth, on a translator that read every golden CSV of every set and overflowed its context; Task L9
// (R3) a seventh, on a documenter session that parked calling the SDK's own `web_fetch`. Objective-first design
// §5: rules 5 and 6 name the objective where they named the contract.

test("L1 R4 / L7 R3 / L8 R3 / L9 R3: the session rules are a constant that says the seven things", () => {
  assert.equal(SESSION_RULE_LINES.length, 7);
  const squeezed = SESSION_RULES.replace(/\s+/g, " ");
  for (const phrase of [
    "relative to the repository root", "workflows/<id>/", "never type an absolute path",
    "glob", "view", "grep", "`python scripts/<name>.py …`",
    "only workflows/<id>/ is yours", "never open another workflow's folder",
    "A refused tool call is final", "do not retry it in another form or through another tool",
    "say so in your notes", "and stop",
    "Do not read this pipeline's own scripts/ or orchestrator/ source to debug a difference",
    "the validation report, the objective, the cookbook and docs/reference/ are the evidence",
    "Never read the golden data in bulk", "the objective describes every column",
    "at most one golden set's inputs", "validate_build.py compares the rest",
    "There is no network in these sessions", "every page you need is in the repository",
    "`cookbook/`", "`docs/reference/`",
  ]) {
    assert.ok(squeezed.includes(phrase), phrase);
  }
  assert.doesNotMatch(SESSION_RULES, /contract/, "the objective replaced the contract in rules 5 and 6");
  for (const rule of SESSION_RULE_LINES) assert.ok(SESSION_RULES.includes(rule), rule);
  assert.doesNotMatch(SESSION_RULES, /wf_\d|seg_\d|batch_\d/, "no workflow's own text or id");
});

test("L1 R4: every role's task, in every form, carries the session rules once, before any inline context", async () => {
  const objectiveQuestions = await makeEnv({ wf: "wf_0001", scenario: "objective-questions" });
  await migrateWorkflow(objectiveQuestions.env, "wf_0001", {});
  objectiveQuestions.answerObjective();
  const runs: [Awaited<ReturnType<typeof makeEnv>>, string][] = [
    [await makeEnv({ wf: "wf_0001", scenario: "fix-loop" }), "wf_0001"],
    [await makeEnv({ wf: "wf_0005" }), "wf_0005"],
    [objectiveQuestions, "wf_0001"],   // resumed with its answers: the analyzer's revision session
  ];
  const forms = new Set<string>();
  let fenced = 0;
  for (const [{ env, calls }, id] of runs) {
    await migrateWorkflow(env, id, {});
    for (const { role, task } of calls.tasks) {
      forms.add(role);
      assert.equal(task.split(SESSION_RULES).length, 2, `${role}: the paragraph exactly once\n${task}`);
      const fence = task.indexOf("## Inline context");
      if (fence >= 0) {
        fenced += 1;
        assert.ok(task.indexOf(SESSION_RULES) < fence, `${role}: the rules come before the inline context\n${task}`);
      }
    }
  }
  for (const role of ["parser-recovery", "intake", "analyzer", "translator", "reviewer", "fixer", "documenter"]) {
    assert.ok(forms.has(role), `${role} was not exercised; saw ${[...forms].join(", ")}`);
  }
  assert.ok(fenced >= 4, `the inline-context forms were exercised (${fenced})`);
});

test("L1 R4: .github/copilot-instructions.md mirrors every session rule", async () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const text = (await readFile(path.join(here, "..", "..", ".github", "copilot-instructions.md"), "utf8")).replace(/\s+/g, " ");
  for (const rule of SESSION_RULE_LINES) assert.ok(text.includes(rule), rule);
});

// ---------- live hardening, Task L7 (R2): a timed-out session's checked output is kept ----------
// Live evidence (task-L7-brief.md): a translator wrote a valid, once-validated procedure and then
// spent the rest of its session reading source trying to debug a genuine semantics diff, until its
// session timed out -- and the orchestrator discarded it and retranslated from scratch
// (RETRY_ONCE). Since R2, a session that ends `timeout` has the stage's own `verify` run against it
// first; if it now passes, the result is accepted instead of retried.

test("L7 R2: a translator that timed out after writing a build.sql that passes its check is kept, not retried", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001" });
  const recording = env.runner;
  let timedOutOnce = false;
  env.runner = {
    async run(role, wf, task, ctx) {
      const result = await recording.run(role, wf, task, ctx);
      // The translator's real MockRunner replay already wrote a compiling build.sql; only its own
      // AgentResult is misreported as a timeout, exactly once -- the SAME shape a real SDK session
      // ending mid-cleanup would leave: the output on disk, the session's own outcome `timeout`.
      if (role === "translator" && !timedOutOnce) {
        timedOutOnce = true;
        return { ok: false, error: "timeout", detail: "Timeout after 2700000ms waiting for session.idle", toolCalls: result.toolCalls, ms: result.ms };
      }
      return result;
    },
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED");
  const translatorCalls = calls.tasks.filter((t) => t.role === "translator");
  assert.equal(translatorCalls.length, 1, "kept, not retried -- one translator session, not two");
  assert.equal(m.metrics.translator?.timeouts, 1, "the kept timeout is counted in the role's metrics");
  assert.ok(
    calls.logs.some((line) => line.includes("wf_0001: translator timed out after writing an output that passes its check — kept")),
    calls.logs.join("\n"),
  );
});

// L6 fix round 2 (I3), through the REAL CopilotRunner and its policy hooks: the live-review shape. A
// translator session writes a build file that passes its check, tries three severe things -- another
// workflow's file, a network command, a write into the pipeline's scripts/ -- each refused by the real
// policy, and then ends by a timeout, a rate limit or a context overflow. Before the fix the timeout was
// kept by L7's path and the workflow reached VALIDATED; every ending now parks the stage `denied`.
const SEVERE_TRANSLATOR_CALLS = [
  { toolName: "view", toolArgs: { path: "workflows/wf_0002/manifest.json" } },
  { toolName: "powershell", toolArgs: { command: "curl.exe https://example.invalid/x" } },
  { toolName: "create", toolArgs: { path: "scripts/_diag.py", file_text: "print(1)\n" } },
];

/** A runner whose translator sessions go through a real `CopilotRunner` with a fake SDK client: the
 * session replays the canned output (the recording MockRunner), runs `SEVERE_TRANSLATOR_CALLS` through the
 * policy's own onPreToolUse hook, and then throws `ending`. Every other role runs as before. */
function severeTranslator(env: Env, recording: AgentRunner, ending: Error): AgentRunner {
  let current: { role: Role; wf: Manifest; task: string; ctx?: AgentCtx } | undefined;
  const client = {
    async createSession(config: { hooks?: SessionHooks }) {
      const hooks = config.hooks!;
      const base = { sessionId: "session-1", timestamp: new Date(), workingDirectory: env.root };
      return {
        on: () => () => undefined,
        async send() {
          return "";
        },
        async sendAndWait() {
          await recording.run(current!.role, current!.wf, current!.task, current!.ctx);
          for (const call of SEVERE_TRANSLATOR_CALLS) {
            const decision = await hooks.onPreToolUse!({ ...base, ...call }, { sessionId: "session-1" });
            assert.equal((decision as { permissionDecision?: string }).permissionDecision, "deny", call.toolName);
          }
          throw ending;
        },
        async disconnect() {
          return undefined;
        },
      };
    },
  } as unknown as CopilotClient;
  const copilot = new CopilotRunner({ client, root: env.root, config: env.config, profile: "local", agents: [] });
  copilot.attach(env);
  return {
    async run(role, wf, task, ctx) {
      if (role !== "translator") return recording.run(role, wf, task, ctx);
      current = { role, wf, task, ctx };
      return copilot.run(role, wf, task, ctx);
    },
  };
}

for (const [how, ending] of [
  ["a timeout", new Error("Timeout after 2700000ms waiting for session.idle")],
  ["a rate limit", new Error("HTTP 429 rate limit exceeded")],
  ["a context overflow", new Error("400 request (34965 tokens) exceeds the available context size (32768 tokens), try increasing it")],
] as [string, Error][]) {
  test(`L6 fix 2 (I3): a real CopilotRunner session with severe denials that ends by ${how} parks denied -- never kept`, async () => {
    const { env, calls } = await makeEnv({ wf: "wf_0001" });
    env.runner = severeTranslator(env, env.runner, ending);
    const m = await migrateWorkflow(env, "wf_0001", {});
    assert.equal(m.status.translate, "NEEDS_HUMAN", `${how}: the stage parks`);
    assert.match(String(m.reasons?.translate), /denied/, how);
    assert.equal(calls.tasks.filter((t) => t.role === "translator").length, 1, `${how}: parked at once -- not kept, not retried`);
    assert.equal(m.metrics.translator?.timeouts, undefined, `${how}: never kept`);
    assert.ok(calls.logs.some((line) => line.includes("severe-denials: 3 (parks at once)")), calls.logs.join("\n"));
    assert.ok(!calls.logs.some((line) => line.includes("— kept")), how);
  });
}

// The same guard in runAgent itself, for a runner that reports the ending but not the park: a result that
// carries severe denials is never kept, whatever `error` it names.
test("L6 fix 2 (I3): runAgent parks any runner's result that carries a severe denial -- never kept", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001" });
  const recording = env.runner;
  env.runner = {
    async run(role, wf, task, ctx) {
      const result = await recording.run(role, wf, task, ctx);
      if (role !== "translator") return result;
      return { ok: false, error: "timeout", detail: "Timeout after 2700000ms waiting for session.idle", toolCalls: result.toolCalls, ms: result.ms, severeDenials: 1 };
    },
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.match(String(m.reasons?.translate), /denied/);
  assert.equal(calls.tasks.filter((t) => t.role === "translator").length, 1);
  assert.equal(m.metrics.translator?.timeouts, undefined);
});

test("L7 R2: a translator that timed out with nothing on disk still retries once, same as before R2", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", agentErrors: { translator: ["timeout"] } });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED", "the retry succeeds");
  const translatorCalls = calls.tasks.filter((t) => t.role === "translator");
  assert.equal(translatorCalls.length, 2, "the existing RETRY_ONCE path is unchanged when verify still fails");
  assert.equal(translatorCalls[1].task, translatorCalls[0].task, "a timeout's retry carries no feedback block, same as before R2");
  assert.equal(m.metrics.translator?.timeouts, undefined, "never kept, so never counted");
});

test("L7 R2: a fixer that timed out after repairing build.sql to a passing compile is kept, not retried", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "fix-loop" });
  const recording = env.runner;
  let timedOutOnce = false;
  env.runner = {
    async run(role, wf, task, ctx) {
      const result = await recording.run(role, wf, task, ctx);
      if (role === "fixer" && !timedOutOnce) {
        timedOutOnce = true;
        return { ok: false, error: "timeout", detail: "Timeout after 2700000ms waiting for session.idle", toolCalls: result.toolCalls, ms: result.ms };
      }
      return result;
    },
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED");
  const fixerCalls = calls.tasks.filter((t) => t.role === "fixer");
  assert.equal(fixerCalls.length, 1, "kept, not retried");
  assert.equal(m.metrics.fixer?.timeouts, 1);
});

// ---------- live hardening, Task L7 fix round 1/2 (R-b, IMP-2): only an output CHANGED this session is kept ----------
// Review I2 (fix round 1): the fixer's `verify` is `fileExists(build file)`, which the translator's iteration 0
// already satisfied -- so a fixer that timed out having changed NOTHING was still logged "kept". Review IMP-2
// (fix round 2): the mtime-based `freshSince` was gameable by housekeeping the orchestrator or the agent itself
// writes near the same instant (`compile_check.json`, `fix_log.md`). The check is by CONTENT (`sameContent` /
// `snapshot` in stages.ts) of the build files alone: no clock, no tolerance, so nothing written before or after
// an attempt, and no file outside the translation's own lane, can be mistaken for a real edit.

/** What a timed-out fixer may leave behind without having touched the build: the orchestrator's own
 * `compile_check.json` from right before the fixer started, the fixer's own permitted compile_check.py run, and
 * `fix_log.md` -- exactly what `fixer.agent.md` tells it to write every iteration. */
const HOUSEKEEPING_ONLY: Record<string, (wfDir: string) => Promise<void>> = {
  "the orchestrator's own compile_check.json, written right before the fixer starts": async (wfDir) => {
    await writeJson(path.join(wfDir, "compile_check.json"), { ok: false, errors: ["stale"], lint: {} });
  },
  "the fixer's own permitted compile_check.py run": async (wfDir) => {
    await writeJson(path.join(wfDir, "compile_check.json"), { ok: true, errors: [], lint: {} });
  },
  "only fix_log.md, as fixer.agent.md instructs every iteration": async (wfDir) => {
    await writeFile(path.join(wfDir, "fix_log.md"), "## iteration 1\n- symptom: see validation.json\n- fix: investigating\n", "utf8");
  },
};

for (const [shape, writeHousekeeping] of Object.entries(HOUSEKEEPING_ONLY)) {
  test(`IMP-2: a fixer that timed out after writing only ${shape} is not kept`, async () => {
    const { env, calls, files } = await makeEnv({ wf: "wf_0001", scenario: "fix-loop" });
    const recording = env.runner;
    const wfDir = files.path("workflows", "wf_0001");
    let fixerAttempts = 0;
    env.runner = {
      async run(role, wf, task, ctx) {
        if (role === "fixer") {
          fixerAttempts += 1;
          if (fixerAttempts === 1) {
            // This attempt never reaches the real (mock) runner -- exactly like an SDK session that
            // timed out having written only housekeeping -- so `calls.tasks`/`roles`/`order` are
            // recorded here, the same bookkeeping `recording.run` itself would have done.
            calls.tasks.push({ role, iteration: ctx?.iteration, task });
            calls.roles.push(role);
            calls.order.push(`agent:${role}`);
            await writeHousekeeping(wfDir);
            return { ok: false, error: "timeout", detail: "Timeout after 2700000ms waiting for session.idle", toolCalls: 0, ms: 0 };
          }
        }
        return recording.run(role, wf, task, ctx);
      },
    };
    const m = await migrateWorkflow(env, "wf_0001", {});
    assert.equal(m.status.translate, "VALIDATED", "the real fixer attempt (the retry) still fixes the build");
    const fixerCalls = calls.tasks.filter((t) => t.role === "fixer");
    assert.equal(fixerCalls.length, 2, "not kept: RETRY_ONCE ran -- housekeeping alone is not a change to the build");
    assert.equal(fixerCalls[1].task, fixerCalls[0].task, "a timeout's retry carries no feedback block");
    assert.equal(m.metrics.fixer?.timeouts, undefined, "never kept, so never counted");
  });
}

test("L7 R-b: a fixer that timed out without writing is not kept -- it retries as before R2", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "fix-loop" });
  const recording = env.runner;
  let fixerAttempts = 0;
  env.runner = {
    async run(role, wf, task, ctx) {
      if (role === "fixer") {
        fixerAttempts += 1;
        if (fixerAttempts === 1) {
          // Nothing touches build.sql, so the content snapshot taken at this attempt's own start is
          // unchanged; no clock or backdating is involved at all (fix round 2, IMP-2).
          calls.tasks.push({ role, iteration: ctx?.iteration, task });
          calls.roles.push(role);
          calls.order.push(`agent:${role}`);
          return { ok: false, error: "timeout", detail: "Timeout after 2700000ms waiting for session.idle", toolCalls: 0, ms: 0 };
        }
      }
      return recording.run(role, wf, task, ctx);
    },
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED", "the real fixer attempt (the retry) still fixes it");
  const fixerCalls = calls.tasks.filter((t) => t.role === "fixer");
  assert.equal(fixerCalls.length, 2, "not kept: RETRY_ONCE ran, same as before R-b");
  assert.equal(fixerCalls[1].task, fixerCalls[0].task, "a timeout's retry carries no feedback block");
  assert.equal(m.metrics.fixer?.timeouts, undefined, "never kept, so never counted");
});

test("L7 R-b: a fixer for a Python workflow is judged on build.py, and kept when it repaired it", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001", language: "python", scenario: "fix-loop" });
  const recording = env.runner;
  let timedOutOnce = false;
  env.runner = {
    async run(role, wf, task, ctx) {
      const result = await recording.run(role, wf, task, ctx);
      if (role === "fixer" && !timedOutOnce) {
        timedOutOnce = true;
        return { ok: false, error: "timeout", detail: "Timeout after 2700000ms waiting for session.idle", toolCalls: result.toolCalls, ms: result.ms };
      }
      return result;
    },
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED");
  assert.equal(await files.exists("workflows", "wf_0001", "build.py"), true);
  assert.equal(await files.exists("workflows", "wf_0001", "build.sql"), false);
  assert.equal(calls.tasks.filter((t) => t.role === "fixer").length, 1, "kept, not retried");
  assert.equal(m.metrics.fixer?.timeouts, 1);
});
