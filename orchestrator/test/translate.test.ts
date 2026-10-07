// The translate stage (objective-first design §3, §4.4-§4.9): ONE loop over the workflow, at most
// `maxFixIterations` rounds of translator (iteration 0) or fixer -> compile_check.py -> reviewer ->
// validate_build.py (a script the orchestrator runs, never an agent) -> render_mapping.py on a PASS, then the dbt
// export (export_dbt.py, validate_dbt.py: design D9). Also the document and pr stages that follow it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { migrateWorkflow, SESSION_RULES } from "../stages.ts";
import { writeJson } from "../manifest.ts";
import { makeEnv } from "./fakes.ts";

/** The calls translate makes, in order: scripts as `script args`, agents as `agent:<role>`. */
const loop = (calls: { order: string[]; py: { script: string; args: string[] }[] }, from = "agent:translator"): string[] => {
  const at = calls.order.indexOf(from);
  return calls.order.slice(at).filter((entry) => /^agent:(translator|fixer|reviewer)$|compile_check|validate_build|render_mapping/.test(entry))
    .map((entry) => entry.replace("py:scripts/", ""));
};

test("one pass: translator, compile check, reviewer, validate_build.py, render_mapping.py, then VALIDATED", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001" });
  const m = await migrateWorkflow(env, "wf_0001", { stopAfter: "translate" });
  assert.equal(m.status.translate, "VALIDATED");
  assert.deepEqual(loop(calls), ["agent:translator", "compile_check.py", "agent:reviewer", "validate_build.py", "render_mapping.py"]);
  assert.deepEqual(
    calls.py.filter((c) => /compile_check|validate_build|render_mapping/.test(c.script)).map((c) => [c.script, c.args] as const),
    [
      ["scripts/compile_check.py", ["wf_0001"]],
      ["scripts/validate_build.py", ["wf_0001"]],
      ["scripts/render_mapping.py", ["wf_0001"]],
    ],
    "one call each, on the workflow: no segment, no --target",
  );
  assert.ok(!calls.roles.includes("validator" as never), "there is no validator agent");
  assert.equal(await files.exists("workflows", "wf_0001", "docs", "mapping.md"), true, "the map is written on a PASS");
  assert.equal(await files.exists("workflows", "wf_0001", "build.sql"), true);
});

/** The dbt scripts the orchestrator ran, as `[script, args]`. */
const dbtCalls = (calls: { py: { script: string; args: string[] }[] }) =>
  calls.py.filter((c) => /export_dbt|validate_dbt/.test(c.script)).map((c) => [c.script, c.args] as const);

test("a PASS renders the map, then exports the dbt project and validates it, each once, then VALIDATED", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001" });
  const m = await migrateWorkflow(env, "wf_0001", { stopAfter: "translate" });
  assert.equal(m.status.translate, "VALIDATED");
  assert.deepEqual(dbtCalls(calls), [["scripts/export_dbt.py", ["wf_0001"]], ["scripts/validate_dbt.py", ["wf_0001"]]]);
  const at = (script: string) => calls.order.indexOf(`py:scripts/${script}`);
  assert.ok(at("validate_build.py") < at("render_mapping.py") && at("render_mapping.py") < at("export_dbt.py") && at("export_dbt.py") < at("validate_dbt.py"));
  assert.equal(await files.exists("workflows", "wf_0001", "dbt", "dbt_project.yml"), true);
  assert.equal(await files.exists("workflows", "wf_0001", "dbt", "validation.json"), true);
  assert.equal(m.reasons?.translate, undefined);
});

test("the dbt scripts run once, after the build validated: not on every fixer round, not before a PASS", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "fix-loop" });
  const m = await migrateWorkflow(env, "wf_0001", { stopAfter: "translate" });
  assert.equal(m.status.translate, "VALIDATED");
  assert.ok(calls.roles.includes("fixer"), "a validation FAIL went round again");
  assert.equal(dbtCalls(calls).length, 2);

  const never = await makeEnv({ wf: "wf_0001", scenario: "never-fixed" });
  const n = await migrateWorkflow(never.env, "wf_0001", { stopAfter: "translate" });
  assert.equal(n.status.translate, "NEEDS_HUMAN");
  assert.deepEqual(dbtCalls(never.calls), [], "a build that never validated has no dbt project");

  const unmapped = await makeEnv({ wf: "wf_0001", scenario: "mapping-fails" });
  await migrateWorkflow(unmapped.env, "wf_0001", { stopAfter: "translate" });
  assert.deepEqual(dbtCalls(unmapped.calls), [], "the export starts from a rendered map");
});

test("a Python workflow still runs both scripts -- they decide: nothing is written and translate is VALIDATED", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001", language: "python" });
  const m = await migrateWorkflow(env, "wf_0001", { stopAfter: "translate" });
  assert.equal(m.status.translate, "VALIDATED");
  assert.deepEqual(dbtCalls(calls).map(([script]) => script), ["scripts/export_dbt.py", "scripts/validate_dbt.py"]);
  assert.equal(await files.exists("workflows", "wf_0001", "dbt"), false);
});

test("export_dbt.py exiting 1 parks translate as `dbt: <its first stderr line>`; the project is never validated", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001", scenario: "dbt-export-fails" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "dbt: build.sql line 14: no dbt form for `CALL cleanup()`, which sqlglot's Snowflake parser does not understand");
  assert.deepEqual(dbtCalls(calls).map(([script]) => script), ["scripts/export_dbt.py"]);
  assert.equal(calls.roles.filter((r) => r === "fixer").length, 0, "the build validated: nothing for a fixer to repair");
  assert.ok(!calls.roles.includes("documenter") && m.status.document === undefined, "document never runs");
  assert.equal(await files.exists("workflows", "wf_0001", "docs", "mapping.md"), true, "the map was rendered before");
  assert.ok(calls.logs.some((line) => line.includes("export_dbt.py") && line.includes("no dbt form for") && line.includes("second line")));
});

test("validate_dbt.py exiting 1 parks translate as `dbt: <its first stderr line>`", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "dbt-validate-fails" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "dbt: normal: dbt build exited 1 on golden set normal: failed models: sales_summary");
  assert.deepEqual(dbtCalls(calls).map(([script]) => script), ["scripts/export_dbt.py", "scripts/validate_dbt.py"]);
  assert.equal(calls.roles.filter((r) => r === "fixer").length, 0);
  assert.ok(!calls.roles.includes("documenter"));
});

test("a dbt script that exits 2 is a script-error, not a verdict on the project", async () => {
  for (const scenario of ["dbt-export-crashes", "dbt-validate-crashes"]) {
    const { env, calls } = await makeEnv({ wf: "wf_0001", scenario });
    const m = await migrateWorkflow(env, "wf_0001", {});
    assert.equal(m.status.translate, "NEEDS_HUMAN", scenario);
    assert.equal(m.reasons?.translate, "script-error", scenario);
    assert.equal(calls.roles.filter((r) => r === "fixer").length, 0, scenario);
    assert.ok(calls.logs.some((line) => /(export|validate)_dbt\.py exited 2 — Traceback/.test(line)), scenario);
  }
});

test("a dbt reason is redacted and bounded like every recorded reason, and a silent exit 1 still says which script", async () => {
  const secret = await makeEnv({ wf: "wf_0001" });
  const py = secret.env.py;
  secret.env.py = async (script, args, opts) =>
    script.endsWith("validate_dbt.py")
      ? { ok: false, code: 1, out: "", err: `  normal: could not log in with password=hunter2 ${"x".repeat(600)}\n` }
      : py(script, args, opts);
  const m = await migrateWorkflow(secret.env, "wf_0001", {});
  const reason = m.reasons?.translate ?? "";
  assert.match(reason, /^dbt: normal: could not log in with <redacted> x+$/);
  assert.ok(!reason.includes("hunter2") && reason.length < 560, `bounded (${reason.length} chars)`);

  const silent = await makeEnv({ wf: "wf_0001" });
  const py2 = silent.env.py;
  silent.env.py = async (script, args, opts) =>
    script.endsWith("export_dbt.py") ? { ok: false, code: 1, out: "", err: "" } : py2(script, args, opts);
  const n = await migrateWorkflow(silent.env, "wf_0001", {});
  assert.equal(n.reasons?.translate, "dbt: export_dbt.py exited 1 and named no problem");
});

test("a translate parked at `dbt: ...` stays parked: a plain re-run does nothing, --from-stage translate starts it again", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "dbt-export-fails" });
  await migrateWorkflow(env, "wf_0001", {});
  const rolesAfterFirst = calls.roles.length;
  await migrateWorkflow(env, "wf_0001", {});
  assert.equal(calls.roles.length, rolesAfterFirst, "parked: a plain re-run does nothing");
  assert.ok(calls.logs.some((line) => line.includes("translate is parked at NEEDS_HUMAN (dbt: build.sql line 14")));
  await migrateWorkflow(env, "wf_0001", { fromStage: "translate" });
  assert.ok(calls.roles.length > rolesAfterFirst, "--from-stage translate runs the stage again");
});

test("the translator's task: the build file from the objective, and one self-test run of each script", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001" });
  await migrateWorkflow(env, "wf_0001", { stopAfter: "translate" });
  const { task } = calls.tasks.find((t) => t.role === "translator")!;
  assert.match(task, /^Translate wf_0001: write workflows\/wf_0001\/build\.sql from workflows\/wf_0001\/objective\.json and objective\.md/);
  assert.match(task, /workflows\/wf_0001\/digest\.md/);
  assert.match(task, /workflows\/wf_0001\/translation_notes\.md/);
  // R10: the compile check as often as needed until it exits 0, the validation once.
  assert.ok(task.includes("run python scripts/compile_check.py wf_0001 as often as you need until it exits 0; run python scripts/validate_build.py wf_0001 once."));
  assert.ok(!/once each/.test(task));
  assert.ok(task.includes(SESSION_RULES));
  assert.ok(!/segment|contract|proc\.sql|skeleton|TODO\(scaffold\)/i.test(task.replace(SESSION_RULES, "")));
});

test("a Python objective is translated into build.py, and a stale build.sql is removed first", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001", language: "python" });
  await migrateWorkflow(env, "wf_0001", { stopAfter: "golden" });
  await writeFile(files.path("workflows", "wf_0001", "build.sql"), "-- left over from an older objective\n", "utf8");
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED");
  assert.match(calls.tasks.find((t) => t.role === "translator")!.task, /write workflows\/wf_0001\/build\.py from/);
  assert.match(calls.tasks.find((t) => t.role === "reviewer")!.task, /Review workflows\/wf_0001\/build\.py against/);
  assert.equal(await files.exists("workflows", "wf_0001", "build.py"), true);
  assert.equal(await files.exists("workflows", "wf_0001", "build.sql"), false);
  assert.ok(calls.logs.some((line) => line.includes("removed build.sql: the objective's language is python")));
  const documenter = calls.tasks.find((t) => t.role === "documenter")!.task;
  assert.match(documenter, /build\.py/);
  assert.doesNotMatch(documenter, /dbt/, "a Python workflow has no dbt project to name");
});

test("a translator that wrote the wrong language's file is told which file is expected", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001" });
  const recording = env.runner;
  let wroteWrongFile = false;
  env.runner = {
    async run(role, wf, task, ctx) {
      if (role === "translator" && !wroteWrongFile) {
        wroteWrongFile = true;
        calls.tasks.push({ role, iteration: ctx?.iteration, task });
        calls.roles.push(role);
        calls.order.push(`agent:${role}`);
        await writeFile(files.path("workflows", "wf_0001", "build.py"), "# the wrong file\n", "utf8");
        return { ok: true, toolCalls: 0, ms: 0 };
      }
      return recording.run(role, wf, task, ctx);
    },
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED", "the one retry fixes it");
  const retry = calls.tasks.filter((t) => t.role === "translator")[1].task;
  assert.match(retry, /workflows\/wf_0001\/build\.sql was not written \(the objective's language is sql/);
});

test("a translator or fixer that leaves the other language's build file behind has it removed before compile_check.py", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001", scenario: "compile-fails-once" });
  const recording = env.runner;
  env.runner = {
    async run(role, wf, task, ctx) {
      const result = await recording.run(role, wf, task, ctx);
      // the session writes its own file and a stray one of the other language: compile_check.py would exit 2
      if (role === "translator" || role === "fixer") await writeFile(files.path("workflows", "wf_0001", "build.py"), "# stray\n", "utf8");
      return result;
    },
  };
  const py = env.py;
  const atCompile: boolean[] = [];
  env.py = async (script, args, opts) => {
    if (script.endsWith("compile_check.py")) atCompile.push(await files.exists("workflows", "wf_0001", "build.py"));
    return py(script, args, opts);
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED");
  assert.deepEqual(calls.roles.filter((r) => r === "translator" || r === "fixer"), ["translator", "fixer"]);
  assert.deepEqual(atCompile, [false, false], "no build.py at either compile check");
  assert.equal(await files.exists("workflows", "wf_0001", "build.py"), false);
  assert.equal(await files.exists("workflows", "wf_0001", "build.sql"), true);
  assert.equal(calls.logs.filter((line) => line.includes("removed build.py: the objective's language is sql")).length, 2);
});

// ---------- the loop ----------

test("a compile failure ends the iteration before review, and the fixer is told what compile_check.py said", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "compile-fails-once" });
  const m = await migrateWorkflow(env, "wf_0001", { stopAfter: "translate" });
  assert.equal(m.status.translate, "VALIDATED");
  assert.deepEqual(loop(calls), [
    "agent:translator", "compile_check.py",
    "agent:fixer", "compile_check.py", "agent:reviewer", "validate_build.py", "render_mapping.py",
  ]);
  const fixer = calls.tasks.find((t) => t.role === "fixer")!;
  assert.equal(fixer.iteration, 1);
  assert.match(fixer.task, /^Repair workflows\/wf_0001\/build\.sql: read validation\.json, review\.json and compile_check\.json first and change only what their diagnosis points at\./);
  assert.match(fixer.task, /failed before review/);
  assert.match(fixer.task, /scripts\/compile_check\.py/);
  assert.match(fixer.task, /workflows\/wf_0001\/compile_check\.json/);
  assert.match(fixer.task, /compile error near line 6/);
  assert.match(fixer.task, /workflows\/wf_0001\/notes\/fixer\.md/);
  // R10: the fixer is held to the same self-test allowance as the translator.
  assert.ok(fixer.task.includes("run python scripts/compile_check.py wf_0001 as often as you need until it exits 0; run python scripts/validate_build.py wf_0001 once."));
});

test("a compile failure that never clears parks translate: three compile checks, no reviewer, no validation", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "compile-fails" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "compile check failed after 3 iterations");
  assert.equal(calls.py.filter((c) => c.script.endsWith("compile_check.py")).length, 3);
  assert.ok(!calls.roles.includes("reviewer"));
  assert.ok(!calls.py.some((c) => /validate_build|render_mapping/.test(c.script)));
  assert.equal(calls.roles.filter((r) => r === "fixer").length, 2);
  assert.ok(!calls.roles.includes("documenter"));
});

test("compile_check.py exiting 2 is a script error, not a domain verdict: the loop stops at once", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "compile-crashes" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "script-error");
  assert.equal(calls.roles.filter((r) => r === "fixer").length, 0);
  assert.ok(calls.logs.some((line) => line.includes("compile_check.py exited 2")));
});

test("a reviewer BLOCK sends the build back to the fixer without validating it", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "review-blocks-once" });
  const m = await migrateWorkflow(env, "wf_0001", { stopAfter: "translate" });
  assert.equal(m.status.translate, "VALIDATED");
  assert.deepEqual(loop(calls), [
    "agent:translator", "compile_check.py", "agent:reviewer",
    "agent:fixer", "compile_check.py", "agent:reviewer", "validate_build.py", "render_mapping.py",
  ]);
  const fixer = calls.tasks.find((t) => t.role === "fixer")!;
  assert.doesNotMatch(fixer.task, /failed before review/, "review.json and validation.json exist: no pre-review diagnosis");
  assert.match(fixer.task, /review\.json/);
  assert.ok(calls.logs.some((line) => line.includes("review BLOCK on iteration 0")));
});

test("a reviewer that blocks every time parks translate and validate_build.py never runs", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "review-blocks" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "reviewer BLOCK after 3 iterations");
  assert.ok(!calls.py.some((c) => /validate_build|render_mapping/.test(c.script)));
});

test("a validation FAIL goes to the fixer, which reads validation.json, review.json and compile_check.json first", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001", scenario: "fix-loop" });
  const m = await migrateWorkflow(env, "wf_0001", { stopAfter: "translate" });
  assert.equal(m.status.translate, "VALIDATED");
  assert.deepEqual(loop(calls), [
    "agent:translator", "compile_check.py", "agent:reviewer", "validate_build.py",
    "agent:fixer", "compile_check.py", "agent:reviewer", "validate_build.py", "render_mapping.py",
  ]);
  const fixer = calls.tasks.find((t) => t.role === "fixer")!;
  assert.match(fixer.task, /read validation\.json, review\.json and compile_check\.json first/);
  assert.doesNotMatch(fixer.task, /failed before review/, "validation.json and review.json do exist in this case");
  assert.match(await files.read("workflows", "wf_0001", "fix_log.md") ?? "", /## iteration 1/);
  assert.doesNotMatch(await files.read("workflows", "wf_0001", "build.sql") ?? "", /BROKEN BUILD/, "the fixer's build replaced the broken one");
  assert.equal(JSON.parse((await files.read("workflows", "wf_0001", "validation.json")) ?? "{}").verdict, "PASS");
});

test("a validation FAIL that never clears parks translate after maxFixIterations rounds", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "never-fixed" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "validation FAIL after 3 iterations");
  assert.equal(calls.py.filter((c) => c.script.endsWith("validate_build.py")).length, 3);
  assert.ok(!calls.py.some((c) => c.script.endsWith("render_mapping.py")), "no map for a build that never validated");

  const one = await makeEnv({ wf: "wf_0001", scenario: "never-fixed", config: { maxFixIterations: 1 } });
  const n = await migrateWorkflow(one.env, "wf_0001", {});
  assert.equal(n.reasons?.translate, "validation FAIL after 1 iterations");
  assert.equal(one.calls.roles.filter((r) => r === "fixer").length, 0);
});

test("needs_human parks at once: no fixer, no map", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "needs-human" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "needs_human");
  assert.equal(calls.roles.filter((r) => r === "fixer").length, 0);
  assert.equal(calls.py.filter((c) => c.script.endsWith("validate_build.py")).length, 1);
  assert.ok(!calls.py.some((c) => c.script.endsWith("render_mapping.py")));
});

// task-15-int brief, coordinator ruling 4: a PASS_WITH_ACCEPTED_DIFF report that also says needs_human: true
// must not be read as validated just because its verdict starts with "PASS".
test("needs_human wins even when the verdict itself starts with PASS", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "needs-human-pass" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "needs_human");
  assert.equal(calls.roles.filter((r) => r === "fixer").length, 0);
  assert.ok(!calls.py.some((c) => c.script.endsWith("render_mapping.py")));
});

test("validate_build.py exiting 2, or exiting without a report, is a script error", async () => {
  for (const scenario of ["validate-crashes", "validate-no-report"]) {
    const { env, calls } = await makeEnv({ wf: "wf_0001", scenario });
    const m = await migrateWorkflow(env, "wf_0001", {});
    assert.equal(m.status.translate, "NEEDS_HUMAN", scenario);
    assert.equal(m.reasons?.translate, "script-error", scenario);
    assert.equal(calls.roles.filter((r) => r === "fixer").length, 0, `${scenario}: no fixer for a script that did not judge the build`);
    assert.ok(!calls.py.some((c) => c.script.endsWith("render_mapping.py")), scenario);
  }
});

test("a PASS renders the map; a failing render_mapping.py parks translate as `mapping` and document never runs", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001", scenario: "mapping-fails" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "mapping");
  assert.equal(calls.py.filter((c) => c.script.endsWith("render_mapping.py")).length, 1);
  assert.equal(calls.roles.filter((r) => r === "fixer").length, 0, "the build validated: nothing for a fixer to repair");
  assert.ok(!calls.roles.includes("documenter"));
  assert.equal(await files.exists("workflows", "wf_0001", "docs", "mapping.md"), false);
  assert.ok(calls.logs.some((line) => line.includes("render_mapping.py exited 1") && line.includes("step s1 has no region")));

  // exit 2 is not a domain verdict either, but the reason is the same: the map could not be written
  const crash = await makeEnv({ wf: "wf_0001" });
  const py = crash.env.py;
  crash.env.py = async (script, args, opts) =>
    script.endsWith("render_mapping.py") ? { ok: false, code: 2, out: "", err: "no objective.json" } : py(script, args, opts);
  const n = await migrateWorkflow(crash.env, "wf_0001", {});
  assert.equal(n.reasons?.translate, "mapping");
});

test("a translate parked at `mapping` stays parked: a plain re-run does nothing", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", scenario: "mapping-fails" });
  await migrateWorkflow(env, "wf_0001", {});
  const rolesAfterFirst = calls.roles.length;
  await migrateWorkflow(env, "wf_0001", {});
  assert.equal(calls.roles.length, rolesAfterFirst, "parked: a plain re-run does nothing");
});

// ---------- agent failures inside the loop ----------

test("an agent failure parks translate with `<role> <error>`", async () => {
  const translator = await makeEnv({ wf: "wf_0001", agentErrors: { translator: ["missing-output", "missing-output"] } });
  const a = await migrateWorkflow(translator.env, "wf_0001", {});
  assert.equal(a.reasons?.translate, "translator missing-output");

  const reviewer = await makeEnv({ wf: "wf_0001", agentErrors: { reviewer: ["denied"] } });
  const b = await migrateWorkflow(reviewer.env, "wf_0001", {});
  assert.equal(b.reasons?.translate, "reviewer denied");
  assert.ok(!reviewer.calls.py.some((c) => c.script.endsWith("validate_build.py")));

  const fixer = await makeEnv({ wf: "wf_0001", scenario: "fix-loop", agentErrors: { fixer: ["error"] } });
  const c = await migrateWorkflow(fixer.env, "wf_0001", {});
  assert.equal(c.reasons?.translate, "fixer error");
});

test("a tool-call budget hit mid-translate records 'budget', not a role name", async () => {
  const { env } = await makeEnv({ wf: "wf_0001" });
  await migrateWorkflow(env, "wf_0001", { stopAfter: "golden" });
  const { readManifest } = await import("./fakes.ts");
  const onDisk = await readManifest(env.root, "wf_0001");
  onDisk.metrics.translator = { toolCalls: 401 };
  await writeJson(path.join(env.root, "workflows", "wf_0001", "manifest.json"), onDisk);
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "budget");
});

// ---------- diagnosis quoted to the fixer ----------

test("a diagnosis quoted to the fixer is redacted and bounded exactly like an audit line", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001" });
  const py = env.py;
  env.py = async (script, args, opts) =>
    script.endsWith("compile_check.py")
      ? { ok: false, code: 1, out: "", err: `compile failed: api_key=sk-live-123456 ${"x".repeat(2000)}` }
      : py(script, args, opts);
  await migrateWorkflow(env, "wf_0001", {});
  const fixer = calls.tasks.find((t) => t.role === "fixer");
  assert.ok(fixer, "the fixer ran");
  assert.doesNotMatch(fixer!.task, /sk-live-123456/, "a secret in a script's stderr must not reach a prompt");
  assert.match(fixer!.task, /<redacted>/);
  // Task L1 (R4): the fixed session-rules paragraph is not part of what this bound measures.
  const bounded = fixer!.task.replace(SESSION_RULES, "");
  assert.ok(bounded.length < 1400, `the quoted diagnosis is bounded, task was ${bounded.length} chars without the session rules`);
});

// ---------- live hardening, Task L4 (R2): a translator's or fixer's own validation is never the verdict ----------
// The translator and the fixer may run validate_build.py on their own work, which writes the same validation*.json the
// orchestrator's own run is judged by. The orchestrator deletes those reports before it runs the script, so a verdict
// read afterwards is only ever the one that run wrote.

const STALE_PASS = { verdict: "PASS", needs_human: false, diff_clusters: [], idempotent: true, sets: { normal: "PASS" } };

test("L4: a translator's own validation.json never stands in for the orchestrator's run", async () => {
  const { env, root } = await makeEnv({ wf: "wf_0001", scenario: "validate-no-report" });
  const recording = env.runner;
  const wfRoot = path.join(root, "workflows", "wf_0001");
  env.runner = {
    async run(role, wf, task, ctx) {
      const result = await recording.run(role, wf, task, ctx);
      if (role === "translator") {
        // what `python scripts/validate_build.py wf_0001 --set normal` leaves behind
        await writeJson(path.join(wfRoot, "validation.json"), STALE_PASS);
        await writeJson(path.join(wfRoot, "validation.normal.json"), STALE_PASS);
      }
      return result;
    },
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN", "the stale PASS was cleared, so the script that wrote no report is a script error");
  assert.equal(m.reasons?.translate, "script-error");
});

test("L4: with the real run reporting, a translator's own report is replaced and the build still passes", async () => {
  const { env, root, calls } = await makeEnv({ wf: "wf_0001", scenario: "fix-loop" });
  const recording = env.runner;
  const wfRoot = path.join(root, "workflows", "wf_0001");
  env.runner = {
    async run(role, wf, task, ctx) {
      const result = await recording.run(role, wf, task, ctx);
      // the broken translator build, with a (stale) PASS its own self-test left behind
      if (role === "translator") await writeJson(path.join(wfRoot, "validation.json"), STALE_PASS);
      return result;
    },
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "VALIDATED");
  // the stale PASS did not end the loop at iteration 0: the broken build was validated for real, FAILed, and fixed
  assert.equal(calls.roles.filter((r) => r === "fixer").length, 1);
});

test("a stale review.json never satisfies the reviewer's check", async () => {
  const { env, root, calls } = await makeEnv({ wf: "wf_0001" });
  await mkdir(path.join(root, "workflows", "wf_0001"), { recursive: true });
  await writeJson(path.join(root, "workflows", "wf_0001", "review.json"), { verdict: "PASS", findings: [] });
  const recording = env.runner;
  env.runner = {
    async run(role, wf, task, ctx) {
      if (role === "reviewer") {
        calls.tasks.push({ role, iteration: ctx?.iteration, task });
        calls.roles.push(role);
        calls.order.push(`agent:${role}`);
        return { ok: true, toolCalls: 0, ms: 0 };   // a session that ends ok and writes nothing
      }
      return recording.run(role, wf, task, ctx);
    },
  };
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.translate, "NEEDS_HUMAN");
  assert.equal(m.reasons?.translate, "reviewer missing-output");
  assert.ok(!calls.py.some((c) => c.script.endsWith("validate_build.py")));
});

// ---------- document and pr ----------

test("the documenter writes migration.md from the objective, the notes, the validation report and a LINKED map", async () => {
  const { env, calls, files } = await makeEnv({ wf: "wf_0001" });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.document, "DONE");
  const { task } = calls.tasks.find((t) => t.role === "documenter")!;
  assert.match(task, /write workflows\/wf_0001\/docs\/migration\.md from workflows\/wf_0001\/objective\.md/);
  assert.match(task, /workflows\/wf_0001\/translation_notes\.md/);
  assert.match(task, /workflows\/wf_0001\/validation\.json/);
  assert.match(task, /Link workflows\/wf_0001\/docs\/mapping\.md/);
  assert.match(task, /never rewrite it/);
  assert.ok(!/segment|master\.sql|procs\/|proc\.sql|output_kind/i.test(task.replace(SESSION_RULES, "")), "nothing of the old flow");
  // a SQL workflow's dbt project -- made by a script, never by the documenter -- is named beside the build file
  assert.match(task, /validation reports \(workflows\/wf_0001\/validation\.json and workflows\/wf_0001\/dbt\/validation\.json\)/);
  assert.match(task, /Deployment section names workflows\/wf_0001\/build\.sql and scripts\/deploy\.py, and the dbt project workflows\/wf_0001\/dbt \(converted from the build file by a script and checked against the same golden data\) with scripts\/deploy\.py wf_0001 --dbt --database <DB> --schema <SCHEMA> -- nothing is deployed/);
  // the map is the script's: the documenter's canned migration.md links it, and the map itself is untouched
  assert.match((await files.read("workflows", "wf_0001", "docs", "migration.md")) ?? "", /\[the step map\]\(mapping\.md\)/);
  assert.match((await files.read("workflows", "wf_0001", "docs", "mapping.md")) ?? "", /how the Alteryx workflow maps/);
});

test("a missing migration.md is retried once, then parks document", async () => {
  const { env, calls } = await makeEnv({ wf: "wf_0001", agentErrors: { documenter: ["missing-output", "missing-output"] } });
  const m = await migrateWorkflow(env, "wf_0001", {});
  assert.equal(m.status.document, "NEEDS_HUMAN");
  assert.equal(calls.roles.filter((r) => r === "documenter").length, 2);
});
