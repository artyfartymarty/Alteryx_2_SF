"""The eight Copilot custom-agent definitions, `.github/copilot-instructions.md` and `config.json`.

The agent files were written for the objective-first pipeline (docs/superpowers/specs/2026-10-06-objective-first-
design.md §5): the analyzer writes an objective, the translator writes ONE minimal build file from it, the
reviewer and the fixer work on that file, and the validator agent is retired -- `scripts/validate_build.py`
is run by the orchestrator, as code. These tests check the frontmatter contract every file must satisfy and the
statements each role has to carry, so a rewrite cannot quietly drop a rule: above all every rule about Alteryx
behaviour the translator keeps, and every name the Snowpark gate refuses.
"""
import getpass
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / ".github" / "agents"
SUFFIX = ".agent.md"

#: An absolute path of *this* PC -- never legitimate in a hand-off artifact.
_ABS_PATH_RE = re.compile(r"[a-zA-Z]:[\\/]Users[\\/]", re.IGNORECASE)

# The eight custom agents (the validator is retired) plus the five built-ins config.json must keep untouched
# (program spec 01-copilot-setup.md Part A §1).
CUSTOM_AGENTS = [
    "intake", "analyzer", "translator", "reviewer",
    "fixer", "parser-recovery", "documenter", "cookbook-curator",
]
BUILTIN_AGENTS = ["task", "explore", "research", "rubber-duck", "general-purpose"]

#: Marks a paragraph rewritten for the objective-first pipeline, the way each earlier phase marked its own.
MARKER = "<!-- amended: objective-first -->"
OBJECTIVE_AGENTS = ("analyzer", "translator", "reviewer", "fixer", "documenter")

#: Words that belonged to the segment, procedure and dbt design: no agent file teaches them any more.
RETIRED_WORDS = (
    "segments/", "seg_NN", "contract.json", "proc.sql", "proc.py", "TODO(scaffold)", "translation_scaffold",
    "contract_scaffold", "contract_check", "check_seams", "plan_batches", "stitch_analysis", "target_check",
    "render_snowpark", "validate_segment", "validate_snowpark", "validate_dbt", "validate_workflow",
    "output_kind", "IDENTIFIER(", "LET <LOGICAL>", "EXECUTE AS CALLER", "procs/master.sql", "analysis.md",
    "unsupported.json", "cookbook/dbt.md", "profiles.yml",
)


def _agent_files():
    return sorted(AGENTS_DIR.glob(f"*{SUFFIX}"))


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter, body) for a `---`-fenced Markdown file. Fails loudly if the fence is malformed.

    Parsed as flat `key: value` lines (split on the first ": "), not full YAML: several descriptions contain a
    colon inside the value itself (documenter's does), which a strict YAML parser rejects as an ambiguous
    nested mapping. The CLI's own frontmatter is documented as three flat string keys (program spec
    01-copilot-setup.md Part A §1), so a flat parse is the faithful model of what it loads, not a workaround.
    """
    assert text.startswith("---\n"), "file must open with a `---` frontmatter fence"
    end = text.index("\n---\n", 4)
    front: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(": ")
        assert sep, f"malformed frontmatter line (expected 'key: value'): {line!r}"
        front[key.strip()] = value.strip()
    body = text[end + 5:]
    return front, body


def _read(name: str) -> tuple[dict, str]:
    return _split_frontmatter((AGENTS_DIR / f"{name}{SUFFIX}").read_text(encoding="utf-8"))


def _squeezed(name: str) -> str:
    _, body = _read(name)
    return " ".join(body.split())


# --- file inventory -----------------------------------------------------------------------

def test_eight_agent_files_exist_and_nothing_else():
    found = {p.name[: -len(SUFFIX)] for p in _agent_files()}
    assert found == set(CUSTOM_AGENTS)


def test_the_validator_agent_is_retired():
    assert not (AGENTS_DIR / f"validator{SUFFIX}").exists()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert "validator" not in config["subagents"]["agents"]


# --- frontmatter contract: exactly name, description, model; name equals the file stem -----

def test_every_agent_has_exactly_name_description_model_frontmatter():
    for path in _agent_files():
        front, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
        assert set(front.keys()) == {"name", "description", "model"}, path.name
        assert isinstance(front["name"], str) and isinstance(front["description"], str)
        assert isinstance(front["model"], str)


def test_agent_name_equals_file_stem():
    for path in _agent_files():
        stem = path.name[: -len(SUFFIX)]
        front, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
        assert front["name"] == stem, path.name


def test_intake_frontmatter_dropped_the_tools_comment():
    text = (AGENTS_DIR / f"intake{SUFFIX}").read_text(encoding="utf-8")
    front_block = text.split("\n---\n", 1)[0]
    assert "tools" not in front_block.lower()


def test_every_agent_file_uses_lf_line_endings():
    for path in _agent_files() + [ROOT / ".github" / "copilot-instructions.md"]:
        assert b"\r" not in path.read_bytes(), path.name


# --- intake: unchanged by the objective-first work ------------------------------------------

def test_intake_amendments():
    _, body = _read("intake")
    assert "scripts/intake_touchpoints.py" in body
    assert "intake/touchpoints.json" in body
    assert "ask_user" in body
    assert "scripts/intake_prompt.py" in body and "--no-interactive" in body
    assert "logical" in body and "C6" in body
    # step 1 no longer hand-enumerates touchpoints
    assert "Enumerate every external touchpoint in dag.json" not in body


def test_intake_no_longer_plans_segments():
    _, body = _read("intake")
    assert "segment" not in body.lower()
    assert "EXECUTE AS" not in body


# --- no agent file teaches the retired design -----------------------------------------------

def test_no_agent_file_teaches_the_segment_procedure_or_dbt_design():
    offenders = []
    for path in _agent_files() + [ROOT / ".github" / "copilot-instructions.md"]:
        text = path.read_text(encoding="utf-8")
        offenders += [f"{path.name}: {word}" for word in RETIRED_WORDS if word in text]
        # dbt as something an agent writes or runs: the word alone appears in no agent file but the documenter's,
        # which names the project a script exports (`test_documenter_names_the_dbt_project_a_script_exports`)
        if path.name == f"translator{SUFFIX}":
            # the one place the translator meets dbt (final review I2): the `dbt:form` check, which names the shapes
            # the script that exports the project cannot convert; it writes and runs no dbt project
            bullet = re.search(r"- \*\*A shape the dbt export can convert\.\*\*.*?(?=\n- \*\*)", text, flags=re.DOTALL)
            assert bullet, "the translator's dbt:form bullet is missing"
            text = text.replace(bullet.group(0), "")
        if path.name != f"documenter{SUFFIX}":
            assert "dbt" not in text.lower(), f"{path.name} still mentions dbt"
    assert not offenders, offenders


def test_amended_paragraphs_are_marked():
    """Every agent the objective-first rewrite touched carries its marker; the two it did not touch
    (parser-recovery and cookbook-curator, bar one stale word each) carry none."""
    for name in CUSTOM_AGENTS:
        _, body = _read(name)
        if name in OBJECTIVE_AGENTS:
            assert MARKER in body, name
        else:
            assert MARKER not in body, name


# --- analyzer ---------------------------------------------------------------------------------

def test_analyzer_writes_the_objective_from_the_digest():
    squeezed = _squeezed("analyzer")
    for token in ("workflows/<id>/objective.json", "workflows/<id>/objective.md", "workflows/<id>/digest.md",
                  "docs/reference/objective.md", "workflows/<id>/notes/analyzer.md",
                  "workflows/<id>/parsed/dag.json", "workflows/<id>/intake/mappings.yaml",
                  "`python scripts/objective_check.py <id>`", "scripts/objective_scaffold.py"):
        assert token in squeezed, token
    assert "Never write SQL" in squeezed
    assert "the durable record stays in the objective and the files you write, never the notes" in squeezed


def test_analyzer_names_the_mechanical_fields_the_orchestrator_rewrites():
    squeezed = _squeezed("analyzer")
    assert "MECHANICAL" in squeezed
    for token in ("`workflow`", "`tool_id`", "`logical`", "`write_mode`", "`pre_sql`", "`post_sql`",
                  "`name` and `type`", "`keys` when the write mode is `update_insert`"):
        assert token in squeezed, token


def test_analyzer_names_every_judgment_field_of_the_objective():
    squeezed = _squeezed("analyzer")
    for token in ("`title`", "`purpose`", "`tier`", "`language`", "`language_reason`", "`nullable`", "`grain`",
                  "`meaning`", "`row_relation`", "`ordering`", "`tolerances`", "`normalizations`", "`steps`",
                  "`no_effect`", "`rules`", "`parity_risks`", "`questions`", "`answered`",
                  '"alteryx_deterministic"', '"order_dependent_columns"'):
        assert token in squeezed, token


def test_analyzer_states_the_step_and_question_rules():
    squeezed = _squeezed("analyzer")
    # every tool id exactly once, macro internals excluded, outputs inside a step
    assert "Every tool id of the DAG is in exactly one step's `tools` or in `no_effect`" in squeezed.replace("**", "")
    assert "macro" in squeezed and "out:<output tool id>" in squeezed
    # unsure points: ids, the assumption, and the revision session
    assert '"id": "O1"' in squeezed and "assumption" in squeezed
    assert "a revision session" in squeezed
    assert "move each answered question from `questions` to `answered`" in squeezed
    # the vocabularies compare.py reads
    for diff_class in ("ROUNDING", "ORDERING", "NULL_SEMANTICS", "TRUNCATION", "TYPE", "LOGIC"):
        assert diff_class in squeezed, diff_class


def test_analyzer_states_language_and_tier():
    squeezed = _squeezed("analyzer")
    for token in ("`language_reason`", "`snowpark`", "`T1`", "`T2`", "`T3`", "`manual`"):
        assert token in squeezed, token
    assert "`python`" in squeezed and "`sql`" in squeezed


def test_analyzer_writes_the_fixed_objective_md_headings():
    _, body = _read("analyzer")
    for heading in ("# <id> — <title>", "## Purpose", "## Inputs", "## Outputs", "## Steps",
                    "## Business rules", "## Alteryx behaviour to keep", "## Questions"):
        assert f"`{heading}`" in body, heading


# --- translator: every Alteryx-behaviour rule is kept, every structure rule is replaced ----------

def test_translator_writes_one_minimal_build_file_from_the_objective():
    squeezed = _squeezed("translator")
    for token in ("build.sql", "build.py", "objective.json", "digest.md", "translation_notes.md",
                  "One write statement per output", "removes repetition", "Never one CTE per Alteryx tool",
                  "never `build.py` on your own judgment"):
        assert token in squeezed, token


def test_translator_states_the_placeholder_rules():
    squeezed = _squeezed("translator")
    for token in ("`{{src}}` and `{{tgt}}`", "`{{src}}.<input logical>`", "`{{tgt}}.<output logical>`",
                  "`.<LOGICAL>`", "TEMPORARY TABLE", "unqualified names"):
        assert token in squeezed, token


def test_translator_names_every_check_compile_check_runs_on_a_build_sql():
    """The check names of design §4.4, each in code ticks, so a compile error can be read against the file."""
    squeezed = _squeezed("translator")
    names = ("template:placeholder", "tables:source", "tables:target", "tables:temporary", "session:form",
             "statement:kind", "external_access", "parse", "write_mode", "write_mode:pre_post", "steps:missing",
             "steps:unknown", "steps:marker", "run", "outputs:columns", "dbt:form", "language")
    missing = [name for name in names if f"`{name}`" not in squeezed]
    assert not missing, missing


def test_translator_states_what_compile_check_enforces_beyond_the_names():
    """A3 review fix round 1: a table named through the IDENTIFIER function is refused, an INSERT / MERGE-insert names
    the objective's columns, and PreSQL / PostSQL / the truncate sit immediately next to the write."""
    squeezed = _squeezed("translator")
    for token in ("A table named through the `IDENTIFIER` function", "is refused under the same two checks",
                  "no other statement of the file may sit between a PreSQL statement and the write",
                  "exactly the objective output's columns in its order",
                  "an `INSERT` with no column list"):
        assert token in squeezed, token


def test_translator_says_which_shapes_the_dbt_export_cannot_convert():
    """Final review I2: `compile_check.py` reports `dbt:form`, so the translator is told what to avoid."""
    squeezed = _squeezed("translator")
    for token in ("A shape the dbt export can convert", "`program.dbt: true`", "names a statement it cannot convert as `dbt:form`",
                  "`WHEN MATCHED AND ...`", "`INSERT ... VALUES`", "`INSERT INTO ... (<columns>) WITH ... SELECT ...`"):
        assert token in squeezed, token
    assert "the suspect step" not in squeezed, "no report carries a suspect step"


def test_translator_states_the_step_marker_rules():
    squeezed = _squeezed("translator")
    for token in ("`-- step s1`", "`-- step s1, s3`", "`# step s1`", "Every step of the objective has at least one region"):
        assert token in squeezed.replace("**", ""), token


def test_translator_states_the_lint_justification_format():
    squeezed = _squeezed("translator")
    assert "`lint <kind> <name>: kept because <the reason>`" in squeezed
    for kind in ("pass_through_cte", "tool_by_tool", "single_use_chain"):
        assert f"`{kind}`" in squeezed, kind
    assert "compile_check.json" in squeezed


def test_translator_may_test_its_own_work_once():
    squeezed = _squeezed("translator")
    assert "`python scripts/compile_check.py <id>`" in squeezed
    assert "`python scripts/validate_build.py <id>`" in squeezed
    assert "at most ONCE" in squeezed
    assert "exits 0" in squeezed and "Exit 2 means the script itself could not run" in squeezed
    assert "stop and report" in squeezed
    assert "cleared before it runs" in squeezed


#: The self-test allowance (ruling R10): the compile check as often as needed until it exits 0, the validation
#: once. `orchestrator/stages.ts`'s translator and fixer task texts say the same words, and
#: `orchestrator/test/translate.test.ts` pins them there.
SELF_TEST = ("run python scripts/compile_check.py <id> as often as you need until it exits 0; "
             "run python scripts/validate_build.py <id> once")


def test_translator_and_fixer_may_run_compile_check_as_needed_and_validate_build_once():
    for name in ("translator", "fixer"):
        flat = _squeezed(name).replace("`", "")
        assert SELF_TEST in flat, name
        assert "at most ONCE" in flat, name
        assert "once each" not in flat, name
    stages = (ROOT / "orchestrator" / "stages.ts").read_text(encoding="utf-8")
    assert "as often as you need until it exits 0" in stages and "once each" not in stages


def test_a_build_py_must_not_hold_the_procedure_body_delimiter():
    """`deploy.py` wraps the module in a `$$ … $$` procedure body, which a `$$` inside it would end."""
    for name in ("translator", "fixer", "reviewer"):
        assert "`$$`" in _squeezed(name), name
    assert "must not contain `$$`" in _squeezed("translator")


def test_the_reviewer_names_no_field_the_objective_does_not_have():
    assert "calls large" not in _squeezed("reviewer")
    assert "expected_rows" not in _squeezed("reviewer")


def test_translator_keeps_every_alteryx_behaviour_rule():
    squeezed = _squeezed("translator")
    for token in (
        "rows evaluating to NULL go to the False branch",                        # Filter
        "`TRY_TO_NUMBER` / `TRY_TO_DATE`", "where Alteryx would warn and null",   # warn-and-null
        "`LEFT(x, n)`", "`String(n)`",                                            # fixed-width strings
        "Money arithmetic is done in NUMBER, not FLOAT", "`ROUND(1.005::FLOAT, 2)` gives `1.00`",
        "gives `1.01`",                                                           # the worked example
        "Order-dependent tools (Sample, Record ID, Unique, Running Total, Multi-Row Formula, Tile)",
        "explicit `ORDER BY`", "`PIVOT` / `UNPIVOT`",                             # order and Cross Tab
        "emit only the L/J/R outputs that something downstream uses",             # Join
        "TIMEZONE, WEEK_START",                                                   # the session parameters
    ):
        assert token in squeezed, token


def test_translator_states_the_write_mode_forms_against_the_target_placeholder():
    squeezed = _squeezed("translator")
    for token in (
        "`CREATE OR REPLACE TABLE {{tgt}}.<LOGICAL> AS ...`", "never TRUNCATE + INSERT",
        "`INSERT INTO {{tgt}}.<LOGICAL> (<columns>) SELECT ...`", "`truncate_append`",
        "`TRUNCATE TABLE {{tgt}}.<LOGICAL>`", "`update_insert` -> one `MERGE INTO {{tgt}}.<LOGICAL>",
        "on exactly the objective's keys", "`WHEN MATCHED THEN UPDATE", "`WHEN NOT MATCHED THEN INSERT",
        "PreSQL and PostSQL", "immediately before and after that write",
    ):
        assert token in squeezed, token


def test_translator_forbids_what_a_build_file_may_not_hold():
    squeezed = _squeezed("translator")
    for token in ("no procedure, `CALL`, `EXECUTE IMMEDIATE`, scripting block, `LET` or `DECLARE`",
                  "`COPY`", "`PUT`", "`GET`", "external functions"):
        assert token in squeezed, token


def test_translator_carries_the_snowpark_rules_for_a_whole_workflow_build_py():
    squeezed = _squeezed("translator")
    for token in (
        "def main(session, src: str, tgt: str) -> str", "snowflake.snowpark.functions", "snowflake.snowpark.types",
        "session.sql", "__import__", "subprocess", 'session.table(f"{src}.<LOGICAL>")',
        '.write.mode("overwrite" | "append").save_as_table', "# step s1", "to_pandas()", "cookbook/snowpark.md",
        "handler `main`",
    ):
        assert token in squeezed, token


# --- every name the Snowpark gate refuses is written down, in code ticks, where a translator reads --------

COLUMN_ORDER_SENTENCE = (
    "`StructType` must list the columns in the objective's declared `outputs[].columns` order: a "
    "different order is a schema FAIL, reported as a `TYPE` difference rather than as an ordering "
    "one"
)

#: Where the column-order rule has to be stated: the two roles that write or judge a Snowpark build, and the
#: cookbook page of its idioms. A Snowpark module hand-builds its output `StructType`, so getting the column
#: ORDER wrong is an easy slip, and `compare.py` reports it as a `TYPE` difference naming every column --
#: which points a fixer at types rather than at order.
COLUMN_ORDER_HOMES = (
    ".github/agents/translator.agent.md",
    ".github/agents/reviewer.agent.md",
    "cookbook/snowpark.md",
)


def _flat(rel: str) -> str:
    return " ".join((ROOT / rel).read_text(encoding="utf-8").split())


def test_the_column_order_rule_is_stated_wherever_a_snowpark_build_is_described():
    wanted = " ".join(COLUMN_ORDER_SENTENCE.split())
    for rel in COLUMN_ORDER_HOMES:
        assert wanted in _flat(rel), f"{rel} does not state the column-order rule"


def _refused_names() -> list[str]:
    from lib import snowpark_rules as rules
    return sorted(rules.FORBIDDEN_NAMES | rules.FORBIDDEN_MODULES | rules.RAW_SQL_NAMES
                  | rules.FORBIDDEN_SINKS | rules.PANDAS_WRITERS | rules.CALL_ONLY_ATTRS
                  | rules.FORBIDDEN_SESSION_NAMES | rules.NO_SESSION_SQL_ATTRS
                  | rules.FORBIDDEN_SESSION_ATTRS | rules.NUMPY_WRITERS)


def _names_missing_from(text: str) -> list[str]:
    """A name counts as written down only when it appears in code ticks -- `save`, not the `save` inside
    `save_as_table`. `sql` and `call` are written as `session.sql` / `session.call`, which is how a reader
    meets them."""
    squeezed = " ".join(text.split())
    return [name for name in _refused_names()
            if f"`{name}`" not in squeezed and f"`session.{name}`" not in squeezed]


def test_the_translator_names_every_refusal_the_snowpark_rules_implement():
    _, body = _read("translator")
    missing = _names_missing_from(body)
    assert not missing, f"the translator's build.py rules do not mention: {missing}"


# --- reviewer -----------------------------------------------------------------------------------

def test_reviewer_writes_review_json_in_the_documented_shape():
    squeezed = _squeezed("reviewer")
    assert "workflows/<id>/review.json" in squeezed
    assert '"verdict": "PASS" | "BLOCK"' in squeezed
    assert '"severity": "blocker" | "note"' in squeezed
    assert '"where": "build.sql:14"' in squeezed
    assert "Never the golden data" in squeezed


def test_reviewer_blocks_on_the_objective_checks_of_the_design():
    squeezed = _squeezed("reviewer")
    for token in (
        "business rule", "`parity_risks`", "exactly once, to `{{tgt}}.<logical>`", "in its `write_mode`",
        "no `SELECT *` into an output", "every step of the objective has a region",
        "`pass_through_cte`", "`tool_by_tool`", "`single_use_chain`",
        "`lint <kind> <name>: kept because <reason>`", "an unresolved flag is blocking",
        "`{{src}}.<logical>`", "PreSQL/PostSQL", "explicit `ORDER BY`", "`TRY_` casts", "`LEFT()`",
    ):
        assert token in squeezed.replace("**", ""), token


def test_reviewer_makes_an_unjustified_build_py_a_note_for_a_human_not_a_blocker():
    """Final review minor 4: the language belongs to the objective, and no fixer can change it."""
    _, body = _read("reviewer")
    blocking, notes = (" ".join(part.split()) for part in body.split("## Notes (advisory, never blocking)"))
    assert "`language` is `python` only with a `language_reason` that holds" in notes
    assert "a note for a human" in notes and "no fixer can change it" in notes
    assert "`language_reason`" not in blocking and "expresses well" not in blocking
    assert "a `build.sql` for a `python` objective" in blocking


def test_reviewer_checks_a_build_py():
    squeezed = _squeezed("reviewer")
    for token in ("`main(session, src, tgt) -> str`", 'session.table(f"{src}.<logical>")', "`session.sql`",
                  "# step sN", "to_pandas()", "translation_notes.md"):
        assert token in squeezed, token


def test_reviewer_documents_money_arithmetic_advisory_check():
    _, body = _read("reviewer")
    assert "1.005" in body
    assert "NUMBER" in body


# --- fixer ---------------------------------------------------------------------------------------

def test_fixer_repairs_the_whole_file_with_the_smallest_change():
    squeezed = _squeezed("fixer")
    for token in ("validation.json", "review.json", "compile_check.json", "objective.json",
                  "workflows/<id>/notes/fixer.md", "fix_log.md", "cookbook_candidate: yes",
                  "no whole-file rewrites; never change tolerances; never touch golden/ or cookbook/.",
                  "`-- step sN`", "smallest change", "NEEDS_HUMAN",
                  "`python scripts/compile_check.py <id>`", "`python scripts/validate_build.py <id>`",
                  "at most ONCE", "lint <kind> <name>: kept because"):
        assert token in squeezed, token
    assert "the durable record stays in the objective and the files you write, never the notes" in squeezed


def test_fixer_keeps_the_build_py_rules():
    squeezed = _squeezed("fixer")
    for token in ("`main(session, src, tgt) -> str`", "ALLOW-list", "`.eval`", "`pd.isna`"):
        assert token in squeezed, token


# --- documenter ----------------------------------------------------------------------------------

def test_documenter_links_the_generated_mapping_and_never_rewrites_it():
    squeezed = _squeezed("documenter")
    assert "docs/mapping.md" in squeezed
    assert "link it from the Steps section and never rewrite it" in squeezed
    assert "workflows/<id>/docs/migration.md" in squeezed
    assert "never add claims about behavior that no artifact supports" in squeezed
    for source in ("objective.md", "translation_notes.md", "validation.json"):
        assert source in squeezed, source


def test_documenter_requires_a_deployment_section_for_the_one_build_file():
    squeezed = _squeezed("documenter")
    for token in (
        "## Deployment", "never deployed from this session", "has run against Snowflake",
        "`scripts/deploy.py <id> --database <DB> --schema <SCHEMA>`",
        "EXECUTE IMMEDIATE FROM @<stage>/build.sql USING (src => '<DB>.<SRC_SCHEMA>', tgt => '<DB>.<SCHEMA>')",
        "`--render`", "`build.rendered.sql`", "scripts/gen_source_views.py", "`LANGUAGE PYTHON`", "HANDLER = 'main'",
        "RUNTIME_VERSION", "PACKAGES", "MIGRATION_CI", "`VALIDATED`",
    ):
        assert token in squeezed, token


def test_documenter_names_the_dbt_project_a_script_exports():
    """Design D9: a SQL workflow's `dbt/` is converted from its build file and checked by scripts. The documenter
    names it and what has been proved about it, writes none of it and runs none of it."""
    squeezed = _squeezed("documenter")
    for token in ("`workflows/<id>/dbt/`", "`scripts/export_dbt.py`", "`dbt/validation.json`",
                  "Both are scripts' work, never yours", "--dbt` prints, and only prints, the one `dbt build` command",
                  "run only on a local DuckDB double", "never on Snowflake", "`program.dbt: false`",
                  "say nothing about dbt for it", "dbt/validation.<set>.json"):
        assert token in squeezed, token
    for old_path in ("dbt/models", "dbt/translation_notes.md", "dbt/compile_check.json", "--target dbt"):
        assert old_path not in squeezed, old_path


def test_documenter_runs_no_script_because_the_policy_lets_it_run_none():
    """Final review I3: `ROLE_SCRIPTS.documenter` is empty, so the file must not say to run the scripts it names."""
    squeezed = _squeezed("documenter")
    assert "you run no script" in squeezed and "the policy refuses every script call from the documenter" in squeezed
    assert "run every script this file names" not in squeezed
    assert "`docs/reference/`" in squeezed and "`snowflake-backend.md`" in squeezed
    assert "you run no script, `scripts/deploy.py` included" in squeezed


# --- copilot-instructions.md ----------------------------------------------------------------------

def test_copilot_instructions_describe_the_objective_first_pipeline():
    text = " ".join((ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8").split())
    for token in ("objective-first", "docs/reference/objective.md", "docs/reference/build-file.md",
                  "workflows/<id>/build.sql", "`{{src}}.<LOGICAL>`", "`{{tgt}}.<LOGICAL>`", "`-- step s1`",
                  "`lint <kind> <name>: kept because <reason>`", "scripts/validate_build.py",
                  "scripts/render_mapping.py", "There is no validator agent"):
        assert token in text, token


def test_copilot_instructions_carry_the_seven_session_rules_with_the_objective():
    text = (ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
    rules = re.findall(r"^\s+(\d)\. ", text, flags=re.MULTILINE)
    assert rules == ["1", "2", "3", "4", "5", "6", "7"], rules
    flat = " ".join(text.split())
    assert "the validation report, the objective, the cookbook and docs/reference/ are the evidence" in flat
    assert "the objective describes every column" in flat
    assert "contract" not in text.lower()


def test_no_machine_path_or_user_name_in_any_agent_or_instruction_file():
    """Hand-off rule: no absolute path of this PC, no OS login name, anywhere in .github/."""
    login = getpass.getuser().lower()
    files = sorted(AGENTS_DIR.glob(f"*{SUFFIX}")) + [ROOT / ".github" / "copilot-instructions.md"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not _ABS_PATH_RE.search(text), f"{path.name} carries an absolute machine path"
        assert login not in text.lower(), f"{path.name} carries the OS login name"


# --- unamended agents keep their spec bodies --------------------------------------------------------

def test_unamended_agents_keep_their_spec_bodies():
    _, parser_recovery = _read("parser-recovery")
    assert "At most 2 attempts per workflow" in parser_recovery

    _, cookbook_curator = _read("cookbook-curator")
    assert "never edit cookbook/*.md directly; proposals only." in cookbook_curator
    assert "workflows/*/fix_log.md" in cookbook_curator


# --- config.json ------------------------------------------------------------------------------

def test_config_json_parses():
    obj = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert isinstance(obj, dict)


def test_config_json_concurrency_and_depth():
    obj = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert obj["subagents"]["maxConcurrency"] == 4
    assert obj["subagents"]["maxDepth"] == 2


def test_config_json_lists_all_eight_custom_agents_plus_five_builtins():
    obj = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    agents = obj["subagents"]["agents"]
    assert set(agents.keys()) == set(CUSTOM_AGENTS) | set(BUILTIN_AGENTS)


def test_config_json_agent_models_match_the_agent_frontmatter():
    obj = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    agents = obj["subagents"]["agents"]
    for name in CUSTOM_AGENTS:
        front, _ = _read(name)
        assert agents[name]["model"] == front["model"], name


# --- the notes file of the three roles that keep one ----------------------------------------------------------

def test_intake_analyzer_and_fixer_keep_notes():
    for name in ("intake", "analyzer", "fixer"):
        squeezed = _squeezed(name)
        assert f"notes/{name}.md" in squeezed, name
        assert "the durable record stays in" in squeezed, name


# --- scripts run as `python scripts/…` from any run root (live hardening L1) -----------------------------------
# A run root is a copy WITHOUT `.venv` whose orchestrator.config.json names the interpreter by absolute path,
# so the old `.venv/Scripts/python.exe scripts/…` form resolved nowhere and the live models probed for an
# interpreter instead. The orchestrator puts the configured interpreter first on PATH for every agent session
# (orchestrator/cli.ts's `sessionEnvironment`).

L1_MARKER = "<!-- amended: live hardening L1 -->"


def test_every_agent_runs_scripts_as_python_on_the_session_path():
    files = sorted(AGENTS_DIR.glob(f"*{SUFFIX}")) + [ROOT / ".github" / "copilot-instructions.md"]
    assert len(files) == 9
    for path in files:
        text = path.read_text(encoding="utf-8")
        squeezed = " ".join(text.split())
        for venv in (".venv/Scripts", ".venv/bin", ".venv\\Scripts", "python.exe"):
            assert venv not in text, f"{path.name} still names {venv}"
        if path.name == f"documenter{SUFFIX}":
            # `ROLE_SCRIPTS.documenter` is empty (final review I3): the file tells the documenter to run no script, so
            # it carries no interpreter rule at all -- a rule about how to run a script would invite one.
            assert "you run no script" in squeezed and "never through a `.venv/…` path" not in squeezed, path.name
            continue
        assert "`python scripts/<name>.py …`" in squeezed, path.name
        assert "never through a `.venv/…` path" in squeezed, path.name
        if path.name == f"cookbook-curator{SUFFIX}":
            # The curator is CLI-only (README §1), so no orchestrator puts anything on its PATH -- the file
            # must not claim one does.
            assert "orchestrator puts" not in squeezed, path.name
            assert "no orchestrator starts you" in squeezed, path.name
        else:
            assert "puts the project's interpreter first on PATH" in squeezed, path.name
        if path.suffix == ".md" and path.name.endswith(SUFFIX):
            assert L1_MARKER in text, path.name


def _role_scripts() -> dict[str, set[str]]:
    """`orchestrator/policy.ts`'s `ROLE_SCRIPTS`: the scripts each role's session may run."""
    text = (ROOT / "orchestrator" / "policy.ts").read_text(encoding="utf-8")
    body = text[text.index("export const ROLE_SCRIPTS"):]
    body = re.sub(r"//[^\n]*", "", body[body.index("{") + 1:body.index("\n};")])
    return {role.strip('"'): set(re.findall(r'"(scripts/[a-z_]+\.py)"', scripts))
            for role, scripts in re.findall(r'("?[a-z-]+"?)\s*:\s*\[([^\]]*)\]', body)}


def test_every_script_an_agent_file_shows_runs_as_bare_python():
    """Every code span that runs a script names the interpreter as `python` (the parser-recovery agent's
    `python -m pytest tests/parser_corpus -q` is the spec's own wording, restored) -- and every script an agent
    file shows running is one its own role may run (`ROLE_SCRIPTS`; the policy's argument rules are checked on
    the same examples in `orchestrator/test/policy.test.ts`)."""
    allowed = _role_scripts()
    assert {"translator", "fixer", "analyzer", "intake"} <= set(allowed), allowed
    assert "validator" not in allowed, allowed
    runs = 0
    for path in sorted(AGENTS_DIR.glob(f"*{SUFFIX}")):
        role = path.name[:-len(SUFFIX)]
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"(\S+)\s+(scripts/[a-z_/]+\.py|-m pytest)", text):
            interpreter = match.group(1).strip("`")
            if interpreter.endswith(("python", "python.exe", "python3", "py")):
                runs += 1
                assert interpreter == "python", f"{path.name}: {match.group(0)}"
                script = match.group(2)
                if script != "-m pytest":
                    assert script in allowed.get(role, set()), (
                        f"{path.name} shows `{match.group(0)}`, and ROLE_SCRIPTS does not let {role} run {script}")
    assert runs >= 6, f"only {runs} script runs found -- the scan is not seeing them"
    for role in ("translator", "fixer"):
        for script in ("scripts/compile_check.py", "scripts/validate_build.py"):
            assert script in allowed[role], (role, script)
    assert "scripts/objective_check.py" in allowed["analyzer"]
    assert not allowed["reviewer"] and not allowed["documenter"]


def _takes_the_workflow_id_first(script: str) -> bool:
    """Whether `scripts/<script>.py`'s argparse declares `wf_id` as its first argument -- the scripts
    `orchestrator/policy.ts`'s WORKFLOW_ID_SCRIPTS holds to the session's workflow."""
    path = ROOT / "scripts" / f"{script}.py"
    if not path.is_file():
        return False
    first = re.search(r'add_argument\(\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    return bool(first) and first.group(1) == "wf_id"


def test_every_agent_command_example_puts_the_workflow_id_first():
    """The policy judges a workflow script's FIRST bare token as its workflow, so a command an agent file shows
    must put the `<id>` placeholder first -- never a flag first, whose value would then be read as the workflow.
    Every code span naming a workflow script is checked; a span naming the script with no argument is only a
    reference. (A script lane A has not written yet is skipped by `_takes_the_workflow_id_first`; the names
    below are pinned either way.)"""
    files = sorted(AGENTS_DIR.glob(f"*{SUFFIX}")) + [ROOT / ".github" / "copilot-instructions.md"]
    offenders, examples = [], 0
    for path in files:
        for span in re.findall(r"`([^`\n]+)`", path.read_text(encoding="utf-8")):
            for match in re.finditer(r"scripts/([a-z_]+)\.py((?:\s+\S+)*)", span):
                args = match.group(2).split()
                if not args:
                    continue
                if not (_takes_the_workflow_id_first(match.group(1))
                        or match.group(1) in ("compile_check", "validate_build", "objective_check", "deploy")):
                    continue
                examples += 1
                if args[0] not in ("<id>", "<wf_id>", "<wf>"):
                    offenders.append(f"{path.name}: `{span}`")
    assert examples >= 8, f"only {examples} command examples found -- the scan is not seeing them"
    assert not offenders, "a command example does not put the workflow id first:\n" + "\n".join(offenders)


def test_no_agent_file_shows_a_script_call_with_root():
    """`--root` is denied in every agent's script call, so no agent file may show one."""
    for path in sorted(AGENTS_DIR.glob(f"*{SUFFIX}")) + [ROOT / ".github" / "copilot-instructions.md"]:
        assert "--root" not in path.read_text(encoding="utf-8"), path.name


def test_no_agent_file_shows_a_script_call_with_a_backend_flag():
    """`--backend`, `--connection` and `--sandbox-database` are denied in every agent's script call
    (orchestrator/policy.ts), so no agent file may show one."""
    for path in sorted(AGENTS_DIR.glob(f"*{SUFFIX}")) + [ROOT / ".github" / "copilot-instructions.md"]:
        text = path.read_text(encoding="utf-8")
        for flag in ("--backend", "--connection", "--sandbox-database"):
            assert flag not in text, f"{path.name} shows {flag}"
