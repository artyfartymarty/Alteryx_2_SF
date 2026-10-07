// Shared types for the orchestrator. No behaviour lives here.
import type { MCPServerConfig } from "@github/copilot-sdk";

export type Role =
  | "intake"
  | "analyzer"
  | "translator"
  | "reviewer"
  | "fixer"
  | "parser-recovery"
  | "documenter";

export type Stage = "parse" | "intake" | "analyze" | "golden" | "translate" | "document" | "pr";

export const STAGES: Stage[] = ["parse", "intake", "analyze", "golden", "translate", "document", "pr"];

export const ROLES: Role[] = [
  "intake",
  "analyzer",
  "translator",
  "reviewer",
  "fixer",
  "parser-recovery",
  "documenter",
];

export type Tier = "T1" | "T2" | "T3";

/** What the one build file of a workflow is written in (objective-first design §2, D1): `sql` is
 * `build.sql`, plain Snowflake SQL; `python` is `build.py`, one Snowpark module, used only where SQL
 * cannot express the logic well. `objective.json` carries the choice; the manifest mirrors it. */
export type Language = "sql" | "python";

export interface Manifest {
  id: string;
  tier?: Tier;
  /** The language of the workflow's build file, mirrored from `objective.json` once analyze has
   * verified it. Unset until then. */
  language?: Language;
  golden_sets?: string[];
  status: Record<string, string>;
  metrics: Record<string, any>;
  parse?: { attempts: number; extensions: string[] };
  /** Why a stage stopped, when the status alone does not say: budget, script-error, … */
  reasons?: Record<string, string>;
  /** A manifest written by the segment-based pipeline may still carry `segments`, `segment_status`,
   * `segmentation`, `output_kind` or `output_target`: they are tolerated here and never read. */
  [k: string]: unknown;
}

export interface ShResult {
  ok: boolean;
  code: number;
  out: string;
  err: string;
}

/** "context-overflow": the model's own context window filled up (a real BYOK-server failure
 * mode, live-verified — see task-16-report.md). It is a reason, not a status: an escalated
 * workflow still lands on the existing NEEDS_HUMAN status, same as any other "error"; it is never
 * retried (orchestrator/stages.ts's RETRY_ONCE), since an identical retry cannot succeed. */
export type AgentError = "missing-output" | "denied" | "timeout" | "rate-limit" | "error" | "context-overflow";

export interface AgentResult {
  ok: boolean;
  error?: AgentError;
  detail?: string;
  toolCalls: number;
  ms: number;
  /** How many of the session's refused calls were severe (Task L6, R2). CopilotRunner reports every
   * session with one as `denied`; `runAgent` never keeps, verifies or retries such a session whatever
   * `error` a runner reports (L6 fix round 2, I3). Absent means none. */
  severeDenials?: number;
}

/** What one agent call acts on. A workflow is translated as one unit, so the only thing a call
 * carries is which turn of the translate loop it is (0 is the translator's, later ones the fixer's). */
export interface AgentCtx {
  iteration?: number;
}

export interface AgentRunner {
  run(role: Role, wf: Manifest, task: string, ctx?: AgentCtx): Promise<AgentResult>;
}

/** Everything a stage is allowed to touch outside pure computation. */
export interface Env {
  root: string;
  config: OrchestratorConfig;
  runner: AgentRunner;
  interactive: boolean;
  /** `gh` answered `--version`; only ever probed when `ghEnabled`. */
  hasGh: boolean;
  /** GitHub integration is on for this run (`github.enabled` or `--gh`); off, no stage calls `gh`. */
  ghEnabled: boolean;
  py(script: string, args: string[], opts?: { inheritStdio?: boolean }): Promise<ShResult>;
  sh(cmd: string, args: string[]): Promise<ShResult>;
  sleep(ms: number): Promise<void>;
  log(line: string): void;
}

export type Profile = "local" | "hosted";

export type ReasoningEffort = "low" | "medium" | "high" | "xhigh" | "max";

export interface ProfileConfig {
  /** BYOK provider; omitted for the hosted profile, which uses Copilot's own models. `maxPromptTokens`
   * / `maxOutputTokens` (live hardening, Task L7, R1) are passed to the SDK's `createSession` unchanged
   * (`ProviderConfig` in `node_modules/@github/copilot-sdk/dist/types.d.ts`): the runtime triggers its
   * own conversation compaction before a request whose prompt would exceed `maxPromptTokens`. The local
   * BYOK profile sets `maxPromptTokens` because the SDK does not know a self-hosted model's limits and
   * would otherwise compact too late for `llama-server`'s own per-slot context budget (see
   * `orchestrator.config.json`, `scripts/dev/serve_model.ps1`'s `-Parallel` and README §3); the hosted
   * profile sets neither, since the SDK already knows Copilot's own models' limits. */
  provider?: {
    type?: "openai" | "azure" | "anthropic";
    baseUrl: string;
    apiKey?: string;
    maxPromptTokens?: number;
    maxOutputTokens?: number;
  };
  model?: string;
  reasoningEffort?: ReasoningEffort;
  /** Per-role model override; falls back to `model`. */
  roleModels?: Partial<Record<Role, string>>;
  /** Per-role context-window tier (docs/handoff-copilot-models.md §4 item 4): `long_context` pins
   * the session to the long-context tier of the SAME model, never a jump to a pricier one (owner's
   * model policy, §1 item 2). Absent for a role means the SDK's own default. The local BYOK
   * profile ignores this by construction (one model, one window). */
  roleContextTiers?: Partial<Record<Role, "default" | "long_context">>;
  /** Per-role reasoning effort override; falls back to `reasoningEffort`. */
  roleReasoningEffort?: Partial<Record<Role, ReasoningEffort>>;
}

export interface OrchestratorConfig {
  /** Python interpreter, resolved relative to the run root. */
  python: string;
  /** Canned agent artifacts for the mock runner, resolved relative to the run root. */
  samplesDir: string;
  parallelism: number;
  maxFixIterations: number;
  maxParseRecovery: number;
  sessionTimeoutMs: number;
  golden: { producer: "simulator" | "alteryx" };
  /** `maxReadDenialsPerSession` (Task L1, R3): how many blocked READS one agent session may collect
   * before it parks as `denied`; absent means 20. `maxActDenialsPerSession` (Task L6, R2): how many
   * attempted actions that are not severe it may collect; absent means 20, and 0 parks on the first. A
   * severe attempt parks it at once. */
  budgets: { maxToolCallsPerWorkflow: number; maxReadDenialsPerSession?: number; maxActDenialsPerSession?: number };
  profiles: Record<Profile, ProfileConfig>;
  /** Passed straight to createSession; the Snowflake MCP server goes here. */
  mcpServers?: Record<string, MCPServerConfig>;
  /** Permission-policy configuration; see orchestrator/POLICY.md. */
  policy?: { sandboxDatabases?: string[] };
  /** GitHub integration (an issue for open intake questions, a pull request after document). Off
   * unless `enabled` is true or the run passes `--gh`; off, the orchestrator never invokes `gh`. */
  github?: { enabled?: boolean };
  /** Live hardening, Task L9 (R1): extra tool names for the SDK's own `excludedTools` (never
   * offered to the model at all), on top of the fixed list every role's policy refuses outright
   * (`policy.ts`'s `ALWAYS_EXCLUDED_BUILTIN_TOOLS`). Validated in `loadConfig`: a list of
   * non-empty strings, or absent (the fixed list alone). */
  session?: { excludedTools?: string[] };
}

export interface RunOptions {
  only?: string;
  fromStage?: Stage;
  stopAfter?: Stage;
  tier?: Tier;
  dryRun?: boolean;
  runner?: "mock" | "copilot" | "external";
  profile?: Profile;
  /** Undefined means "decide from stdin being a TTY". */
  interactive?: boolean;
  root?: string;
  scenario?: string;
  /** `--check-models`: list the catalog and check every configured hosted-profile id, instead of
   * running any workflow (docs/handoff-copilot-models.md §2). */
  checkModels?: boolean;
  /** `--gh`: GitHub integration on for this run, whatever `github.enabled` says. */
  gh?: boolean;
}
