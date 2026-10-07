"""README §1's diagrams draw what the code does; this pins their content and runs the structural lint
the generated `docs/mapping.md` diagrams share (`lib/mermaid_lint.py`).
Rendering is checked by a person in a browser with Mermaid 11 (objective-first plan, Task B3)."""
from __future__ import annotations

from pathlib import Path

from lib.mermaid_lint import lint_mermaid, mermaid_blocks

ROOT = Path(__file__).resolve().parents[1]

#: What the diagrams must show: the objective's scripts, the one build file, its check, validation and
#: mapping, the questions gate, and the backend switch only a person flips.
MUST_SHOW = ("digest.py", "objective_scaffold.py", "objective_check.py", "objective_questions.py",
             "compile_check.py", "validate_build.py", "render_mapping.py", "build.sql", "build.py",
             "docs/mapping.md", "objective.json", "--backend snowflake", "WAITING_FOR_ANSWERS", "questions gate",
             "NEEDS_HUMAN", "QUARANTINED", "MANUAL", "parser-recovery", "export_dbt.py", "validate_dbt.py")

#: What was removed with the segment design: no diagram may draw it as part of the flow.
#: (The dbt EXPORT of a validated build, `export_dbt.py` / `validate_dbt.py` / `dbt/`, is drawn: design D9.)
MUST_NOT_SHOW = ("segment.py", "validate_segment", "validate_snowpark", "validate_workflow",
                 "check_seams", "plan_batches", "stitch_analysis", "contract_scaffold", "contract_check",
                 "translation_scaffold", "render_snowpark", "target_check", "dbt_surface", "master.sql", "procs/",
                 "chain-drift", "segments/")


def _blocks() -> list[str]:
    return mermaid_blocks((ROOT / "README.md").read_text(encoding="utf-8"))


def test_every_block_is_a_known_diagram_type_and_structurally_balanced():
    blocks = _blocks()
    assert len(blocks) >= 4
    for block in blocks:
        problems = lint_mermaid(block)
        assert not problems, (problems, block[:60])


def test_the_diagrams_show_the_objective_the_one_build_file_its_checks_and_the_mapping():
    text = "\n".join(_blocks())
    missing = [item for item in MUST_SHOW if item not in text]
    assert not missing, missing


def test_no_diagram_draws_the_retired_segment_chain_or_dbt_machinery():
    text = "\n".join(_blocks())
    drawn = [item for item in MUST_NOT_SHOW if item in text]
    assert not drawn, drawn
    assert "validator" not in text.lower()


def test_the_readme_has_a_components_a_sequence_and_a_stage_diagram():
    kinds = [block.lstrip().split("\n")[0].strip() for block in _blocks()]
    assert kinds.count("sequenceDiagram") == 1
    assert sum(kind.startswith("flowchart TB") for kind in kinds) >= 2


def test_no_sequence_diagram_line_holds_a_semicolon():
    # Mermaid reads ";" as a statement separator in a sequence diagram, so a message containing one
    # breaks the whole diagram on GitHub (found by the Mermaid 11 render check, 2026-09-24).
    for block in _blocks():
        if not block.lstrip().startswith("sequenceDiagram"):
            continue
        offenders = [problem for problem in lint_mermaid(block) if problem.startswith("semicolon:")]
        assert not offenders, offenders


def test_no_flowchart_node_label_is_left_unquoted_when_it_holds_a_bracket_or_a_pipe():
    """A label such as `A[foo (bar)]` or `A[x | y]` is read as syntax by Mermaid; every label that holds a
    parenthesis, a pipe or a colon must be written as `["..."]`."""
    import re
    unquoted = re.compile(r"\b[A-Za-z][A-Za-z0-9]*\[([^\]\"\n]*[(|:][^\]\"\n]*)\]")
    for block in _blocks():
        if not block.lstrip().startswith("flowchart"):
            continue
        offenders = [match.group(0) for match in unquoted.finditer(block)]
        assert not offenders, offenders
