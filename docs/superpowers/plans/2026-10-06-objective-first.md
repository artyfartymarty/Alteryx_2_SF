# Objective-first translation — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`)
> syntax. This is a refactor of a large, heavily tested codebase: each task names exact files, interfaces, behaviours
> and test cases; the implementer reads the existing code it replaces and moves logic rather than rewriting it where
> this plan says "move".

**Goal:** Replace tool-by-tool segment translation with an objective-first pipeline that writes one minimal
`build.sql` (or `build.py`) per workflow, checked against golden data, with a generated Mermaid map of Alteryx steps
to SQL lines.

**Architecture:** Python scripts do every deterministic step (digest, objective scaffold/check/questions, compile
check with a minimality lint, golden validation, mapping render, deploy); the TypeScript orchestrator drives the
stages and the agents (analyzer → objective; translator/fixer → one file; reviewer); the validator agent is retired.

**Tech stack:** Python 3.14 (`.venv`), sqlglot (Snowflake dialect) + DuckDB as the local double, Snowpark Local
Testing; Node 22 with `--experimental-strip-types`, `@github/copilot-sdk`; Mermaid 11.

**Spec:** `docs/superpowers/specs/2026-10-06-objective-first-design.md` (read it first; §-references below are to it).

## Global Constraints

- Python: run everything with `.venv/Scripts/python.exe` from the repo root (or worktree root). `pyproject.toml`
  already sets `-q`; do not add another. Never pipe a heredoc into `python`.
- Node 22: `fnm exec --using=22 node --experimental-strip-types --test orchestrator/test/*.test.ts`;
  typecheck `fnm exec --using=22 npm run typecheck` (i.e. `tsc --noEmit`).
- Files: UTF-8, LF line endings; JSON with indent 2 and a trailing newline (`lib/io.py` helpers).
- `docs/spec/**` is never edited.
- No machine paths, user names or login names in committed files.
- Never `git stash`, `git checkout -- <path>`, `git reset --hard`, `git clean`. Commit early with `wip:` commits;
  final commits per task use `feat:`/`refactor:`/`test:`/`docs:` prefixes and end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never push. Never authenticate anywhere, never enter credentials or tokens.
- No adversarial security work: no attack passes, no exploit probes. Keep existing safety checks working; do not
  extend them beyond what this plan asks.
- Nothing in this repo has run on real Snowflake or Alteryx: keep that honesty in docstrings and docs.
- Code style: match the surrounding code — module docstrings that explain *why*, typed signatures, small functions,
  error messages that name the file, field and expected value.
- Every suite green at the end of each task for the files that task owns: `pytest` (whole suite at the end of A6),
  `node --test`, `tsc --noEmit`. No skipped tests, no `xfail`.
- Placeholders in `build.sql` are exactly `{{src}}` and `{{tgt}}` (inner spaces allowed). Step markers are
  `-- step s1` / `-- step s1, s3` (SQL) and `# step s1` (Python). Step ids match `^s[1-9][0-9]*$`. Objective question
  ids match `^O[1-9][0-9]*$`.
- Exit codes for every new script: 0 success, 1 domain failure (the thing checked is wrong), 2 usage error
  (missing prerequisite file, unknown workflow, bad arguments).

## Lanes and order

Two lanes run in parallel in separate git worktrees off branch `objective-first`:

- **Lane A (Python):** A1 → A2 → A3 → A4 → A5 → A6, worktree `.worktrees/lane-a`, branch `wt/lane-a`.
- **Lane B (TypeScript + agent files):** B1 (after A1 is merged, using the script interfaces in this plan), then B2
  (orchestrator integration test + regenerated `workflows/`, after A6 is merged), then B3 (agent files, cookbook,
  docs). Worktree `.worktrees/lane-b`, branch `wt/lane-b`.
- **A7 (dbt export, added 2026-10-06, spec D9/§4.10):** after A4 is merged, in parallel with A6, in the lane that is
  free. B2 runs after A6 and A7.
- **C1:** live verification with a real model (controller-run), then the final review.

The controller merges each finished task into `objective-first`; a lane rebases (or merges `objective-first`) before
its next task when it needs the other lane's work.

---

### Task A1: Foundations — `lib/build_sql.py`, `lib/objective.py`, `lib/mermaid_lint.py`

**Files:**
- Create: `scripts/lib/build_sql.py`, `scripts/lib/objective.py`, `scripts/lib/mermaid_lint.py`
- Create tests: `tests/test_build_sql.py`, `tests/test_objective_lib.py`, `tests/test_mermaid_lint.py`
- Modify: `tests/test_readme_diagrams.py` (use `lib.mermaid_lint` instead of its private helpers; behaviour unchanged)

**Interfaces (produced):**

```python
# scripts/lib/build_sql.py
PLACEHOLDER_NAMES = ("src", "tgt")

@dataclass(frozen=True)
class Statement:
    text: str        # the statement without its terminating ';' (leading comments kept)
    start_line: int  # 1-based line of its first code character (not a comment or blank)
    end_line: int    # 1-based line holding its terminating ';' (or its last code line)

@dataclass(frozen=True)
class Region:
    steps: tuple[str, ...]   # e.g. ("s1", "s3")
    marker_line: int         # 1-based line of the marker comment
    start_line: int          # first code line after the marker
    end_line: int            # last code line before the next marker or EOF

def placeholders(text: str) -> list[tuple[str, int]]: ...      # every {{ name }} with its 1-based line, unknown names included
def render(text: str, values: dict[str, str]) -> str: ...       # ValueError naming a placeholder with no value or an unknown name
def split_statements(text: str) -> list[Statement]: ...         # aware of '...' strings, "..." identifiers, -- and /* */ comments, $$ bodies
def step_regions(text: str, comment: str = "--") -> list[Region]: ...   # comment="#" for build.py
def session_settings(statement: Statement) -> dict[str, str] | None: ...  # ALTER SESSION SET a = 'x', b = 1 → {"TIMEZONE": "America/New_York", "WEEK_START": "1"}; None if not that form
def masked_code(text: str, *, strings: bool = True) -> str: ... # moved verbatim from lib/proc_runner.py (same behaviour)
```

```python
# scripts/lib/objective.py
WRITE_MODES = ("overwrite", "append", "truncate_append", "update_insert")
LANGUAGES = ("sql", "python")
TIERS = ("T1", "T2", "T3")
STEP_ID = re.compile(r"^s[1-9][0-9]*$")
QUESTION_ID = re.compile(r"^O[1-9][0-9]*$")

def objective_path(repo: Repo, wf_id: str) -> Path: ...          # workflows/<id>/objective.json
def load(repo: Repo, wf_id: str) -> dict: ...                     # FileNotFoundError / ValueError (not a JSON object)
def build_path(repo: Repo, wf_id: str) -> Path | None: ...        # build.sql or build.py that exists; ValueError if both exist
def contract_view(objective: dict, output: dict) -> dict: ...     # the segment-contract shape compare.py reads, for ONE output
def dag_tool_ids(dag: dict) -> list[str]: ...                      # every top-level tool id of parsed/dag.json, macro internals excluded
```

`contract_view` returns `{"workflow": objective["workflow"], "segment": None, "target": objective["language"],
"inputs": objective["inputs"], "outputs": [entry], "output": entry, "row_relation": output.get("row_relation"),
"ordering": output.get("ordering") or {}, "tolerances": output.get("tolerances") or {},
"normalizations": output.get("normalizations") or [], "parity_risks": objective.get("parity_risks") or []}` where
`entry` is `output` plus `"kind": "target"` and `"stream": None` when absent. Read `scripts/compare.py` and confirm
every key `compare.compare`/`select_output` reads is present; add any it needs (with a test) rather than changing
`compare.py`.

`lib/mermaid_lint.py`: move the README diagram lint rules out of `tests/test_readme_diagrams.py` into
`def lint_mermaid(block: str) -> list[str]` (one message per problem, empty when clean) and
`def mermaid_blocks(markdown: str) -> list[str]`, plus `def escape_label(text: str) -> str` (escapes `"` as `#quot;`,
`#` as `#35;`, `;` as `#59;`, `<`/`>` as `#lt;`/`#gt;` except a literal `<br/>`, which is kept). The README tests keep
passing unchanged in behaviour.

- [ ] Step 1: write failing tests for `split_statements` (a `;` inside a string, an identifier, a `--` comment, a
  `/* */` comment and a `$$…$$` body does not split; line numbers are exact; a trailing statement without `;` is
  returned), `placeholders`/`render` (spaces inside braces; unknown name reported with its line; missing value is a
  `ValueError` naming it), `step_regions` (single and multiple steps per marker; region ends before the next
  marker; trailing blank/comment lines trimmed; `#` comments for Python; a marker inside a string is not a marker),
  `session_settings`.
- [ ] Step 2: implement `build_sql.py` (move `_split_code`/`masked_code`/span helpers from `lib/proc_runner.py`;
  leave `proc_runner.py` in place for now — A6 deletes it — importing the moved helpers from `build_sql` so there is
  one copy).
- [ ] Step 3: tests + implementation for `objective.py` (`contract_view` on a hand-written objective; `dag_tool_ids`
  on `samples/wf_0002` parsed through `tests/helpers.prepare_workflow` or the parser directly — include a macro
  sample if one exists among wf_0001–wf_0007).
- [ ] Step 4: `mermaid_lint.py` + tests; switch `tests/test_readme_diagrams.py` to it.
- [ ] Step 5: run `pytest tests/test_build_sql.py tests/test_objective_lib.py tests/test_mermaid_lint.py tests/test_readme_diagrams.py tests/test_proc_runner*.py` → all pass; commit.

---

### Task A2: The objective — `digest.py`, `objective_scaffold.py`, `objective_check.py`, `objective_questions.py`, canned objectives

**Files:**
- Create: `scripts/digest.py`, `scripts/objective_scaffold.py`, `scripts/objective_check.py`,
  `scripts/objective_questions.py`
- Create tests: `tests/test_digest.py`, `tests/test_objective_scaffold.py`, `tests/test_objective_check.py`,
  `tests/test_objective_questions.py`
- Create: `samples/<wf>/canned/objective.json` and `samples/<wf>/canned/objective.md` for wf_0001…wf_0007
- Create: `docs/reference/objective.md` (the objective's fields, mechanical vs judgment, vocabularies — moved from
  `docs/reference/contracts.md`, which A6 deletes)
- Read (do not delete yet): `scripts/contract_scaffold.py`, `scripts/contract_check.py`, `scripts/prompt_context.py`,
  `scripts/parsers/plugin_map.py`, `samples/<wf>/canned/segments/*/contract.json`, `samples/<wf>/canned/analysis.md`

**Interfaces (consumed):** A1's `lib/objective.py`, `lib/build_sql.py`.

**Behaviour:**
- `digest.py <wf_id> [--root .]`: spec §4.1. Writes `workflows/<id>/digest.md`, prints the path. Deterministic
  (same input → byte-identical output). Reuse `prompt_context.py`'s per-tool rendering (`_dag_lines_by_tool`,
  `dag_summary_lines`) by moving it into a shared module (e.g. `scripts/lib/tool_lines.py`) that both import; widen it
  so each line carries the behaviour-deciding config listed in §4.1 (read `scripts/parsers/tool_config.py` for the
  parsed config keys). Exit 2 if `parsed/dag.json` is missing.
- `objective_scaffold.py <wf_id> (--prefill | --apply) [--root .]`: spec §4.2 "Mechanical". Move the derivations
  from `contract_scaffold.py` (input/output columns and types from RecordInfo, write mode and keys from
  `intake/mappings.yaml` including its `merge` → `update_insert` rule, PreSQL/PostSQL from the Output tool config,
  `logical` from the mapping entry whose `tool_ids` holds the tool, else `null`). `--prefill` writes a complete
  skeleton only when `objective.json` does not exist: mechanical fields filled, judgment fields present and empty
  (`""`, `[]`, `{}`, `null`; `tier`/`language` `null`). `--apply` rewrites only the mechanical fields of an existing
  file and leaves every judgment field byte-for-byte as the analyzer wrote it; a column the analyzer added or
  dropped is restored to the mechanical list (judgment fields of a surviving column are kept, matched by name).
  Fields it cannot derive: a note on stderr, field left as is. Exit 1 if `--apply` finds no file or a non-object.
- `objective_check.py <wf_id> [--ready] [--root .]`: every rule of §4.2 as a named problem printed one per line to
  stderr as `<field>: <problem>; expected <what>` (e.g. `steps: tool 12 is in no step and not in no_effect; expected
  every tool id of parsed/dag.json exactly once`). Checks: required keys and types; `tier` in TIERS and `language`
  in LANGUAGES (non-null); `language_reason` non-empty iff `python`; a `snowpark`-class node forces `python`, a
  `manual`-class node forces `T3`; step ids valid and unique, `feeds` valid; every dag tool id exactly once across
  `steps[].tools` ∪ `no_effect[].tool_id`, and no unknown ids; every output's tool id inside some step; each
  output's `keys` ⊆ its column names; every column has a boolean `nullable`; vocabularies of `row_relation`,
  `parity_risks[].class`, `normalizations`, `tolerances` keys exactly as `contract_check.py` enforced them (move
  those constants); question ids valid and unique; mechanical fields equal a fresh derivation (call the scaffold's
  derivation in memory, never write). `--ready` adds: every `logical` non-null and `questions` empty. Exit 0/1/2.
- `objective_questions.py <wf_id> (--write | --status) [--root .]`: spec §4.3. `--write` renders
  `intake/objective_questions.md` from `objective.json.questions`, keeping any answer already typed in the file and
  filling `Answer:` from `manifest.answers["O<n>"]` when the file has none; removes the file and exits 0 when there
  are no questions. `--status` exits 0 when every question has an answer (or there are none), 1 otherwise, printing
  the open ids. Expose `answers(repo, wf_id) -> dict[str, str]` for tests and other scripts.
- Canned objectives: convert each sample's canned contracts + `analysis.md` into `canned/objective.json` and
  `canned/objective.md` (steps chosen as a person would describe the business process, not one per tool; wf_0006's
  language is `python` with a reason; the parity risks carried over). Each must pass
  `objective_check.py --ready` on a workflow prepared from the sample with its answers applied (use
  `tests/helpers.py`'s preparation path; add a helper if needed). None of the canned objectives has open questions;
  add one test fixture objective with a question for the questions script's tests.

- [ ] Step 1: failing tests for digest (wf_0001 golden digest text asserted line by line for 3 tools; a shared tool
  is listed once; determinism), then implement.
- [ ] Step 2: failing tests for the scaffold (`--prefill` on wf_0001 equals the expected mechanical fields; `--apply`
  restores a tampered type and keeps a judgment `meaning`; `logical` null before intake answers), then implement.
- [ ] Step 3: failing tests for the checker (one test per named problem; every canned objective passes `--ready`),
  then implement.
- [ ] Step 4: failing tests for questions (`--write` keeps a typed answer; manifest answer fills a blank; no questions
  → no file; `--status` codes), then implement.
- [ ] Step 5: write the seven canned objectives and `docs/reference/objective.md`; run the new tests plus
  `pytest tests/test_prompt_context*.py tests/test_contract*.py` (old behaviour still intact); commit.

---

### Task A3: `compile_check.py` for one file, the minimality lint, Snowpark rules for `main(session, src, tgt)`, canned builds

**Files:**
- Rewrite: `scripts/compile_check.py` (CLI `compile_check.py <wf_id> [--root .]`; writes
  `workflows/<id>/compile_check.json`, spec §4.6)
- Create: `scripts/lib/minimality.py`, `tests/test_minimality.py`
- Modify: `scripts/lib/snowpark_rules.py` (entry point `main(session, src, tgt)`; reads `session.table(f"{src}.X")`;
  writes `…save_as_table(f"{tgt}.X", mode=…)` / the existing MERGE form; step markers `# step sN` instead of
  `# tool <id>:` comments), `scripts/lib/write_modes.py` (targets from the objective instead of a contract)
- Rewrite tests: `tests/test_compile_check*.py` → `tests/test_compile_check.py` (+ split files if large),
  `tests/test_snowpark_rules.py` (keep every existing safety case, adapted to the new signature)
- Create: `samples/<wf>/canned/build.sql` (or `build.py` for wf_0006) for wf_0001…wf_0007, condensed by hand from
  the sample's canned segment procedures (which are known correct) into minimal code per spec §4.4/§4.5, with step
  markers matching the canned objective.
- Create: `docs/reference/build-file.md` (the build file rules of §4.4/§4.5/§4.6, one line per named check)

**Behaviour:** every named check in spec §4.4 (SQL) and §4.5 (Python), each error a single line naming the check,
the line number and the expectation. Move, don't rewrite: `write_mode_errors`, `_merge_errors`, `_merge_keys`,
`external_access_errors`, `_parse_errors` and the empty-sandbox run (`_fixtures`, `_create_empty`, `_output_errors`)
adapt from `ProcInfo` to `list[Statement]`. The run renders `{{src}}` = `MIGDB.MIG_COMPILE`, `{{tgt}}` =
`MIGDB.MIG_WORK` and applies `session_settings` the way `proc_runner.run_proc` did. Lint (spec §4.6) is computed
for SQL only, reported under `lint`, never changes the exit code. Python: the static rules, then the runtime
sandbox run on empty inputs (existing machinery in `compile_check._check_snowpark` / `lib/snowpark_sandbox.py`).
Delete the procedure/dbt/scaffold branches of `compile_check.py`.

- [ ] Step 1: failing tests: one per named check with a minimal bad file (fixture objective + build text written by
  the test into a tmp workflow); each canned build passes with `ok: true` and no lint flag.
- [ ] Step 2: failing lint tests: a pass-through CTE, `SELECT *` CTE, tool-by-tool chain of 5 CTEs, a legitimate
  filtering CTE that is not flagged; then implement `lib/minimality.py` with sqlglot ASTs.
- [ ] Step 3: implement `compile_check.py`; adapt `snowpark_rules.py`/`write_modes.py` with their tests.
- [ ] Step 4: write the seven canned builds; assert in a test that each canned build has fewer lines than the sum
  of its sample's canned segment procedures (record both numbers in the test's failure message).
- [ ] Step 5: run the new/changed tests; commit.

---

### Task A4: `validate_build.py`, golden data without intermediates, broken builds

**Files:**
- Create: `scripts/validate_build.py`, `tests/test_validate_build.py`
- Modify: `scripts/lib/validation.py` (workflow-level report helpers; drop segment/chain helpers once unused),
  `scripts/dev/alteryx_sim.py` (no `golden/intermediates/`), `scripts/inject_outputs.py` (captures after inputs and at
  final outputs only), `scripts/dev/build_samples.py` (no segmentation step), `scripts/load_golden.py`
  (`load_intermediate` removed), their tests
- Create: `samples/<wf>/broken/` replacing `samples/<wf>/broken_sql/`: whole-file broken builds (one per existing
  broken case, same bug, same `broken.json` scenario metadata adapted: `"file": "build.sql"`)
- Read: `scripts/validate_segment.py`, `scripts/validate_snowpark.py`, `scripts/validate_workflow.py`

**Behaviour:** spec §4.8. CLI `validate_build.py <wf_id> [--set NAME]... [--root .] [--backend duckdb|snowflake]
[--connection NAME] [--sandbox-database DB]`. Deletes its own stale reports first (as `validate_segment.py` did).
Usage errors (no objective, no build file, unknown set, missing golden file) exit 2 with nothing written; a compile
or runtime error in the build is that set's FAIL report (exit 1). The idempotency rule, the `needs_human` rule, the
`keys_not_unique`/`nullability_contract` advisories and the update-insert caveat all carry over (they live in
`compare.py`, called per output through `contract_view`). Python builds run through the Snowpark path moved from
`validate_snowpark.py` (sandbox included). `--backend snowflake` keeps working in shape (unexercised; tests use
fakes as today).

- [ ] Step 1: failing tests: every canned build PASSes every golden set of its sample (wf_0001…wf_0007); each broken
  build FAILs with the diff cluster its `broken.json` names; idempotency failure with a `RANDOM()` build; missing
  output table → FAIL naming it; usage errors exit 2 and write nothing.
- [ ] Step 2: implement; adapt `lib/validation.py`.
- [ ] Step 3: golden without intermediates: update the simulator, `inject_outputs.py`, `build_samples.py`,
  `load_golden.py` and their tests (an `intermediates` directory is never written).
- [ ] Step 4: run the new/changed tests; commit.

---

### Task A5: `render_mapping.py`, `deploy.py`, `gen_source_views.py`

**Files:**
- Create: `scripts/render_mapping.py`, `tests/test_render_mapping.py`
- Rewrite: `scripts/deploy.py`, `tests/test_deploy*.py`
- Modify: `scripts/gen_source_views.py` docstring (its output is the `{{src}}` schema), its tests if they assert text

**Behaviour:**
- `render_mapping.py <wf_id> [--root .]`: spec §4.9. Writes `workflows/<id>/docs/mapping.md` (creates `docs/`).
  Region line ranges come from `step_regions`; each SQL region's label names its statement kind and target or temp
  table (`CREATE TABLE SALES_SUMMARY`, `TEMPORARY TABLE orders`, `MERGE INTO X`, `INSERT INTO X`); Python regions
  are labelled `lines a–b`. Step-to-step edges from `feeds`; step-to-region edges dotted. The table lists every dag
  tool in id order with type, a one-line "what it does" (the digest line's config part, escaped, truncated at 120
  characters with `…`), its step (or `no effect: <reason>`) and the line ranges. The Mermaid block must pass
  `lint_mermaid`. Exit 1 if a step has no region or a tool is unaccounted for (message names it).
- `deploy.py <wf_id> --database DB --schema SCHEMA [--src-schema S] [--stage STAGE] [--connection NAME]
  [--execute] [--render] [--root .]`: dry run by default (prints, connects to nothing). SQL: prints the `PUT` of
  `build.sql` to the stage, the `EXECUTE IMMEDIATE FROM @<stage>/build.sql USING (src => '<DB>.<SRC_SCHEMA>', tgt =>
  '<DB>.<SCHEMA>')` and a commented `CREATE TASK … AS EXECUTE IMMEDIATE FROM …` with the workflow's schedule from
  `manifest.source.server_schedule` when present; `--render` also writes `build.rendered.sql` with the names
  substituted. Python: prints `CREATE OR REPLACE PROCEDURE <SCHEMA>.<WF>_BUILD(SRC STRING, TGT STRING) … LANGUAGE
  PYTHON RUNTIME_VERSION = '3.11' PACKAGES = ('snowflake-snowpark-python') HANDLER = 'main' AS $$ … $$` and the
  `CALL`. Only a `VALIDATED` workflow deploys (exit 1 otherwise). `--execute` runs through `lib/snowflake_conn.py`
  as today (unexercised here; tested with fakes).

- [ ] Step 1: failing tests for render_mapping on every sample (diagram lint clean; table has every tool; a removed
  marker → exit 1 naming the step), then implement.
- [ ] Step 2: failing tests for deploy (dry-run text for wf_0001 SQL and wf_0006 Python; not VALIDATED → exit 1;
  `--render` file content), then implement.
- [ ] Step 3: run the new/changed tests; commit.

---

### Task A6: Remove the segment machinery (Python side) and make `pytest` fully green

**Files:**
- Delete scripts: `segment.py`, `plan_batches.py`, `stitch_analysis.py`, `check_seams.py`, `contract_scaffold.py`,
  `contract_check.py`, `translation_scaffold.py`, `render_snowpark.py`, `target_check.py`, `validate_segment.py`,
  `validate_workflow.py`, `validate_snowpark.py`, `validate_dbt.py`, `lib/proc_runner.py`, `lib/scaffold.py`,
  `lib/dbt_surface.py`, `lib/handoff.py`, `dev/segment_scale.py` — but NOT `lib/dbt_project.py`,
  `scripts/validate_dbt.py`, their tests or the `dbt-*` requirements: Task A7 owns and rewrites them (if their old
  tests fail because of your deletions, leave them for A7 and list them in your report)
- Delete their tests and any test that only exercised them; adapt tests that exercised shared behaviour through them
  (e.g. comparison semantics) to go through `validate_build.py`
- Modify: `scripts/prompt_context.py` (intake role only), `scripts/intake_prompt.py` (no output-target question),
  `scripts/survey_corpus.py` (no `--prefer procedures|dbt`; report SQL vs Python per workflow from `node_class`),
  `scripts/lib/vocab.py` (drop output kinds/targets), `mappings/global.yaml` (drop `program.output_target`),
  `scripts/dev/answer_samples.py`, `scripts/lib/sample_answers.py` (if they reference segments), `samples/*/sample.json`
  (drop `segmentation`), `samples/*/README.md` (describe the new canned files), `samples/*/canned/` (delete
  `segments/`, `analysis.md`, `unsupported.json`; keep `intake/plan.md`, `docs/migration.md` rewritten to the new
  sources), `samples/*/broken_sql/` (deleted; A4 created `broken/`), `requirements.txt` (drop `dbt-*` if present)
- Delete: `cookbook/dbt.md`; `docs/reference/contracts.md`, `docs/reference/large-workflows.md`,
  `docs/reference/output-targets.md` (their still-true content now lives in `objective.md`/`build-file.md`)
- Leave `workflows/` (committed run outputs) alone: B2 regenerates it.

- [ ] Step 1: delete; `grep -rn` the scripts, tests and samples for every deleted module name, `segments/`,
  `seg_0`, `contract.json`, `output_kind`, `output_target`, `dbt` and fix each hit (keep historical mentions only
  in `docs/live-smoke-test.md`, `docs/superpowers/**` records and `docs/spec/**`).
- [ ] Step 2: run the whole `pytest` suite → 0 failures, 0 skipped; commit.

---

### Task A7: dbt export of a validated `build.sql`

**Files:**
- Create: `scripts/export_dbt.py`, `tests/test_export_dbt.py`
- Rewrite: `scripts/validate_dbt.py` (one project per workflow, outputs from the objective), `tests/test_validate_dbt*.py`
- Modify: `scripts/lib/dbt_project.py` (keep `run_dbt`, `dbt_executable`, `local_vars`, `sandbox_path`, the cleaning
  helpers; drop segment/contract-specific helpers and `check_surface`'s use of `lib/dbt_surface.py`), its tests
- Modify: `mappings/global.yaml` (`program.dbt: true`, replacing `program.output_target`), `scripts/deploy.py`
  (`--dbt`: print `dbt build --project-dir workflows/<id>/dbt --profiles-dir <your profiles dir> --target <name>`
  and the `--vars` for `src_schema`/`tgt_schema`; refuse when there is no `dbt/`), `tests/test_deploy.py`
- Modify: `orchestrator/stages.ts` (after `render_mapping.py`: `export_dbt.py <id>` then `validate_dbt.py <id>`;
  exit 1 → translate `NEEDS_HUMAN`, reason `dbt: <first stderr line>`; exit 2 → `script-error`), its tests
- Modify docs: README (deliverables, flow diagram node, commands), `docs/handoff-production.md` (deploying the dbt
  project), `docs/reference/build-file.md` (a "dbt export" section), the documenter agent file (Deployment section
  names the dbt project when `dbt/` exists), and any test that pins "no dbt" wording

**Behaviour:** spec §4.10 exactly. Deterministic output (byte-identical on a re-run). The exporter parses statements
with `lib/build_sql.split_statements` and sqlglot (Snowflake dialect) to find each statement's kind, target and query;
the bodies keep the build's own SQL text (with the substitutions), not a re-generated rendering, so the model reads
like the build file. `validate_dbt.py` exit codes and report shape as `validate_build.py`'s; its sandboxes are
git-ignored as before.

- [ ] Step 1: failing tests: every canned SQL build (wf_0001, 0002, 0003, 0004, 0007) exports, and its project PASSes
  every golden set; the update_insert sample (wf_0003) becomes an incremental merge model with the objective's keys;
  pre/post SQL becomes hooks; a temporary table becomes an ephemeral model; an unsupported statement is exit 1 naming
  its line; `program.dbt: false` and a python workflow are skipped with nothing written; re-export is byte-identical.
- [ ] Step 2: implement the exporter, then the validator (port the old validate_dbt's runner/compare/idempotency code).
- [ ] Step 3: orchestrator call + tests; deploy `--dbt` + tests; docs.
- [ ] Step 4: run the touched tests, `node --test`, `tsc`; commit.

---

### Task B1: Orchestrator — the new stage flow

**Files:** `orchestrator/types.ts`, `stages.ts`, `runner.ts`, `policy.ts` (write lanes only), `agents.ts`,
`models.ts`, `cli.ts`, `hooks.ts`, `manifest.ts`, `external_runner.ts` (role output folders only),
`orchestrator.config.json`, `orchestrator/POLICY.md`; tests `orchestrator/test/*.test.ts` and
`orchestrator/test/fakes.ts`, `orchestrator/test/fixtures/**`.

**Interfaces (consumed):** the Python CLIs and exit codes of A2–A5 exactly as written above; the canned layout
`samples/<wf>/canned/{objective.json, objective.md, build.sql|build.py, translation_notes.md, review.json,
intake/plan.md, docs/migration.md}` and `samples/<wf>/broken/` (`broken.json` + whole-file builds).

**Behaviour:** spec §3 and §5.
- `types.ts`: `Stage` unchanged (`parse, intake, analyze, golden, translate, document, pr`). `Role` drops
  `validator`. Manifest drops `segments`, `segment_status`, `segmentation`, `output_kind`, `output_target`; gains
  `language?: "sql" | "python"`. `TERMINAL_GOOD.analyze = ["DONE"]`; `WAITING_FOR_ANSWERS` is a non-terminal,
  non-escalated analyze status (it re-runs the stage).
- `stageIntake`: `WAITING_FOR_ANSWERS` returns `"continue"` (no issue, no log of questions here); everything else
  unchanged.
- `stageAnalyze`: (a) if `status.analyze === "WAITING_FOR_ANSWERS"`: `objective_questions.py --write` (refresh
  answers) then `--status`; open → questions gate; all answered → analyzer revision session (task: apply the
  answers in `workflows/<id>/intake/objective_questions.md` to `objective.json`/`objective.md`, moving each to
  `answered`; same verify as (b)) → (c). (b) fresh: `digest.py`, `objective_scaffold.py --prefill`, analyzer
  session (instructions: write the objective per `.github/agents/analyzer.agent.md`; the digest inline when it fits
  `PROMPT_CONTEXT_CHARS`×4, else "read workflows/<id>/digest.md"; notes instruction), verify = both files exist,
  `objective_scaffold.py --apply` exit 0, `objective_check.py` exit 0 (on failure, the checker's stderr becomes the
  recorded reason and the retry note, as `contractReason` did). (c) tier/language from `objective.json` into the
  manifest; T3 → translate `MANUAL`, stop. (d) `objective_questions.py --write`; then the **questions gate**: if
  intake is `WAITING_FOR_ANSWERS` or `--status` is 1 → one notice (GitHub issue when enabled with both files'
  contents as the body — write a combined body file under `workflows/<id>/intake/`; else one log line naming both
  files), `status.analyze = "WAITING_FOR_ANSWERS"` when its own questions are open else `"DONE"`, stop. Otherwise
  `DONE`, continue.
- `stageGolden`: first `objective_scaffold.py --apply` and `objective_check.py --ready`; failure → analyze
  `NEEDS_HUMAN` with the checker's first line as reason, stop. Then as today.
- `stageTranslate`: one loop of at most `maxFixIterations` over the workflow (port `migrateSegment` without
  segments): translator (iteration 0, task: write `build.sql`, or `build.py` when `objective.json.language` is
  `python`, from the objective; may run compile_check and validate_build once) or fixer (reads `validation.json`,
  `review.json`, `compile_check.json`; the pre-review diagnosis sentence when the last iteration failed before
  review); verify = the build file exists, plus `freshSince` content snapshot of `build.sql`/`build.py`;
  `compile_check.py <id>` (exit 1 → next iteration with its diagnosis; 2 → park script-error); reviewer →
  `review.json` (`BLOCK` → next iteration); clear stale reports, run `validate_build.py <id>` **as a script**
  (`env.py`), exit 2 → park script-error; `needs_human` → park; PASS* → `render_mapping.py <id>` (exit ≠ 0 → park
  `mapping`) → `VALIDATED`. Remove `migrateSegment`, `migrateDbt`, `translateDbt`, `chainCheck`, batches, seams,
  `masterSql`, `dbtReadme`, `dbtModelName`, skeleton helpers (`writeSkeleton`, `pendingSkeleton`, `untouched`,
  `holdsTodo`, `SCAFFOLD_TODO`) — keep `snapshot`/`sameContent` for `freshSince`.
- `stageDocument`: one task per spec §3 (sources: `objective.md`, `translation_notes.md`, `validation.json`,
  `docs/mapping.md` linked, not rewritten; Deployment section names `build.sql`/`build.py` and `scripts/deploy.py`).
- `stagePr`: title `migrate(<id>): <tier>, <language>`.
- `runner.ts` MockRunner: analyzer copies `canned/objective.json` + `objective.md`; translator copies
  `canned/build.*` + `translation_notes.md`, or for a `fix-loop` scenario the `broken/` build first then the canned
  build for the fixer; reviewer copies `canned/review.json`; documenter `canned/docs/migration.md`; no validator.
  CopilotRunner/ExternalRunner: role output folders (`notes/`, `docs/`, `intake/`) pre-created; nothing
  segment-scoped.
- `policy.ts`: write lanes per spec §5 (analyzer: `objective.json`, `objective.md`, `notes/analyzer.md`;
  translator/fixer: `build.sql`, `build.py`, `translation_notes.md`, `fix_log.md`, `notes/fixer.md`; reviewer:
  `review.json`; documenter: `docs/migration.md` only — never `docs/mapping.md`); remove segment, batch and dbt
  lanes and the `AnalyzerBatch` context. Shell allow-list: translator/fixer may run `python scripts/compile_check.py
  <id>` and `python scripts/validate_build.py <id>`; analyzer `python scripts/objective_check.py <id>`. Leave the SQL
  tool policy alone unless a test depends on a removed type.
- `stages.ts` `SESSION_RULE_LINES` rules 5 and 6: "contract" → "objective" (spec §5); keep 7 rules.
- `cli.ts`: remove dbt/output-target flags; `--runner external` unchanged.
- Tests: port every stage test that still applies; delete segment/batch/seam/chain/dbt/scaffold tests; add tests for
  the questions gate (intake waiting + no objective questions → one notice, analyze DONE; objective questions open →
  analyze WAITING; resumed with answers → revision session then DONE), the golden `--ready` gate, the translate loop
  (compile fail → fixer; review BLOCK → fixer; validation FAIL → fixer; needs_human → park; PASS → mapping →
  VALIDATED; mapping failure → park), and policy lanes (allowed and refused writes per role).

- [ ] Step 1: types + manifest + policy lanes with their tests.
- [ ] Step 2: stages (analyze, gate, golden, translate, document, pr) with fakes-based tests.
- [ ] Step 3: runners (MockRunner canned layout) with tests; cli.
- [ ] Step 4: `node --test` (all but `integration.test.ts`, which B2 updates) and `tsc --noEmit` green; commit.

---

### Task B2: Integration test and regenerated `workflows/` (after A6 and B1 are merged)

**Files:** `orchestrator/test/integration.test.ts`, `workflows/**` (committed demo run outputs).

- [ ] Step 1: rewrite the integration test: seed wf_0001, run 1 stops at the questions gate (intake waiting), answer
  with `answer_samples.py`, run 2 with scenario `fix-loop` ends `VALIDATED` after one fixer round with
  `docs/mapping.md` written, run 3 is a no-op. Make it pass.
- [ ] Step 2: regenerate `workflows/` by seeding all seven samples and running the orchestrator with the MockRunner
  to `document`; delete every stale `segments/`, `procs/`, `dbt/`, `analysis.md`, `unsupported.json`,
  `validation_workflow*.json`, `golden/intermediates/`; commit the result.
- [ ] Step 3: full suites: `pytest`, `node --test`, `tsc --noEmit` green; commit.

---

### Task B3: Agent files, cookbook, docs, README diagrams

**Files:** `.github/agents/{analyzer,translator,reviewer,fixer,documenter,intake}.agent.md` (delete
`validator.agent.md`), `.github/copilot-instructions.md`, `cookbook/index.md`, `cookbook/snowpark.md`, `README.md`,
`docs/handoff-production.md`, `docs/handoff-copilot-models.md`, `docs/production-backlog.md`, `docs/reference/*.md`
that still mention segments; tests that lint agent files or docs (`tests/test_agent_files*.py`,
`tests/test_readme_diagrams.py`, `tests/test_committed_*.py`, etc.).

- [ ] Step 1: rewrite the agent files per spec §5 (translator: keep every Alteryx-behaviour rule, drop every
  procedure/skeleton/CTE-per-tool rule; add "minimal code", the lint-justification line format, step markers, the
  `{{src}}`/`{{tgt}}` rules, the self-test allowance). `copilot-instructions.md` mirrors the session rules.
- [ ] Step 2: cookbook: `index.md` says patterns are expression references, not structure; `snowpark.md` for a
  whole-workflow `build.py`.
- [ ] Step 3: README: first paragraph, "Where it stands", the flow, the components and sequence diagrams (objective,
  one file, mapping, no segments/dbt/validator), the artifacts table, the commands; every Mermaid block lint-clean
  (`tests/test_readme_diagrams.py`) and render-checked in Mermaid 11. Handoff docs: replace segment/dbt/master-proc
  instructions with build-file deployment (`deploy.py`, `EXECUTE IMMEDIATE FROM`, the `--render` fallback).
- [ ] Step 4: `pytest`, `node --test`, `tsc --noEmit` green; commit.

---

### Task C1: Live verification (controller)

- [ ] Run wf_0001, wf_0006 and wf_0007 end to end with Sonnet stand-ins (`--runner external`), then with the local
  model if time allows; fix what breaks; record the "Fifth live test" in `docs/live-smoke-test.md` with the line
  counts of each build against the old segment procedures.
- [ ] Final whole-branch review; fix; merge `objective-first` into `main` locally; ask the owner for the new GitHub
  project's name before any push.
