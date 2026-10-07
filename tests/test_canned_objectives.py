"""The seven samples' canned objectives (`samples/<wf>/canned/objective.json` and `objective.md`): what a
correct analyzer writes for each, converted from the segment contracts and `analysis.md` the samples used
to carry.

Each must pass `objective_check.py --ready` on a workflow prepared from its sample with intake's answers
applied, must already agree with what `objective_scaffold.py --apply` would derive, and has no open
question (the analyzer asks only what the golden data cannot settle). The steps are the business process as
a person would describe it, not one per tool. What the old contracts declared is kept: keys, nullability,
`row_relation`, `ordering`, every parity risk -- except two that were about how the old pipeline cut and
duplicated work, not about Alteryx (below).

Nothing here has run on Snowflake or Alteryx: the dags and mappings are built locally from `samples/`.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import objective_check as oc
import objective_questions as oq
import objective_scaffold as osc
from lib import objective as objective_lib
from lib.io import read_json
from lib.vocab import DIFF_CLASSES
from tests.helpers import prepare_objective_workflow

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "samples"
WORKFLOWS = [f"wf_000{n}" for n in range(1, 8)]

TIERS = {"wf_0001": "T1", "wf_0002": "T1", "wf_0003": "T1", "wf_0004": "T1", "wf_0005": "T3", "wf_0006": "T2",
         "wf_0007": "T1"}

#: Every output's keys and its NOT NULL columns, as the old canned contracts declared them (an
#: `update_insert` output's keys are the Output tool's own update keys).
KEYS = {
    "wf_0001/7": ["REGION", "SIZE_BAND"], "wf_0001/8": [], "wf_0002/10": [], "wf_0003/10": ["ACCT", "PERIOD"],
    "wf_0004/6": ["SKU", "FAMILY"], "wf_0004/7": ["SKU", "NAME"], "wf_0005/4": [], "wf_0006/5": ["PERIOD"],
    "wf_0007/6": ["REGION", "PERIOD"], "wf_0007/7": ["REGION", "PERIOD"],
}
NOT_NULL = {
    "wf_0001/7": ["REGION", "SIZE_BAND"], "wf_0001/8": [], "wf_0002/10": ["MATCH_FLAG"], "wf_0003/10": [],
    "wf_0004/6": ["SKU"], "wf_0004/7": ["SKU", "NAME"], "wf_0005/4": [],
    "wf_0006/5": ["TOTAL_RECOGNIZED", "TOTAL_DEFERRED", "CUSTOMERS"],
    "wf_0007/6": ["REGION", "PERIOD", "LINES"], "wf_0007/7": [],
}

#: How many parity risks each (tool, class) carries. The old contracts' risks all appear, plus the three
#: wf_0001 analysis.md listed that no contract did (Count vs CountNonNull, Sum accumulation, NULL placement
#: of a descending Sort) and the two decisions wf_0005's analysis left to a person. Two old risks are not
#: here on purpose: wf_0004's "the macro needs a segment of its own" and "two Output tools compute tools 3
#: and 4 twice" were about how the old pipeline cut and duplicated work; the file the translator writes now
#: has no segments and may share a temporary table, so neither names behaviour to keep.
RISKS = {
    "wf_0001": {"2:TRUNCATION": 1, "3:NULL_SEMANTICS": 1, "4:NULL_SEMANTICS": 1, "4:ROUNDING": 1, "5:LOGIC": 1,
                "5:ROUNDING": 1, "6:ORDERING": 1},
    "wf_0002": {"10:LOGIC": 1, "2:LOGIC": 2, "2:NULL_SEMANTICS": 1, "4:NULL_SEMANTICS": 1, "4:ROUNDING": 1,
                "4:TYPE": 1, "5:LOGIC": 1, "5:NULL_SEMANTICS": 1, "9:LOGIC": 1},
    "wf_0003": {"1:LOGIC": 1, "10:LOGIC": 1, "10:NULL_SEMANTICS": 1, "2:LOGIC": 1, "2:NULL_SEMANTICS": 1,
                "3:NULL_SEMANTICS": 1, "3:TYPE": 1, "4:ORDERING": 1, "5:LOGIC": 1, "5:NULL_SEMANTICS": 1,
                "6:ROUNDING": 1, "7:ORDERING": 1, "8:NULL_SEMANTICS": 1, "9:LOGIC": 1, "9:ORDERING": 1},
    "wf_0004": {"1:TYPE": 1, "2:LOGIC": 5, "2:NULL_SEMANTICS": 1, "3:LOGIC": 1, "3:NULL_SEMANTICS": 1,
                "4:LOGIC": 1, "4:NULL_SEMANTICS": 1, "4:ROUNDING": 1, "5:LOGIC": 1, "5:NULL_SEMANTICS": 1},
    "wf_0005": {"2:UNKNOWN": 1, "3:UNKNOWN": 1},
    "wf_0006": {"1:TYPE": 1, "2:LOGIC": 1, "2:NULL_SEMANTICS": 1, "3:LOGIC": 2, "3:NULL_SEMANTICS": 3,
                "3:ROUNDING": 1, "4:LOGIC": 1, "4:ORDERING": 1, "4:ROUNDING": 1, "5:LOGIC": 1},
    "wf_0007": {"3:LOGIC": 2, "3:NULL_SEMANTICS": 1, "4:LOGIC": 1, "4:NULL_SEMANTICS": 1, "5:LOGIC": 1,
                "5:NULL_SEMANTICS": 1, "5:ROUNDING": 1, "6:LOGIC": 1, "7:LOGIC": 1, "7:NULL_SEMANTICS": 1},
}


def canned(wf_id: str) -> dict:
    return read_json(SAMPLES / wf_id / "canned" / "objective.json")


@pytest.fixture(params=WORKFLOWS)
def prepared(request, tmp_path):
    """(workflow id, a Repo holding it parsed, intake READY and its canned objective in place)."""
    return request.param, prepare_objective_workflow(tmp_path, request.param)


def test_every_canned_objective_passes_the_ready_check(prepared):
    wf_id, repo = prepared
    assert oc.check(repo, wf_id, ready=True) == []
    assert oc.main([wf_id, "--ready", "--root", str(repo.root)]) == 0


def test_the_canned_objective_already_agrees_with_what_the_scaffold_derives(prepared):
    wf_id, repo = prepared
    before = repo.wf(wf_id, "objective.json").read_bytes()
    assert osc.apply_file(repo, wf_id) is False
    assert repo.wf(wf_id, "objective.json").read_bytes() == before


def test_a_canned_objective_has_no_open_question(prepared):
    wf_id, repo = prepared
    assert canned(wf_id)["questions"] == [] and canned(wf_id)["answered"] == []
    assert oq.main([wf_id, "--status", "--root", str(repo.root)]) == 0
    assert oq.main([wf_id, "--write", "--root", str(repo.root)]) == 0
    assert not repo.wf(wf_id, "intake", "objective_questions.md").exists()


@pytest.mark.parametrize("wf_id", WORKFLOWS)
def test_the_tier_and_language_follow_the_tools(wf_id):
    objective = canned(wf_id)
    assert objective["tier"] == TIERS[wf_id]
    if wf_id == "wf_0006":
        assert objective["language"] == "python"
        assert "SUM(...) OVER" in objective["language_reason"]
    else:
        assert objective["language"] == "sql" and objective["language_reason"] is None


@pytest.mark.parametrize("wf_id", WORKFLOWS)
def test_the_steps_describe_the_business_process_not_every_tool(wf_id):
    objective = canned(wf_id)
    steps = objective["steps"]
    assert 2 <= len(steps) <= 6
    tools = [t for step in steps for t in step["tools"]]
    assert len(tools) == len(set(tools)), "a tool is in one step only"
    assert any(len(step["tools"]) > 1 for step in steps) or len(tools) <= 5, "steps group tools"
    for step in steps:
        assert objective_lib.STEP_ID.fullmatch(step["id"])
        assert step["name"].strip() and len(step["does"]) > 20


@pytest.mark.parametrize("wf_id", WORKFLOWS)
def test_every_output_is_named_by_the_samples_logical_names(wf_id):
    objective = canned(wf_id)
    sample = read_json(SAMPLES / wf_id / "sample.json")["logical"]
    assert {e["tool_id"]: e["logical"] for e in objective["inputs"] + objective["outputs"]} == sample


def test_keys_and_not_null_columns_are_the_ones_the_old_contracts_declared():
    found_keys, found_not_null = {}, {}
    for wf_id in WORKFLOWS:
        for output in canned(wf_id)["outputs"]:
            name = f"{wf_id}/{output['tool_id']}"
            found_keys[name] = output["keys"]
            found_not_null[name] = [c["name"] for c in output["columns"] if not c["nullable"]]
    assert found_keys == KEYS
    assert found_not_null == NOT_NULL


@pytest.mark.parametrize("wf_id", WORKFLOWS)
def test_every_parity_risk_is_carried_over(wf_id):
    risks = canned(wf_id)["parity_risks"]
    assert dict(Counter(f"{r['tool_id']}:{r['class']}" for r in risks)) == RISKS[wf_id]
    assert all(r["class"] in DIFF_CLASSES and len(r["note"]) > 30 for r in risks)


@pytest.mark.parametrize("wf_id", WORKFLOWS)
def test_no_note_still_speaks_of_segments_procedures_or_work_tables(wf_id):
    text = (SAMPLES / wf_id / "canned" / "objective.json").read_text(encoding="utf-8")
    for word in ("segment", "seg_0", "proc.sql", "proc.py", "work table", "MIG_WORK", "broken_sql", "validate_segment"):
        assert word not in text, f"{wf_id}: objective.json still mentions {word!r}"


@pytest.mark.parametrize("wf_id", WORKFLOWS)
def test_objective_md_has_the_fixed_headings_and_every_step_and_output(wf_id):
    objective = canned(wf_id)
    text = (SAMPLES / wf_id / "canned" / "objective.md").read_text(encoding="utf-8")
    assert text.startswith(f"# {wf_id} — {objective['title']}\n")
    assert [line for line in text.splitlines() if line.startswith("## ")] == list(oc.HEADINGS)
    assert text.endswith("\n") and "\r" not in text
    for step in objective["steps"]:
        assert f"**{step['id']} · {step['name']}**" in text
    for output in objective["outputs"]:
        assert f"### {output['logical']} (tool {output['tool_id']})" in text
        for column in output["columns"]:
            assert f"`{column['name']}`" in text
    for risk in objective["parity_risks"]:
        assert risk["note"] in text, "the parity risks are said in words under 'Alteryx behaviour to keep'"


def test_wf_0003_keeps_its_target_only_column_after_the_streams_own():
    (output,) = canned("wf_0003")["outputs"]
    assert [c["name"] for c in output["columns"]][-1] == "LOADED_FLAG"
    assert output["write_mode"] == "update_insert" and output["keys"] == ["ACCT", "PERIOD"]
    assert output["pre_sql"].startswith("DELETE FROM") and output["post_sql"].startswith("UPDATE")


def test_data_less_nodes_need_no_step_but_wf_0002_lists_its_browse_tool(tmp_path):
    repo = prepare_objective_workflow(tmp_path, "wf_0002")
    dag = read_json(repo.wf("wf_0002", "parsed", "dag.json"))
    exempt = {n["tool_id"] for n in dag["nodes"] if n["type"] in ("container", "comment", "browse")}
    objective = canned("wf_0002")
    in_steps = {t for step in objective["steps"] for t in step["tools"]}
    assert exempt == {"100", "200", "210", "300", "11"}
    assert not exempt & in_steps
    assert [n["tool_id"] for n in objective["no_effect"]] == ["11"]


def test_the_two_decisions_that_stop_wf_0005_are_parity_risks_not_questions():
    objective = canned("wf_0005")
    assert objective["tier"] == "T3" and objective["questions"] == []
    assert {(r["tool_id"], r["class"]) for r in objective["parity_risks"]} == {("2", "UNKNOWN"), ("3", "UNKNOWN")}


@pytest.mark.parametrize("wf_id", WORKFLOWS)
def test_each_output_converts_to_the_contract_shape_compare_reads(wf_id):
    objective = canned(wf_id)
    for output in objective["outputs"]:
        view = objective_lib.contract_view(objective, output)
        assert view["output"]["tool_id"] == output["tool_id"]
        assert view["row_relation"] == output["row_relation"]
        names = {c["name"] for c in view["output"]["columns"]}
        assert set(view["output"]["keys"]) <= names
        assert set(view["ordering"]["order_dependent_columns"]) <= names
