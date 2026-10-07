"""The documents the objective-first change rewrote describe the pipeline as it is now.

The segment, procedure and dbt design was removed, not kept as a fallback (docs/superpowers/specs/
2026-10-06-objective-first-design.md §6). This scan keeps the README, the agent files, the cookbook and the
reference pages from naming the scripts and files that went with it, and from claiming a validator agent.
Historical records are not scanned: `docs/live-smoke-test.md`, `docs/superpowers/**` and `docs/spec/**`
describe what was true when they were written.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: What the fifth live test was, as the README and the hand-off guides say it (final review I3): Claude Sonnet stood in
#: for the model through `--runner external`, outside the Copilot SDK and its tool policy; no local or hosted model ran.
NEW_TRUTH = ('re-run end to end with Claude Sonnet standing in for the model (`--runner external`, outside the Copilot SDK '
             'and its tool policy — docs/live-smoke-test.md, "Fifth live test"); not yet with the local model or a '
             'GitHub-hosted model')

#: Scripts, libraries and files of the segment/procedure/dbt design.
#: The agent-written dbt project went with them (`dbt_surface`, `--target dbt`, the project files an agent kept
#: beside its models); the dbt EXPORT of a validated `build.sql` (`export_dbt.py`, `validate_dbt.py`, `dbt/`,
#: `dbt_project.yml`, design D9) is part of the pipeline now, and the documents are expected to name it.
RETIRED = (
    "validate_segment.py", "validate_snowpark.py", "validate_workflow.py",
    "target_check.py", "plan_batches.py", "check_seams.py", "stitch_analysis.py", "contract_scaffold.py",
    "contract_check.py", "translation_scaffold.py", "render_snowpark.py", "segment_scale.py",
    "proc_runner", "dbt_surface", "procs/master.sql", "procs/README.md", "output_kind",
    "segments/", "contract.json", "unsupported.json", "cookbook/dbt.md", "output-targets.md",
    "large-workflows.md", "docs/reference/contracts.md", "TODO(scaffold)",
    "--target dbt", "dbt/compile_check.json", "dbt/review.json", "dbt/translation_notes.md", "PROFILES_TEMPLATE",
)

#: What this scan reads: the front page, the instructions, the agent files, the cookbook and the references
#: the change rewrote (the live-test record and the superpowers/spec trees are history, never scanned).
SCANNED = (
    ["README.md", ".github/copilot-instructions.md", "docs/handoff-production.md", "docs/handoff-copilot-models.md",
     "docs/production-backlog.md"]
    + [f"docs/reference/{name}.md" for name in ("dag-contract", "real-workflows", "simulator-semantics",
                                                  "snowflake-backend")]
)


def _files() -> list[Path]:
    paths = [ROOT / rel for rel in SCANNED]
    paths += sorted((ROOT / ".github" / "agents").glob("*.agent.md"))
    # cookbook/dbt.md is deleted with the dbt target (and its test); every other page is scanned
    paths += [path for path in sorted((ROOT / "cookbook").glob("*.md")) if path.name != "dbt.md"]
    return paths


def test_no_rewritten_document_names_a_retired_script_or_file():
    offenders = []
    for path in _files():
        text = path.read_text(encoding="utf-8")
        offenders += [f"{path.relative_to(ROOT).as_posix()}: {word}" for word in RETIRED if word in text]
    assert not offenders, offenders


def test_no_rewritten_document_claims_a_validator_agent():
    offenders = []
    for path in _files():
        text = " ".join(path.read_text(encoding="utf-8").split())
        for match in re.finditer(r"[^.]*\bvalidator (agent|role)\b[^.]*\.", text, flags=re.IGNORECASE):
            sentence = match.group(0)
            # the one thing the documents say about it is that there is none
            if not re.search(r"\b(no|not|retired|never|there is no|without|first version|earlier)\b", sentence,
                             flags=re.IGNORECASE):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {sentence.strip()[:100]}")
    assert not offenders, offenders


def test_the_readme_says_what_it_has_and_has_not_run():
    squeezed = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert NEW_TRUTH in squeezed.replace("**", "")
    assert "has **not yet been re-run" not in squeezed and "That live re-run is still to come" not in squeezed
    assert "through the real Copilot SDK, and with Claude Sonnet standing in through `--runner external`" in squeezed
    assert "nothing has run on a real Snowflake account, a real Alteryx engine, or a GitHub-hosted model" in \
        squeezed.replace("Nothing", "nothing")
    assert "mock runner" in squeezed
    assert "The templated `EXECUTE IMMEDIATE FROM … USING` form has not run on any account" in squeezed.replace("**", "")


def test_every_document_that_says_what_has_run_says_the_fifth_live_test_was_a_stand_in():
    for path in ("README.md", "docs/handoff-production.md", "docs/handoff-copilot-models.md"):
        squeezed = " ".join((ROOT / path).read_text(encoding="utf-8").split()).replace("**", "")
        assert NEW_TRUTH in squeezed, path
        assert "not yet been re-run with a real model" not in squeezed, path


def test_the_documented_e2e_command_selects_tests():
    """Final review I4: `pytest -m e2e` exits 5 when nothing carries the marker, so a command the README shows (and a
    marker pyproject registers) needs tests that carry it."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    registered = "e2e" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    marked = sum(path.read_text(encoding="utf-8").count("@pytest.mark.e2e") for path in (ROOT / "tests").glob("test_*.py")
                 if path.name != Path(__file__).name)
    if "pytest -m e2e" in readme or registered:
        assert marked >= 2, "the e2e marker is advertised, and no test carries it"
    assert marked == 0 or registered, "tests carry an e2e marker pyproject.toml does not register"


def test_the_readme_describes_the_build_file_deliverable_and_its_scripts():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for token in ("build.sql", "build.py", "objective.json", "objective.md", "docs/mapping.md",
                  "scripts/render_mapping.py", "scripts/deploy.py", "scripts/validate_build.py",
                  "scripts/objective_check.py", "digest.py", "--render", "build.rendered.sql",
                  "lint <kind> <name>: kept because <reason>", "docs/reference/objective.md",
                  "docs/reference/build-file.md", "2026-10-06-objective-first-design.md",
                  "intake/objective_questions.md", "--scenario fix-loop"):
        assert token in text, token


def test_the_readme_describes_the_dbt_export_as_a_script_not_an_agent():
    text = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    for token in ("scripts/export_dbt.py", "scripts/validate_dbt.py", "workflows/<id>/dbt/", "program.dbt: false",
                  "converted from the validated `build.sql` by a script", "never an agent",
                  "dbt: <the first line of the script's stderr>", "deploy.py", "--dbt",
                  "dbt build --project-dir workflows/<id>/dbt --profiles-dir <your profiles dir> --target <name>",
                  "**The project has only run on that DuckDB double**"):
        assert token in text, token


def test_the_cookbook_index_says_the_patterns_are_expression_references():
    text = " ".join((ROOT / "cookbook" / "index.md").read_text(encoding="utf-8").split())
    assert "expression references, not structure" in text
    assert "ONE minimal build file" in text
    assert "leave the CTE-per-tool wrapper behind" in text


def test_the_snowpark_page_describes_a_whole_workflow_build_py():
    text = " ".join((ROOT / "cookbook" / "snowpark.md").read_text(encoding="utf-8").split())
    for token in ("def main(session, src: str, tgt: str) -> str", "# step s1", "`language` is `python`",
                  '{src}.<LOGICAL>', '{tgt}.<LOGICAL>'):
        assert token in text, token
    assert "def run(" not in text and "src_db" not in text and "proc.py" not in text


def test_the_cookbook_never_teaches_dynamic_sql_for_a_build_sql():
    """A `build.sql` may not hold `EXECUTE IMMEDIATE` or any scripting (`statement:kind`): a dynamic header or
    column set is frozen from the golden/parsed metadata and noted in `translation_notes.md`, or the workflow is
    a `build.py` one."""
    index = " ".join((ROOT / "cookbook" / "index.md").read_text(encoding="utf-8").split())
    assert "dynamic SQL" not in index
    for token in ("a frozen list from the golden/parsed metadata", "translation_notes.md", "`build.py` only when the set truly varies"):
        assert token in index, token
    cross_tab = " ".join((ROOT / "cookbook" / "cross_tab.md").read_text(encoding="utf-8").split())
    assert "dynamic header set → dynamic SQL or frozen list" not in cross_tab
    assert "(`statement:kind`)" in cross_tab and "Freeze the column list from the golden or parsed metadata" in cross_tab
    assert "unless the header set is genuinely expected to grow" not in cross_tab
