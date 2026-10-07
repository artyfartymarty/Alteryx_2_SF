// The AgentRunner seam: everything except a live model call is exercised by MockRunner.
//
// MockRunner replays the canned artifacts a human wrote under samples/<wf>/canned/ (Task 13),
// so the state machine, the file contracts and the fix loop can be tested offline. It never
// invents a number or a verdict: it copies files, and the verdicts come from the scripts the
// orchestrator runs on them (compile_check.py, validate_build.py), never from a mock.
//
// CopilotRunner creates one disposable session per call, selects the custom agent by role and
// lets the hooks in hooks.ts enforce the permission policy.
import { appendFile, copyFile, mkdir, readdir, stat } from "node:fs/promises";
import path from "node:path";
import type { CopilotClient, CustomAgentConfig, SessionConfig } from "@github/copilot-sdk";
import {
  AUDIT_ARG_LIMIT,
  CONTEXT_OVERFLOW,
  errorText,
  hooksFor,
  MAX_ACT_DENIALS_PER_SESSION,
  MAX_READ_DENIALS_PER_SESSION,
  NOTES_ROLES,
  notesReminder,
  outputFolders,
  RATE_LIMIT,
  recordMetrics,
  redact,
} from "./hooks.ts";
import { readJsonOr, wfDir, writeJson } from "./manifest.ts";
import { sessionExcludedTools } from "./policy.ts";
import type {
  AgentCtx,
  AgentError,
  AgentResult,
  AgentRunner,
  Env,
  Manifest,
  OrchestratorConfig,
  Profile,
  Role,
} from "./types.ts";

type UserInputHandler = NonNullable<SessionConfig["onUserInputRequest"]>;
type UserInputRequest = Parameters<UserInputHandler>[0];
type UserInputResponse = Awaited<ReturnType<UserInputHandler>>;

export const USER_UNAVAILABLE =
  "The user is not available. Record this as an unchecked item in open_questions.md and continue.";

interface Outcome {
  ok: boolean;
  error?: AgentError;
  detail?: string;
}

const OK: Outcome = { ok: true };
const missing = (what: string): Outcome => ({ ok: false, error: "missing-output", detail: `no canned ${what}` });

async function exists(file: string): Promise<boolean> {
  try {
    await stat(file);
    return true;
  } catch {
    return false;
  }
}

async function copyInto(from: string, to: string): Promise<boolean> {
  if (!(await exists(from))) return false;
  await mkdir(path.dirname(to), { recursive: true });
  await copyFile(from, to);
  return true;
}

async function filesUnder(dir: string): Promise<string[]> {
  const out: string[] = [];
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await filesUnder(full)));
    else out.push(full);
  }
  return out.sort();
}

/** The scenarios a mock run can be told to act out: `fix-loop` (the translator's first build is the
 * deliberately broken one; the fixer then writes the canned one), `never-fixed` (the translator and
 * every fixer write the broken one) and `recovery-fails`. A workflow is translated as one unit, so a
 * scenario names no segment; anything after a `:` is ignored. */
function scenarioIs(scenario: string | undefined, kind: string): boolean {
  return scenario?.split(":")[0] === kind;
}

/** What a build file's extension says its language is. */
const BUILD_EXTENSIONS = [".sql", ".py"];

export class MockRunner implements AgentRunner {
  readonly root: string;
  readonly samplesDir: string;
  readonly scenario?: string;

  constructor(root: string, samplesDir: string, scenario?: string) {
    this.root = root;
    this.samplesDir = samplesDir;
    this.scenario = scenario;
  }

  /** Every runner is attached to the env that owns it. This one has no script to run (the stages run
   * them), so it keeps nothing. */
  attach(_env: Env): void {}

  async run(role: Role, wf: Manifest, _task: string, ctx?: AgentCtx): Promise<AgentResult> {
    const started = Date.now();
    const outcome = await this.replay(role, wf, ctx ?? {});
    return { ...outcome, toolCalls: 0, ms: Date.now() - started };
  }

  private canned(wf: Manifest, ...rest: string[]): string {
    return path.join(this.samplesDir, wf.id, "canned", ...rest);
  }

  /** The first broken build a human wrote for this workflow, in name order: a whole-file build
   * (`.sql` or `.py`) anywhere under `samples/<wf>/broken/` -- `broken.json`, which describes each case,
   * is never one. A variant with the canned build's own extension is preferred, so a Python
   * workflow's broken first attempt is a `.py` file. */
  private async brokenVariant(wf: Manifest, extension: string): Promise<string | undefined> {
    const builds = (await filesUnder(path.join(this.samplesDir, wf.id, "broken"))).filter((file) =>
      BUILD_EXTENSIONS.includes(path.extname(file).toLowerCase()),
    );
    return builds.find((file) => path.extname(file).toLowerCase() === extension) ?? builds[0];
  }

  private async replay(role: Role, wf: Manifest, ctx: AgentCtx): Promise<Outcome> {
    switch (role) {
      case "intake":
        return (await copyInto(this.canned(wf, "intake", "plan.md"), wfDir(this.root, wf.id, "intake", "plan.md")))
          ? OK
          : missing("intake/plan.md");

      case "analyzer":
        return await this.replayAnalyzer(wf);

      case "translator":
      case "fixer":
        return await this.replayBuild(role, wf, ctx.iteration ?? 0);

      case "reviewer":
        return (await copyInto(this.canned(wf, "review.json"), wfDir(this.root, wf.id, "review.json")))
          ? OK
          : missing("review.json");

      case "documenter":
        return (await copyInto(this.canned(wf, "docs", "migration.md"), wfDir(this.root, wf.id, "docs", "migration.md")))
          ? OK
          : missing("docs/migration.md");

      case "parser-recovery":
        return await this.replayRecovery(wf);

      default:
        return { ok: false, error: "error", detail: `no canned artifacts for role ${role}` };
    }
  }

  /** The analyzer's two files: `objective.json` over the orchestrator's pre-filled skeleton, and
   * `objective.md`. The tier and the language are read from the objective by the stage, never set here. */
  private async replayAnalyzer(wf: Manifest): Promise<Outcome> {
    if (!(await copyInto(this.canned(wf, "objective.json"), wfDir(this.root, wf.id, "objective.json")))) {
      return missing("objective.json");
    }
    return (await copyInto(this.canned(wf, "objective.md"), wfDir(this.root, wf.id, "objective.md")))
      ? OK
      : missing("objective.md");
  }

  /**
   * Replay the translator/fixer: the workflow's one build file. The canned tree holds `build.sql`, or
   * `build.py` for a Python workflow, and the file is written under its own name. On the `fix-loop`
   * scenario the translator's first build is the broken variant instead (the fixer then writes the
   * canned one); on `never-fixed` the fixer writes the broken one too. A translator also leaves its
   * `translation_notes.md`; a fixer appends to `fix_log.md`.
   */
  private async replayBuild(role: Role, wf: Manifest, iteration: number): Promise<Outcome> {
    let canned: string | undefined;
    for (const candidate of [this.canned(wf, "build.sql"), this.canned(wf, "build.py")]) {
      if (!canned && (await exists(candidate))) canned = candidate;
    }
    if (!canned) return missing("build.sql or build.py");

    const fixLoop = scenarioIs(this.scenario, "fix-loop");
    const neverFixed = scenarioIs(this.scenario, "never-fixed");
    const serveBroken = (role === "translator" && iteration === 0 && (fixLoop || neverFixed)) || (role === "fixer" && neverFixed);
    let source = canned;
    if (serveBroken) {
      const broken = await this.brokenVariant(wf, path.extname(canned).toLowerCase());
      if (!broken) return missing("broken/*.sql or broken/*.py");
      source = broken;
    }
    await copyInto(source, wfDir(this.root, wf.id, `build${path.extname(source).toLowerCase()}`));

    if (role === "translator") {
      await copyInto(this.canned(wf, "translation_notes.md"), wfDir(this.root, wf.id, "translation_notes.md"));
      return OK;
    }

    const fixLog = wfDir(this.root, wf.id, "fix_log.md");
    await mkdir(path.dirname(fixLog), { recursive: true });
    await appendFile(
      fixLog,
      `## iteration ${iteration}\n- symptom: see validation.json\n- fix: replayed from samples/${wf.id}/canned\n- status: ${serveBroken ? "UNFIXED" : "FIXED"}\n\n`,
      "utf8",
    );
    return OK;
  }

  private async replayRecovery(wf: Manifest): Promise<Outcome> {
    if (scenarioIs(this.scenario, "recovery-fails")) return OK; // diagnosed nothing; the re-parse will fail again
    const from = this.canned(wf, "parser-recovery");
    const files = await filesUnder(from);
    if (files.length === 0) return missing("parser-recovery/");
    for (const file of files) {
      const rel = path.relative(from, file).split(path.sep).join("/");
      const to = rel.startsWith("parsed/")
        ? wfDir(this.root, wf.id, ...rel.split("/"))
        : path.join(this.root, ...rel.split("/"));
      await copyInto(file, to);
    }
    const report = wfDir(this.root, wf.id, "parsed", "parse_report.json");
    const current = await readJsonOr<Record<string, unknown>>(report, {});
    await writeJson(report, { ...current, status: "RECOVERED" });
    return OK;
  }
}

export interface CopilotRunnerOptions {
  client: CopilotClient;
  root: string;
  config: OrchestratorConfig;
  profile: Profile;
  agents: CustomAgentConfig[];
  /** Live hardening, Task L7 fix round 1 (R-a): overrides `ABORT_BOUND_MS` for this runner. Tests
   * only -- production code never sets this and gets the real bound. */
  abortBoundMs?: number;
}

/** Live hardening, Task L7 fix round 1 (R-a): how long `run` waits for `session.abort()` to settle
 * after a timeout, before giving up on it and moving on to `disconnect()` regardless. Bounded so a
 * hung `abort()` call cannot itself hang `run` -- the SDK's own `sendAndWait` timeout is what put
 * this method here in the first place, so nothing here should be able to wait unboundedly either. */
const ABORT_BOUND_MS = 10_000;

export class CopilotRunner implements AgentRunner {
  readonly root: string;
  readonly config: OrchestratorConfig;
  readonly profile: Profile;
  readonly agents: CustomAgentConfig[];
  private readonly client: CopilotClient;
  private readonly abortBoundMs: number;
  private env?: Env;

  constructor(options: CopilotRunnerOptions) {
    this.client = options.client;
    this.root = options.root;
    this.config = options.config;
    this.profile = options.profile;
    this.agents = options.agents;
    this.abortBoundMs = options.abortBoundMs ?? ABORT_BOUND_MS;
  }

  attach(env: Env): void {
    this.env = env;
  }

  async run(role: Role, wf: Manifest, task: string, ctx?: AgentCtx): Promise<AgentResult> {
    const env = this.env;
    if (!env) throw new Error("CopilotRunner has no env; call attach(env) before running agents");
    const profile = this.config.profiles[this.profile];
    // Live hardening, Task L7 fix round 1 (R-d): once per session start, what the SDK was actually
    // told for this session's provider -- an operator reading the log can see the effective limits
    // without cross-referencing orchestrator.config.json, and a live run that overflows despite a
    // set maxPromptTokens (the wf_0007 probe) is diagnosable from the log alone.
    env.log(
      `${wf.id}: ${role} session starting: model=${profile.roleModels?.[role] ?? profile.model ?? "(none)"} ` +
        `maxPromptTokens=${profile.provider?.maxPromptTokens ?? "(unset)"} ` +
        `maxOutputTokens=${profile.provider?.maxOutputTokens ?? "(unset)"}`,
    );
    const { hooks, state } = hooksFor(role, wf, env);
    const started = Date.now();
    // `detail` is logged to the console and recorded in the manifest by stages.ts. errorText keeps
    // a whole error object as JSON (so classification sees everything), which means a key carried
    // by an upstream error could reach those places: redact, then bound, exactly like an audit line.
    const done = (error?: AgentError, detail?: string): AgentResult => ({
      ok: error === undefined,
      error,
      detail: detail === undefined ? undefined : redact(detail).slice(0, AUDIT_ARG_LIMIT),
      toolCalls: state.toolCalls,
      ms: Date.now() - started,
      severeDenials: state.severeDenials.length,
    });
    // Task L1 (R3) and Task L6 (R2), a documented deviation from the program spec's "tool denied by
    // policy -> abort stage, NEEDS_HUMAN" (docs/spec/01-copilot-setup.md): the spec's rule holds at once
    // for a SEVERE attempt -- one that reached outside the workflow or the sandbox, or tried to do damage
    // -- and any one parks the stage as `denied`. Every other refused call had no effect, and every
    // output is verified afterwards, so it is recorded and budgeted instead: attempted actions over
    // `budgets.maxActDenialsPerSession` (default 20) park the stage, and so do blocked reads over
    // `budgets.maxReadDenialsPerSession` (default 20); within both, the session's outcome is decided
    // exactly as if nothing had been denied. Every refused call is still refused, audited and counted.
    // Undefined means "no denial decides this session".
    const readBudget = this.config.budgets?.maxReadDenialsPerSession ?? MAX_READ_DENIALS_PER_SESSION;
    const actBudget = this.config.budgets?.maxActDenialsPerSession ?? MAX_ACT_DENIALS_PER_SESSION;
    const gradedDenials = (): AgentResult | undefined => {
      const all = [...state.severeDenials, ...state.actDenials, ...state.readDenials].join("; ");
      if (state.severeDenials.length > 0) {
        return done("denied", `severe-denials: ${state.severeDenials.length} (parks at once); ${all}`);
      }
      if (state.actDenials.length > actBudget) {
        return done("denied", `act-denials: ${state.actDenials.length} over the budget of ${actBudget}; ${all}`);
      }
      if (state.readDenials.length > readBudget) {
        return done("denied", `read-denials: ${state.readDenials.length} over the budget of ${readBudget}; ${all}`);
      }
      return undefined;
    };

    // Task N1 (live evidence, docs/live-smoke-test.md "Third live test"), generalised by Task L9
    // (R2, live evidence task-L9-brief.md): a role's session is told to write into folders whose
    // policy lane has no directory creation -- so the orchestrator creates every one of them
    // itself, before the session starts. Each path is built only from this.root, the fixed
    // "workflows" segment, wf.id and hooks.ts's outputFolders (role and the fixed ids ctx carries)
    // -- never from task text. Every folder is tried independently, so one failure never stops the
    // rest; each failure is logged (naming the workflow, the role and the folder) and the session
    // still runs regardless -- this never throws out of run.
    for (const folder of outputFolders(role)) {
      try {
        await mkdir(wfDir(this.root, wf.id, ...folder.split("/")), { recursive: true });
      } catch (error) {
        env.log(
          `${wf.id}: ${role} could not create ${folder}/: ${redact(errorText(error)).slice(0, AUDIT_ARG_LIMIT)}`,
        );
      }
    }

    let session;
    // Task W4: unsubscribed in `finally` below, on every exit path (a normal return, a thrown
    // error, a timeout) -- never left attached to a session this method is done with.
    const unsubscribe: (() => void)[] = [];
    try {
      session = await this.client.createSession({
        workingDirectory: this.root,
        model: profile.roleModels?.[role] ?? profile.model,
        reasoningEffort: profile.roleReasoningEffort?.[role] ?? profile.reasoningEffort,
        ...(profile.roleContextTiers?.[role] ? { contextTier: profile.roleContextTiers[role] } : {}),
        provider: profile.provider,
        mcpServers: this.config.mcpServers,
        customAgents: this.agents,
        agent: role,
        // Live hardening, Task L9 (R1): never offer a tool the policy refuses for every role to
        // begin with -- defence in depth on top of the policy itself (POLICY.md), extendable from
        // orchestrator.config.json's session.excludedTools.
        excludedTools: sessionExcludedTools(this.config.session?.excludedTools),
        hooks,
        // The policy already decided in onPreToolUse, so the session never blocks on a prompt.
        onPermissionRequest: () => ({ kind: "approve-once" }),
        onUserInputRequest: (request) => this.answer(request, env),
      });
      // `session` is reassigned in this `let`, so a plain `const` alias is what lets these two
      // closures (invoked later, asynchronously, by the SDK) keep a definitely-assigned reference
      // instead of the widened "maybe still undefined" type `session` itself carries outside a
      // straight-line read (spike fact S5: session.d.ts:190's `on`, types.d.ts:2791's `send`).
      const activeSession = session;
      unsubscribe.push(
        activeSession.on("session.compaction_complete", (event) => {
          if (!event.data.success) {
            env.log(`${wf.id}: ${role} context compaction failed`);
            return;
          }
          state.compactions += 1;
          env.log(`${wf.id}: ${role} context compacted (${state.compactions} so far)`);
          // The reminder names only the fixed notes path -- never workflow-authored text -- and is
          // sent `mode: "immediate"` so it lands before whatever the agent does next, not queued
          // behind it. Fire-and-forget: a failed send is logged (fix round 1 minor), not thrown --
          // this handler runs inside the SDK's own event dispatch, not CopilotRunner.run's try/catch.
          if (NOTES_ROLES.includes(role)) {
            void activeSession
              .send({ prompt: notesReminder(role, wf.id), mode: "immediate" })
              .catch((error) => env.log(`${wf.id}: ${role} notes reminder failed to send: ${errorText(error)}`));
          }
        }),
      );
      unsubscribe.push(
        activeSession.on("assistant.usage", (event) => {
          const input = event.data.inputTokens;
          if (typeof input === "number" && input > state.peakInputTokens) state.peakInputTokens = input;
        }),
      );
      await session.sendAndWait({ prompt: task }, this.config.sessionTimeoutMs);
    } catch (error) {
      const text = errorText(error);
      const timedOut = /timed?\s?out|timeout/i.test(text);
      // Live hardening, Task L7 fix round 2 (IMP-1): every timeout aborts the in-flight turn
      // (bounded, then still disconnected in `finally` regardless), BEFORE the session is
      // classified -- including a session with a severe denial, which the check right below used
      // to return `denied` on without ever reaching this block. `sendAndWait`'s timeout "does not
      // abort in-flight agent work" (session.d.ts ~154), so the one turn most worth stopping -- one
      // that already tried to reach outside its lane -- was exactly the one fix round 1's version
      // of this block (unconditionally after the severe check) skipped.
      if (timedOut && session) {
        // A local, definitely-non-null alias for the closure below -- the same reason `activeSession`
        // exists in the `try` block above (spike fact S5: a `let` narrowed by `if (... && session)`
        // does not stay narrowed inside a nested closure).
        const timedOutSession = session;
        let bound: NodeJS.Timeout | undefined;
        let offIdle: (() => void) | undefined;
        try {
          await Promise.race([
            (async () => {
              // Live hardening, Task L7 fix round 1 review (L7-m1): `abort()` "resolves when the
              // abort request is acknowledged" (session.d.ts ~282), not when the turn has actually
              // stopped -- a shell command or an attached sub-agent task can still be running, and
              // still writing to disk, until the session actually goes idle. `session.idle`'s
              // `IdleData.aborted` (generated/session-events.d.ts ~1417) is the SDK's own signal for
              // that: "no background agents or attached shell commands in flight". The listener is
              // registered BEFORE `abort()` is called, so an idle event fired synchronously inside
              // `abort()` itself cannot be missed.
              const idle = new Promise<void>((resolve) => {
                offIdle = timedOutSession.on("session.idle", (event: { data?: { aborted?: boolean } }) => {
                  if (event.data?.aborted) resolve();
                });
              });
              await timedOutSession.abort();
              await idle;
            })(),
            new Promise<never>((_, reject) => {
              bound = setTimeout(
                () => reject(new Error(`abort()/session.idle did not settle within ${this.abortBoundMs}ms`)),
                this.abortBoundMs,
              );
            }),
          ]);
        } catch (abortError) {
          env.log(
            `${wf.id}: ${role} session.abort() after timeout failed or did not finish in time: ` +
              `${errorText(abortError)}`,
          );
        } finally {
          // The losing side of the race otherwise leaves a live timer for up to abortBoundMs after
          // a fast, successful abort() -- harmless (Promise.race already handled both promises, so
          // this can never become an unhandled rejection), but needless work for the event loop.
          if (bound) clearTimeout(bound);
          offIdle?.();
        }
      }
      // L6 fix round 2 (I3): a SEVERE attempt parks the session whatever ended it -- the check
      // comes first on every exit path (wins over context-overflow/rate-limit too, unchanged).
      // Judged after the error, a severe attempt followed by a timeout or a rate limit came back
      // `timeout` / `rate-limit`: retried, or (Task L7) kept as a success when the output on disk
      // passed its check.
      if (state.severeDenials.length > 0) return gradedDenials()!;
      // Live hardening, Task L7 fix round 2 (L7-m2): an over-budget act/read denial (not severe)
      // wins over a timeout classification too, on the same principle -- L6 fix round 2's I3 ruled
      // only on severe denials, so a session that blew its (non-severe) denial budget and then also
      // timed out fell all the way through to `done("timeout", text)` unclassified by its denials at
      // all, unlike a crash, which already fell back to `gradedDenials() ?? done("error", text)`
      // below. `gradedDenials()` here is `undefined` whenever neither budget was actually exceeded,
      // so an ordinary timeout (nothing denied, or denials within budget) is unaffected.
      if (timedOut) return gradedDenials() ?? done("timeout", text);
      // Otherwise the thrown error is judged before any denial: a crash that happens minutes
      // after earlier, already-recovered-from permission denials must not be misreported as
      // "denied" just because a denial happened at some point earlier in the same session
      // (live evidence, task-16-report.md ATTEMPT 1/2 — a context-window overflow was
      // misclassified as "denied" this way). "denied" is the fallback classification, used only
      // when no other error signature explains why the session actually ended -- and, since
      // Tasks L1 and L6, only for attempted actions or blocked reads over their budgets
      // (gradedDenials).
      //
      // context-overflow is judged BEFORE rate-limit and wins when both signatures are present
      // (F7, final review), mirroring hooks.ts's onErrorOccurred: an anchored "429" can still
      // legitimately co-occur with an "exceeds the available context size" message, and the two
      // errors have different retry policies (RETRY_ONCE never retries context-overflow, but
      // rate-limit backs off and retries up to MAX_RATE_LIMIT_RETRIES times) -- misclassifying a
      // context overflow as a rate limit burns three paid retries that can never succeed.
      // state.contextOverflow (set from onErrorOccurred) is an OR-fallback for CONTEXT_OVERFLOW
      // on the thrown error's own text: the SDK's error-reporting hook may see the real signature
      // even when what sendAndWait itself throws does not.
      if (state.contextOverflow || CONTEXT_OVERFLOW.test(text)) return done("context-overflow", text);
      if (state.rateLimited || RATE_LIMIT.test(text)) return done("rate-limit", text);
      // Task L1 (R3), Task L6 (R2): attempted actions or blocked reads over their budgets explain
      // the end; denials within the budgets do not, so the session ends as the error it threw.
      return gradedDenials() ?? done("error", text);
    } finally {
      // Task W4: unsubscribed before disconnect, on every exit path -- including the timeout and
      // crash paths that reach this block via the catch above.
      for (const off of unsubscribe) off();
      await session?.disconnect().catch(() => undefined);
      // Every session's spend is recorded here, whether or not onSessionEnd fired for it (it was
      // observed live not to fire on this crash path at all — task-16-report.md ATTEMPT 2).
      // recordMetrics is idempotent per session, so a session where onSessionEnd DID already run
      // is not double-counted.
      recordMetrics(wf, role, state, Date.now() - started);
      // Task L1 (R3), Task L6 (R2): one line per session, on every exit path, naming all three
      // counts; audit.jsonl holds every denial with its class.
      env.log(
        `${wf.id}: ${role} session denials: ${state.readDenials.length} read (budget ${readBudget}), ` +
          `${state.actDenials.length} act (budget ${actBudget}), ${state.severeDenials.length} severe ` +
          `— see workflows/${wf.id}/audit.jsonl`,
      );
    }

    // A completed session that tried to reach outside its workflow or the sandbox, or kept trying
    // what it may not, is still a prompt bug, not a success; one whose refused calls stayed within the
    // budgets goes on to be judged by its stage's own verify callback.
    return gradedDenials() ?? done();
  }

  private async answer(request: UserInputRequest, env: Env): Promise<UserInputResponse> {
    if (!env.interactive) return { answer: USER_UNAVAILABLE, wasFreeform: true };
    const { createInterface } = await import("node:readline/promises");
    const rl = createInterface({ input: process.stdin, output: process.stdout });
    try {
      const choices = request.choices?.length
        ? `\n${request.choices.map((choice, i) => `  ${i + 1}) ${choice}`).join("\n")}\n`
        : " ";
      const typed = (await rl.question(`\n${request.question}${choices}> `)).trim();
      const chosen = request.choices?.[Number(typed) - 1];
      return { answer: chosen ?? typed, wasFreeform: chosen === undefined };
    } finally {
      rl.close();
    }
  }
}
