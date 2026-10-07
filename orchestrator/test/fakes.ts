// Self-contained fixtures for the stage and CLI tests.
//
// Two things are faked and nothing else:
//   * `py` performs each Python script's FILE EFFECTS itself, so these tests need no
//     interpreter, no DuckDB and no samples committed by another task. Exit codes follow
//     the scripts' documented contract (0 success, 1 domain failure, 2 usage/unexpected) as the
//     objective-first plan states it (docs/superpowers/plans/2026-10-06-objective-first.md, A2-A5).
//   * the agent runner records which role ran and can be told to fail with a given
//     AgentError; everything else is the real MockRunner copying real files.
//
// The canned trees MockRunner replays are the static fixtures under `fixtures/samples/`, in the
// layout the real samples have (`canned/{objective.json, objective.md, build.sql|build.py,
// translation_notes.md, review.json, intake/plan.md, docs/migration.md}` and `broken/`): wf_0001 is a
// SQL workflow that validates, wf_0006 a Python one, and wf_0005 a tier-T3 workflow whose canned
// parser-recovery writes a parser extension.
import { cp, mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DEFAULT_CONFIG } from "../cli.ts";
import { loadManifest, readJsonOr, wfDir, writeJson } from "../manifest.ts";
import { MockRunner } from "../runner.ts";
import { DIGEST_INLINE_CHARS } from "../stages.ts";
import type {
  AgentError,
  AgentResult,
  AgentRunner,
  Env,
  Manifest,
  OrchestratorConfig,
  Role,
  ShResult,
} from "../types.ts";

export { loadManifest as readManifest };

/** `orchestrator/test/fixtures/samples/`: the canned trees (and nothing else) a MockRunner test replays. */
export const FIXTURE_SAMPLES = fileURLToPath(new URL("./fixtures/samples/", import.meta.url));

/** The marker the broken fixture builds (fixtures/samples/<wf>/broken/) carry: the fake validate_build.py FAILs a build that holds it. */
export const BROKEN_BUILD = "BROKEN BUILD";

const temporaries: string[] = [];
let cleanupRegistered = false;

/** Temp roots are removed when the test process exits, whatever the outcome. */
function registerCleanup(): void {
  if (cleanupRegistered) return;
  cleanupRegistered = true;
  process.on("exit", () => {
    for (const dir of temporaries) {
      try {
        rmSync(dir, { recursive: true, force: true });
      } catch {
        /* a leftover temp dir is not worth failing a test run */
      }
    }
  });
}

export async function tempRoot(prefix = "orch-"): Promise<string> {
  registerCleanup();
  const dir = await mkdtemp(path.join(os.tmpdir(), prefix));
  temporaries.push(dir);
  return dir;
}

async function write(file: string, text: string): Promise<void> {
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, text, "utf8");
}

async function exists(file: string): Promise<boolean> {
  try {
    await stat(file);
    return true;
  } catch {
    return false;
  }
}

export interface FakeCalls {
  py: { script: string; args: string[]; inheritStdio: boolean }[];
  sh: { cmd: string; args: string[] }[];
  roles: Role[];
  /** Every task prompt an agent was given, so a test can assert what a role was told to do. */
  tasks: { role: Role; iteration?: number; task: string }[];
  /** `py:<script>` and `agent:<role>` interleaved in the order they actually happened, so a test
   * can assert a script ran BEFORE an agent (digest.py before the analyzer) rather than
   * only that both happened. */
  order: string[];
  sleeps: number[];
  logs: string[];
}

export interface FakeOptions {
  wf: string;
  /**
   * What the fake scripts and the canned tree act out (a name; anything after a `:` is ignored):
   * `fix-loop` / `never-fixed` / `recovery-fails` (MockRunner), `objective-questions` (the analyzer's
   * objective carries one open question, O1),
   * `compile-fails` / `compile-fails-once` / `compile-crashes`, `review-blocks-once` /
   * `review-blocks`, `needs-human` / `needs-human-pass` (the validation report), `validate-crashes` /
   * `validate-no-report`, `mapping-fails`, `dbt-export-fails` / `dbt-export-crashes` / `dbt-validate-fails` /
   * `dbt-validate-crashes` (the dbt export and its check, after the map), `objective-bad` (the checker refuses the objective once) /
   * `objective-bad-stuck` / `not-ready` (`objective_check.py --ready` refuses) / `apply-fails`,
   * `digest-large` / `digest-crashes`, `scaffold-crashes`, `objective-check-crashes`,
   * `questions-crashes`, `context-fails`.
   */
  scenario?: string;
  /** `python`: the canned workflow is the Python one (`objective.json` says `python`, the canned build is
   * `build.py`); absent, it is the SQL one. */
  language?: "python";
  /** What the fake intake_prompt.py records in manifest.status.intake. */
  intake?: string;
  /** Errors to inject, one per call of that role, in order. `null` means "let this call run for
   * real" -- what a test needs to make attempt 1 fail VERIFY and attempt 2 fail before it. */
  agentErrors?: Partial<Record<Role, (AgentError | null)[]>>;
  interactive?: boolean;
  hasGh?: boolean;
  /** GitHub integration on for this run; off by default, as in the orchestrator (fix round 1, B3). */
  ghEnabled?: boolean;
  /** Merged into the seeded manifest.json on disk. */
  manifest?: Partial<Manifest>;
  config?: Partial<OrchestratorConfig>;
  /** "empty" makes the simulator produce no golden sets and exit 1. */
  golden?: "ok" | "empty";
}

export interface Fake {
  root: string;
  env: Env;
  calls: FakeCalls;
  files: {
    path(...rest: string[]): string;
    read(...rest: string[]): Promise<string | undefined>;
    exists(...rest: string[]): Promise<boolean>;
  };
  /** Makes the next intake_prompt.py run report READY, as an owner answering the questions. */
  answerIntake(): void;
  /** Answers every open objective question (O1, …), as a person would in objective_questions.md. */
  answerObjective(): void;
  cleanup(): Promise<void>;
}

const scenarioName = (scenario?: string): string => (scenario ?? "").split(":")[0];

/** Which fixture tree a workflow replays: wf_0005 is the tier-T3 one, `language: "python"` the Python workflow. */
function fixtureOf(wf: string, language?: "python"): string {
  if (wf === "wf_0005") return "wf_0005";
  return language === "python" ? "wf_0006" : "wf_0001";
}

/** The canned artifacts a human would have written under samples/<wf>/, copied from the fixtures. */
async function seedSamples(root: string, wf: string, scenario?: string, language?: "python"): Promise<void> {
  const target = path.join(root, "samples", wf);
  await cp(path.join(FIXTURE_SAMPLES, fixtureOf(wf, language)), target, { recursive: true });
  if (scenarioName(scenario) === "objective-questions") {
    const file = path.join(target, "canned", "objective.json");
    const objective = JSON.parse(await readFile(file, "utf8")) as Record<string, unknown>;
    objective.questions = [
      { id: "O1", tools: ["2"], question: "Is excluding the WEST region deliberate?", assumption: "Deliberate." },
    ];
    await writeJson(file, objective);
  }
}

/** Seeds one more workflow (source + samples + a manifest.json) into a root a Fake already
 * owns — for tests that need more than one workflow directory, e.g. proving that a corrupt
 * manifest for one workflow does not stop another workflow in the same run (F12). */
export async function seedWorkflow(
  root: string,
  wf: string,
  manifest?: Partial<Manifest>,
  scenario?: string,
  language?: "python",
): Promise<void> {
  await seedSamples(root, wf, scenario, language);
  await write(path.join(root, "workflows", wf, "source", `${wf}.yxmd`), "<AlteryxDocument yxmdVer=\"2023.1\"/>\n");
  await writeJson(path.join(root, "workflows", wf, "manifest.json"), { id: wf, status: {}, metrics: {}, ...(manifest ?? {}) });
}

export async function makeEnv(options: FakeOptions): Promise<Fake> {
  const wf = options.wf;
  const root = await tempRoot();
  const calls: FakeCalls = { py: [], sh: [], roles: [], tasks: [], order: [], sleeps: [], logs: [] };
  const state = { intake: options.intake ?? "READY", objectiveAnswered: false };
  const scenario = scenarioName(options.scenario);

  await seedWorkflow(root, wf, options.manifest, options.scenario, options.language);

  const config: OrchestratorConfig = { ...DEFAULT_CONFIG, ...(options.config ?? {}) };
  const runner = new MockRunner(root, path.join(root, "samples"), options.scenario);

  const result = (code: number, out = "", err = ""): ShResult => ({ ok: code === 0, code, out, err });

  /** The workflow's build file as it stands, whichever language it is in. */
  const currentBuild = async (id: string): Promise<string> =>
    (await readFile(wfDir(root, id, "build.sql"), "utf8").catch(() => "")) +
    (await readFile(wfDir(root, id, "build.py"), "utf8").catch(() => ""));

  const readObjective = (id: string) => readJsonOr<Record<string, any>>(wfDir(root, id, "objective.json"), {});

  /** `digest.py <wf>`: `digest.md`, one line per tool in the real one; `digest-large` makes it larger than
   * the analyzer's inline allowance; `digest-crashes` exits 2. */
  const fakeDigest = async (id: string): Promise<ShResult> => {
    if (scenario === "digest-crashes") return result(2, "", "digest.py: error: parsed/dag.json is missing");
    const body = scenario === "digest-large" ? `T1 Input ${"x".repeat(DIGEST_INLINE_CHARS)}\n` : "T1 Input  ORDERS\nT2 Filter  [REGION] != \"WEST\"  ← T1\n";
    await write(wfDir(root, id, "digest.md"), `# ${id} digest (fake)\n${body}`);
    return result(0, wfDir(root, id, "digest.md"));
  };

  /** `objective_scaffold.py <wf> --prefill | --apply`: --prefill writes a skeleton (mechanical fields only,
   * judgment fields empty) where no objective exists; --apply changes nothing here (the canned objective is
   * the reference it would reproduce) but exits 1 on `apply-fails`; `scaffold-crashes` exits 2. */
  const fakeScaffold = async (id: string, args: string[]): Promise<ShResult> => {
    if (scenario === "scaffold-crashes") return result(2, "", "Traceback (most recent call last): objective_scaffold.py crashed");
    if (args.includes("--prefill")) {
      const file = wfDir(root, id, "objective.json");
      if (!(await exists(file))) {
        await writeJson(file, {
          workflow: id, title: "", purpose: "", tier: null, language: null, language_reason: null,
          inputs: [], outputs: [], steps: [], no_effect: [], rules: [], parity_risks: [], questions: [], answered: [],
        });
      }
      return result(0, `${id}: pre-filled the objective`, "note: outputs[0].keys: not derivable from intake/mappings.yaml");
    }
    if (args.includes("--apply")) {
      if (scenario === "apply-fails") {
        return result(1, "", "objective.json is not a JSON object (Expecting value: line 1 column 1 (char 0))");
      }
      return result(0, `${id}: re-applied the mechanical fields; rewrote none`);
    }
    return result(2, "", "usage: objective_scaffold.py <wf_id> (--prefill | --apply)");
  };

  /** `objective_check.py <wf> [--ready]` (stderr: one `<field>: <problem>; expected <what>` line per problem):
   * `objective-bad` refuses the objective the FIRST time it is checked (the retry then passes),
   * `objective-bad-stuck` every time; `--ready` refuses on `not-ready` (a `logical` is null) and while the
   * objective still lists a question; `objective-check-crashes` exits 2. */
  let objectiveChecks = 0;
  const fakeObjectiveCheck = async (id: string, args: string[]): Promise<ShResult> => {
    if (scenario === "objective-check-crashes") return result(2, "", "Traceback (most recent call last): objective_check.py crashed");
    if (args.includes("--ready")) {
      const problems: string[] = [];
      if (scenario === "not-ready") problems.push("outputs[0].logical: is null; expected a logical name from intake/mappings.yaml");
      const questions = (await readObjective(id)).questions ?? [];
      for (const question of questions) problems.push(`questions: ${question.id} is still open; expected none`);
      return problems.length > 0 ? result(1, `${id}: ${problems.length} problem(s)`, problems.join("\n")) : result(0, `${id}: ready`);
    }
    objectiveChecks += 1;
    if (scenario === "objective-bad-stuck" || (scenario === "objective-bad" && objectiveChecks === 1)) {
      return result(1, `${id}: 2 problems`,
        "steps: tool 12 is in no step and not in no_effect; expected every tool id of parsed/dag.json exactly once\n" +
          "outputs[0].keys: REGIONX is not a column; expected a subset of the output's column names");
    }
    return result(0, `${id}: the objective passes`);
  };

  /** `objective_questions.py <wf> --write | --status | --answers`: --write renders `intake/objective_questions.md`
   * from the objective's `questions` (an `Answer:` once answered) and removes it when there are none; --status
   * exits 1 while any question is open; --answers prints the resolved answers as JSON. `questions-crashes`
   * exits 2. */
  const fakeQuestions = async (id: string, args: string[]): Promise<ShResult> => {
    if (scenario === "questions-crashes") return result(2, "", "Traceback (most recent call last): objective_questions.py crashed");
    const questions: { id: string; question: string }[] = (await readObjective(id)).questions ?? [];
    const file = wfDir(root, id, "intake", "objective_questions.md");
    if (args.includes("--write")) {
      if (questions.length === 0) {
        await rm(file, { force: true });
      } else {
        const boxes = questions.map((q) => `- [${state.objectiveAnswered ? "x" : " "}] ${q.id} - ${q.question}\n      Answer: ${state.objectiveAnswered ? "yes" : ""}`);
        await write(file, `# Objective questions - ${id}\n${boxes.join("\n")}\n`);
      }
      return result(0, `${id}: ${questions.length} question(s)`);
    }
    if (args.includes("--status")) {
      const open = state.objectiveAnswered ? [] : questions.map((q) => q.id);
      return open.length > 0 ? result(1, `${id}: open ${open.join(", ")}`) : result(0, `${id}: every question is answered`);
    }
    if (args.includes("--answers")) {
      // the resolved answers as JSON: every question once a person has answered them (`answerObjective`), else none
      const given = Object.fromEntries(state.objectiveAnswered ? questions.map((q) => [q.id, "yes"]) : []);
      return result(0, JSON.stringify(given, null, 2));
    }
    return result(2, "", "usage: objective_questions.py <wf_id> (--write | --status | --answers)");
  };

  /** `compile_check.py <wf>`: `compile_check.json` written on exit 0 and 1; `compile-fails` exits 1 every
   * time, `compile-fails-once` on its first call, `compile-crashes` exits 2 (the real script writes nothing). */
  let compileCalls = 0;
  const fakeCompile = async (id: string): Promise<ShResult> => {
    compileCalls += 1;
    if (scenario === "compile-crashes") return result(2, "", "Traceback (most recent call last): compile_check.py crashed");
    const fails = scenario === "compile-fails" || (scenario === "compile-fails-once" && compileCalls === 1);
    const errors = fails ? ["statement:kind: build.sql:6 CALL is not allowed"] : [];
    await writeJson(wfDir(root, id, "compile_check.json"), { ok: !fails, errors, lint: { lines: 6, statements: 1, ctes: 0, flags: [] } });
    return fails ? result(1, "", "compile error near line 6") : result(0, "compile OK");
  };

  /** `validate_build.py <wf>`: `validation.json` and `validation.normal.json` written. The set FAILs while
   * the build is the broken fixture (it holds `BROKEN_BUILD`), or on `needs-human`; `needs-human-pass` is a PASS
   * that still says needs_human: true (task-15-int ruling 4); `validate-crashes` exits 2 writing nothing;
   * `validate-no-report` exits 1 writing nothing. */
  const fakeValidate = async (id: string): Promise<ShResult> => {
    if (scenario === "validate-crashes") return result(2, "", "Traceback (most recent call last): validate_build.py crashed");
    if (scenario === "validate-no-report") return result(1, "", "validate_build.py: no report was written");
    const needsHuman = scenario === "needs-human";
    const needsHumanPass = scenario === "needs-human-pass";
    const fail = needsHuman || (await currentBuild(id)).includes(BROKEN_BUILD);
    const report = {
      workflow: id,
      language: (await readObjective(id)).language ?? "sql",
      golden_set: "normal",
      verdict: needsHumanPass ? "PASS_WITH_ACCEPTED_DIFF" : fail ? "FAIL" : "PASS",
      checks: { schema: "PASS" },
      diff_clusters: fail || needsHumanPass ? [{ class: "NULL_SEMANTICS", columns: ["REGION"], count: 2 }] : [],
      idempotent: true,
      needs_human: needsHuman || needsHumanPass,
    };
    await writeJson(wfDir(root, id, "validation.json"), report);
    await writeJson(wfDir(root, id, "validation.normal.json"), report);
    return result(fail ? 1 : 0);
  };

  /** `render_mapping.py <wf>`: `docs/mapping.md`; `mapping-fails` exits 1 (a step has no region). */
  const fakeMapping = async (id: string): Promise<ShResult> => {
    if (scenario === "mapping-fails") return result(1, "", "step s1 has no region in build.sql; expected a `-- step s1` marker");
    await write(wfDir(root, id, "docs", "mapping.md"), `# ${id} — how the Alteryx workflow maps to the build file (fake)\n`);
    return result(0, wfDir(root, id, "docs", "mapping.md"));
  };

  /** `export_dbt.py <wf>`: a SQL workflow's `dbt/dbt_project.yml`; a Python workflow is skipped (exit 0, nothing
   * written, as the real script does); `dbt-export-fails` exits 1 naming a line, `dbt-export-crashes` exits 2. */
  const fakeDbtExport = async (id: string): Promise<ShResult> => {
    if (scenario === "dbt-export-crashes") return result(2, "", "Traceback (most recent call last): export_dbt.py crashed");
    if (scenario === "dbt-export-fails") {
      return result(1, "", "build.sql line 14: no dbt form for `CALL cleanup()`, which sqlglot's Snowflake parser does not understand\nsecond line");
    }
    if ((await readObjective(id)).language === "python") {
      return result(0, `${id}: dbt export skipped: the objective's language is python`);
    }
    await write(wfDir(root, id, "dbt", "dbt_project.yml"), `name: mig_${id}\n`);
    return result(0, `${id}: wrote workflows/${id}/dbt (1 models: sales_summary)`);
  };

  /** `validate_dbt.py <wf>`: `dbt/validation.json` for a SQL workflow (a Python one has nothing to validate: exit 0);
   * `dbt-validate-fails` exits 1 with the failure on stderr, `dbt-validate-crashes` exits 2. */
  const fakeDbtValidate = async (id: string): Promise<ShResult> => {
    if (scenario === "dbt-validate-crashes") return result(2, "", "Traceback (most recent call last): validate_dbt.py crashed");
    if (scenario === "dbt-validate-fails") {
      return result(1, `${id}: FAIL {'normal': 'FAIL'} (idempotent=True)`, "  normal: dbt build exited 1 on golden set normal: failed models: sales_summary\nsecond line");
    }
    if ((await readObjective(id)).language === "python") {
      return result(0, `${id}: dbt validation skipped: the objective's language is python`);
    }
    await writeJson(wfDir(root, id, "dbt", "validation.json"), { workflow: id, language: "sql", verdict: "PASS", sets: { normal: "PASS" } });
    return result(0, `${id}: PASS {'normal': 'PASS'} (idempotent=True)`);
  };

  const py = async (script: string, args: string[], opts?: { inheritStdio?: boolean }): Promise<ShResult> => {
    calls.py.push({ script, args, inheritStdio: Boolean(opts?.inheritStdio) });
    calls.order.push(`py:${script}`);
    const name = script.split("/").pop() ?? script;
    const id = args[0] ?? wf;
    const manifestFile = path.join(root, "workflows", id, "manifest.json");
    const patchManifest = async (patch: (m: Manifest) => void): Promise<void> => {
      const manifest = await loadManifest(root, id);
      patch(manifest);
      await writeJson(manifestFile, manifest);
    };

    switch (name) {
      case "parse.py": {
        // wf_0005 only parses once a recovery extension exists (the vendor plugin case).
        const recovered = await exists(path.join(root, "scripts", "parsers", "ext", "vendor_tool.py"));
        const ok = id !== "wf_0005" || recovered;
        await writeJson(wfDir(root, id, "parsed", "parse_report.json"), {
          status: ok ? (recovered ? "RECOVERED" : "PARSED") : "FAILED",
          errors: ok ? [] : ["unknown Plugin name: Vendor.CustomTool"],
          node_count: 4,
          extension: recovered ? "vendor_tool" : null,
        });
        return result(ok ? 0 : 1, "", ok ? "" : "parse failed: unknown Plugin name");
      }
      case "intake_touchpoints.py":
        await writeJson(wfDir(root, id, "intake", "touchpoints.json"), { inputs: [], outputs: [] });
        return result(0);
      case "intake_prompt.py": {
        const status = state.intake;
        await patchManifest((m) => {
          m.status.intake = status;
        });
        await write(
          wfDir(root, id, "intake", "open_questions.md"),
          status === "READY" ? "- [x] Tool 1 Input → SALES.RAW.ORDERS\n" : "- [ ] Tool 1 Input → ?\n",
        );
        return result(status === "READY" ? 0 : 1, "", status === "READY" ? "" : `intake ${status}`);
      }
      case "alteryx_sim.py": {
        const empty = options.golden === "empty";
        await patchManifest((m) => {
          m.golden_sets = empty ? [] : ["normal", "period_end", "empty", "edge"];
        });
        return result(empty ? 1 : 0, "", empty ? "no simulator support for this workflow" : "");
      }
      case "prompt_context.py": {
        // Task F: exit 2 on scenario `context-fails` (a broken renderer -- the stage must still
        // run its agent, just without the inline block); otherwise a fixed, recognisable stand-in
        // for the real Markdown, naming the role it was asked for.
        const role = args[1] === "--role" ? args[2] : "?";
        if (scenario === "context-fails") return result(2, "", "prompt_context: crashed");
        return result(0, `## Inline context for ${role} (fake)\n- tool 1 input`);
      }
      case "digest.py":
        return await fakeDigest(id);
      case "objective_scaffold.py":
        return await fakeScaffold(id, args);
      case "objective_check.py":
        return await fakeObjectiveCheck(id, args);
      case "objective_questions.py":
        return await fakeQuestions(id, args);
      case "compile_check.py":
        return await fakeCompile(id);
      case "validate_build.py":
        return await fakeValidate(id);
      case "render_mapping.py":
        return await fakeMapping(id);
      case "export_dbt.py":
        return await fakeDbtExport(id);
      case "validate_dbt.py":
        return await fakeDbtValidate(id);
      default:
        return result(2, "", `fake py has no script ${script}`);
    }
  };

  const errors = options.agentErrors ?? {};
  let reviews = 0;
  const recording: AgentRunner = {
    async run(role, manifest, task, ctx): Promise<AgentResult> {
      calls.roles.push(role);
      calls.tasks.push({ role, iteration: ctx?.iteration, task });
      calls.order.push(`agent:${role}`);
      const queued = errors[role]?.shift();
      if (queued) return { ok: false, error: queued, detail: `injected ${queued}`, toolCalls: 0, ms: 0 };
      const outcome = await runner.run(role, manifest, task, ctx);
      if (role === "analyzer" && state.objectiveAnswered) {
        // A revision session: the answers are applied to the objective, each question moved to `answered`.
        const objective = await readObjective(manifest.id);
        if ((objective.questions ?? []).length > 0) {
          objective.answered = [
            ...(objective.answered ?? []),
            ...objective.questions.map((q: { id: string; question: string }) => ({ id: q.id, question: q.question, answer: "yes" })),
          ];
          objective.questions = [];
          await writeJson(wfDir(root, manifest.id, "objective.json"), objective);
        }
      }
      if (role === "reviewer") {
        reviews += 1;
        if (scenario === "review-blocks" || (scenario === "review-blocks-once" && reviews === 1)) {
          await writeJson(wfDir(root, manifest.id, "review.json"), {
            verdict: "BLOCK",
            findings: [{ severity: "blocker", where: "build.sql:6", text: "NULL REGION rows are dropped" }],
          });
        }
      }
      return outcome;
    },
  };

  const env: Env = {
    root,
    config,
    runner: recording,
    interactive: options.interactive ?? false,
    hasGh: options.hasGh ?? false,
    ghEnabled: options.ghEnabled ?? false,
    py,
    sh: async (cmd, args) => {
      calls.sh.push({ cmd, args });
      return env.hasGh ? result(0, "https://example.invalid/pr/1") : result(127, "", `${cmd}: not found`);
    },
    sleep: async (ms) => {
      calls.sleeps.push(ms);
    },
    log: (line) => calls.logs.push(line),
  };

  runner.attach(env);

  return {
    root,
    env,
    calls,
    files: {
      path: (...rest) => path.join(root, ...rest),
      read: (...rest) => readFile(path.join(root, ...rest), "utf8").then((t) => t, () => undefined),
      exists: (...rest) => exists(path.join(root, ...rest)),
    },
    answerIntake: () => {
      state.intake = "READY";
    },
    answerObjective: () => {
      state.objectiveAnswered = true;
    },
    cleanup: () => rm(root, { recursive: true, force: true }),
  };
}
