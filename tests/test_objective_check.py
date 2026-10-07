"""`objective_check.py`: every rule of the objective (design §4.2) as a named problem, printed one per line
as `<field>: <problem>; expected <what>` -- the text the orchestrator records and shows the analyzer on its
retry, and what the analyzer sees when it runs this script itself.

Each test starts from a complete, passing objective for `wf_0001` (the skeleton `objective_scaffold.py`
writes, plus the analyzer's judgment) and breaks one thing. A rule has a test that names the problem it
prints; the seven committed samples' canned objectives are checked in `test_canned_objectives.py`.

Nothing here has run on Snowflake or Alteryx: the dag and mappings are built locally from `samples/`.
"""
from __future__ import annotations

import copy
import re

import pytest

import objective_check as oc
import objective_scaffold as osc
from lib.io import read_json, read_yaml, write_json, write_yaml
from lib.paths import Repo
from tests.helpers import prepare_objective_workflow

WF = "wf_0001"

STEPS = [
    {"id": "s1", "name": "Read and clean orders", "does": "Reads ORDERS and shortens CUSTOMER.",
     "tools": ["1", "2"], "feeds": ["s2"]},
    {"id": "s2", "name": "Set aside the WEST region", "does": "Splits WEST orders off.",
     "tools": ["3"], "feeds": ["s3", "s5"]},
    {"id": "s3", "name": "Net value and size band", "does": "Prices each order.", "tools": ["4"], "feeds": ["s4"]},
    {"id": "s4", "name": "Totals by region and band", "does": "Sums by region and band and writes them.",
     "tools": ["5", "6", "7"], "feeds": ["out:7"]},
    {"id": "s5", "name": "List the excluded orders", "does": "Writes the WEST orders.", "tools": ["8"],
     "feeds": ["out:8"]},
]


def fill(objective: dict) -> None:
    """Plays the analyzer: judgment for the wf_0001 skeleton."""
    objective.update(
        title="Sales summary by region and size band",
        purpose="Totals each region's net order value by size band, leaving WEST out, and lists the left-out orders.",
        tier="T1", language="sql", language_reason=None,
        steps=copy.deepcopy(STEPS), no_effect=[],
        rules=["Orders whose region is WEST are written to EXCLUDED_ORDERS instead of the summary."],
        parity_risks=[{"tool_id": "3", "class": "NULL_SEMANTICS", "note": "A NULL region goes to the False side."}],
        questions=[], answered=[])
    for entry in objective["inputs"]:
        entry["keys"] = []
    summary, excluded = objective["outputs"]
    summary.update(grain="one row per region and size band", keys=["REGION", "SIZE_BAND"], row_relation="aggregate",
                   ordering={"keys": ["TOTAL_NET", "REGION"], "alteryx_deterministic": True,
                             "order_dependent_columns": []},
                   tolerances={"TOTAL_NET": {"float_abs": 0.01}}, normalizations=["trim:REGION"])
    excluded.update(grain="one row per excluded order", keys=[], row_relation="filter",
                    ordering={"keys": [], "alteryx_deterministic": True, "order_dependent_columns": []})
    for output in objective["outputs"]:
        for column in output["columns"]:
            column["meaning"] = f"the {column['name'].lower().replace('_', ' ')}"


MD = """\
# wf_0001 — Sales summary by region and size band

## Purpose
x

## Inputs
x

## Outputs
x

## Steps
x

## Business rules
x

## Alteryx behaviour to keep
x

## Questions
None.
"""


def build(tmp_path, *, md: bool = True) -> Repo:
    repo = prepare_objective_workflow(tmp_path, WF, canned=False)
    osc.prefill(repo, WF)
    objective = read_json(repo.wf(WF, "objective.json"))
    fill(objective)
    write_json(repo.wf(WF, "objective.json"), objective)
    if md:
        repo.wf(WF, "objective.md").write_text(MD, encoding="utf-8", newline="\n")
    return repo


@pytest.fixture
def repo(tmp_path) -> Repo:
    return build(tmp_path)


def edit(repo: Repo, change) -> None:
    objective = read_json(repo.wf(WF, "objective.json"))
    change(objective)
    write_json(repo.wf(WF, "objective.json"), objective)


def problems(repo: Repo, *, ready: bool = False) -> list[str]:
    return oc.check(repo, WF, ready=ready)


def has(found: list[str], text: str) -> bool:
    return any(text in line for line in found)


def test_the_baseline_objective_passes_both_checks(repo):
    assert problems(repo) == []
    assert problems(repo, ready=True) == []


# --- the file itself ----------------------------------------------------------------------------------

def test_a_missing_objective_is_a_problem(tmp_path):
    repo = prepare_objective_workflow(tmp_path, WF, canned=False)
    assert problems(repo) == ["objective.json: is missing; expected the analyzer's objective for wf_0001"]


def test_an_objective_that_is_not_json_or_not_an_object_is_a_problem(repo):
    path = repo.wf(WF, "objective.json")
    path.write_text("{oops", encoding="utf-8")
    (line,) = problems(repo)
    assert line.startswith("objective.json: not valid JSON") and line.endswith("expected a JSON object")
    path.write_text("[1]\n", encoding="utf-8")
    assert problems(repo) == ["objective.json: is a JSON array; expected a JSON object"]


# --- required keys and their types ---------------------------------------------------------------------

@pytest.mark.parametrize("key, shape", [
    ("title", "a non-empty string"), ("purpose", "a non-empty string"),
    ("inputs", "a list"), ("outputs", "a list"), ("steps", "a list"), ("no_effect", "a list"),
    ("rules", "a list"), ("parity_risks", "a list"), ("questions", "a list"), ("answered", "a list"),
    ("tier", "one of"), ("language", "one of"), ("language_reason", "null"), ("workflow", '"wf_0001"'),
])
def test_a_missing_top_level_key_is_named(repo, key, shape):
    edit(repo, lambda o: o.pop(key))
    found = problems(repo)
    assert has(found, f"{key}: is missing; expected ") and has(found, shape)


@pytest.mark.parametrize("key, value, expected", [
    ("title", "", "a non-empty string"), ("title", 5, "a non-empty string"),
    ("purpose", "  ", "a non-empty string"),
    ("workflow", "wf_0009", '"wf_0001"'),
    ("tier", "T9", 'one of "T1", "T2", "T3"'), ("tier", None, 'one of "T1", "T2", "T3"'),
    ("language", "rust", 'one of "sql", "python"'), ("language", None, 'one of "sql", "python"'),
    ("steps", "x", "a list"), ("rules", {"a": 1}, "a list"), ("parity_risks", None, "a list"),
])
def test_a_wrong_top_level_value_is_named_with_what_is_expected(repo, key, value, expected):
    edit(repo, lambda o: o.__setitem__(key, value))
    found = problems(repo)
    assert any(line.startswith(f"{key}: is ") and f"; expected {expected}" in line for line in found), found


def test_every_problem_line_has_the_field_the_problem_and_what_is_expected(repo):
    def wreck(o):
        o["title"] = ""
        o["steps"][0]["id"] = "S1"
        o["outputs"][0]["row_relation"] = "weird"
        o["parity_risks"][0]["class"] = "NOPE"
        o["questions"] = [{"id": "x"}]
        o["inputs"][0]["columns"][0]["type"] = "NUMBER(1)"
    edit(repo, wreck)
    found = problems(repo)
    assert len(found) >= 5
    for line in found:
        assert re.fullmatch(r"[^:]+: .+; expected .+", line), line


# --- language and tier ---------------------------------------------------------------------------------

def test_python_needs_a_reason(repo):
    edit(repo, lambda o: o.update(language="python", language_reason=None))
    assert has(problems(repo), "language_reason: is null but language is python; expected a sentence saying why SQL")
    edit(repo, lambda o: o.update(language_reason="  "))
    assert has(problems(repo), "language_reason: is \"  \" but language is python")
    edit(repo, lambda o: o.update(language_reason="A running balance carries between rows."))
    assert problems(repo) == []


def test_sql_takes_no_reason(repo):
    edit(repo, lambda o: o.update(language_reason="because"))
    assert has(problems(repo), 'language_reason: is "because" but language is sql; expected null')
    edit(repo, lambda o: o.update(language_reason=""))
    assert problems(repo) == [], "an empty reason counts as none"


def _put_node(repo: Repo, tool_type: str, tool_id: str = "50") -> None:
    dag = read_json(repo.wf(WF, "parsed", "dag.json"))
    dag["nodes"].append({"tool_id": tool_id, "type": tool_type, "config": {}, "in_anchors": [], "out_anchors": [],
                         "meta": {}})
    write_json(repo.wf(WF, "parsed", "dag.json"), dag)


def test_a_snowpark_class_tool_forces_python(repo):
    _put_node(repo, "python")
    edit(repo, lambda o: o["steps"][0]["tools"].append("50"))
    assert has(problems(repo), 'language: is "sql" but tool 50 (python) is a snowpark-class tool; expected "python"')
    edit(repo, lambda o: o.update(language="python", language_reason="The tool is a Python script."))
    assert not has(problems(repo), "snowpark-class")


def test_a_manual_class_tool_forces_tier_t3(repo):
    _put_node(repo, "run_command")
    edit(repo, lambda o: o["steps"][0]["tools"].append("50"))
    assert has(problems(repo), 'tier: is "T1" but tool 50 (run_command) is a manual-class tool; expected "T3"')
    edit(repo, lambda o: o.update(tier="T3"))
    assert not has(problems(repo), "manual-class")


def test_a_snowpark_tool_inside_a_macro_counts_too(repo):
    dag = read_json(repo.wf(WF, "parsed", "dag.json"))
    dag["nodes"].append({"tool_id": "60", "type": "macro", "config": {}, "in_anchors": [], "out_anchors": [],
                         "meta": {}, "sub_dag": {"nodes": [{"tool_id": "1", "type": "python", "config": {},
                                                             "meta": {}}], "edges": []}})
    write_json(repo.wf(WF, "parsed", "dag.json"), dag)
    edit(repo, lambda o: o["steps"][0]["tools"].append("60"))
    assert has(problems(repo), "tool 60/1 (python) is a snowpark-class tool")


# --- steps ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("step_id", ["S1", "s0", "s01", "s", "1", "s1\n", "seg_01", 7, None])
def test_a_step_id_must_be_s_then_a_number_without_a_leading_zero(repo, step_id):
    edit(repo, lambda o: o["steps"][0].__setitem__("id", step_id))
    assert has(problems(repo), "steps[0].id: is ") and has(problems(repo), "expected s<N> such as s1")


def test_step_ids_are_unique(repo):
    edit(repo, lambda o: o["steps"][1].__setitem__("id", "s1"))
    assert has(problems(repo), 'steps[1].id: "s1" is used by more than one step; expected unique step ids')


@pytest.mark.parametrize("key", ["name", "does"])
def test_a_step_needs_a_name_and_a_description(repo, key):
    edit(repo, lambda o: o["steps"][0].__setitem__(key, ""))
    assert has(problems(repo), f"steps[0].{key}: is \"\"; expected a non-empty string")


def test_a_step_is_an_object_with_a_list_of_tools_and_feeds(repo):
    edit(repo, lambda o: o["steps"].append("not a step"))
    assert has(problems(repo), 'steps[5]: is "not a step"; expected an object {id, name, does, tools, feeds}')
    edit(repo, lambda o: o["steps"].pop())
    edit(repo, lambda o: o["steps"][0].__setitem__("tools", "1"))
    assert has(problems(repo), 'steps[0].tools: is "1"; expected a non-empty list of tool ids')
    edit(repo, lambda o: o["steps"][0].update(tools=["1", "2"], feeds="s2"))
    assert has(problems(repo), 'steps[0].feeds: is "s2"; expected a list of step ids and out:<output tool id>')
    edit(repo, lambda o: o["steps"][0].pop("feeds"))
    assert has(problems(repo), "steps[0].feeds: is missing; expected a list of step ids")


def test_a_step_whose_tools_hold_a_list_is_named_not_a_crash(repo):
    edit(repo, lambda o: o["steps"][0].__setitem__("tools", [["1", "2"]]))
    found = problems(repo)
    assert has(found, "steps[0].tools[0]:"), found


def test_a_step_with_no_tools_is_a_problem(repo):
    edit(repo, lambda o: o["steps"].append({"id": "s6", "name": "Nothing", "does": "x", "tools": [], "feeds": []}))
    assert has(problems(repo), "steps[5].tools: is []; expected a non-empty list of tool ids")


@pytest.mark.parametrize("feed, reason", [
    ("s9", '"s9" is not a step'), ("out:99", '"out:99" is not an output tool'),
    ("out:3", '"out:3" is not an output tool'), ("t1", '"t1" is not a step'), ("s1\n", '"s1\\n" is not a step'),
])
def test_feeds_name_a_step_or_an_output_tool(repo, feed, reason):
    edit(repo, lambda o: o["steps"][0]["feeds"].append(feed))
    assert has(problems(repo), f"steps[0].feeds: {reason}; expected a step id or out:<output tool id>")


def test_a_step_may_not_feed_itself(repo):
    edit(repo, lambda o: o["steps"][2]["feeds"].append("s3"))
    found = problems(repo)
    assert "steps[2].feeds: names its own step; expected later steps or out:<output tool id>" in found
    assert len([line for line in found if "feeds" in line]) == 1           # only the self-feed is named


def test_an_input_tool_may_not_be_set_aside_in_no_effect(repo):
    """An Input tool carries the data a step reads: it belongs in the step that reads it, never in `no_effect`."""
    edit(repo, lambda o: (o["steps"][0]["tools"].remove("1"),
                          o["no_effect"].append({"tool_id": "1", "reason": "It only reads a file."})))
    found = problems(repo)
    assert "no_effect[0]: tool 1 is an Input tool; expected it in the step that reads it" in found
    assert not has(found, "steps: tool 1")                      # it is accounted for: the one problem is the above


def test_a_non_input_tool_may_still_be_set_aside_in_no_effect(repo):
    edit(repo, lambda o: (o["steps"][1]["tools"].remove("3"),
                          o["no_effect"].append({"tool_id": "3", "reason": "it does nothing"})))
    assert not has(problems(repo), "is an Input tool")


def test_every_tool_of_the_dag_must_be_in_a_step_or_in_no_effect(repo):
    edit(repo, lambda o: o["steps"][1]["tools"].remove("3"))
    assert has(problems(repo), "steps: tool 3 is in no step and not in no_effect; "
                               "expected every tool id of parsed/dag.json exactly once")


def test_no_effect_accounts_for_a_tool_too(repo):
    edit(repo, lambda o: (o["steps"][1]["tools"].remove("3"),
                          o["no_effect"].append({"tool_id": "3", "reason": "it does nothing"})))
    found = problems(repo)
    assert not has(found, "tool 3 is in no step")


def test_a_tool_in_two_places_is_a_problem(repo):
    edit(repo, lambda o: o["steps"][0]["tools"].append("3"))
    assert has(problems(repo), "steps: tool 3 is listed more than once (steps s1, s2); "
                               "expected every tool id of parsed/dag.json exactly once")
    edit(repo, lambda o: (o["steps"][0]["tools"].remove("3"),
                          o["no_effect"].append({"tool_id": "3", "reason": "x"})))
    assert has(problems(repo), "steps: tool 3 is listed more than once (steps s2, no_effect)")


def test_a_tool_that_is_not_in_the_dag_is_a_problem(repo):
    edit(repo, lambda o: o["steps"][0]["tools"].append("99"))
    assert has(problems(repo), 'steps[0].tools[2]: is "99", not a tool of parsed/dag.json; '
                               "expected a tool id of the DAG")
    edit(repo, lambda o: o["no_effect"].append({"tool_id": "98", "reason": "x"}))
    assert has(problems(repo), 'no_effect[0].tool_id: is "98", not a tool of parsed/dag.json')


def test_a_tool_inside_a_macro_is_not_a_top_level_tool(repo):
    edit(repo, lambda o: o["steps"][0]["tools"].append("2/1"))
    assert has(problems(repo), 'steps[0].tools[2]: is "2/1", not a tool of parsed/dag.json')


def test_containers_comments_and_browse_tools_need_no_step(repo):
    for tool_id, tool_type in (("50", "container"), ("51", "comment"), ("52", "browse"), ("53", "interface")):
        _put_node(repo, tool_type, tool_id)
    assert problems(repo) == []


def test_a_data_less_tool_may_be_listed_in_no_effect_but_never_twice(repo):
    _put_node(repo, "browse", "52")
    edit(repo, lambda o: o["no_effect"].append({"tool_id": "52", "reason": "Browse tool: displays data"}))
    assert problems(repo) == []
    edit(repo, lambda o: o["steps"][0]["tools"].append("52"))
    assert has(problems(repo), "steps: tool 52 is listed more than once (steps s1, no_effect)")


def test_a_tool_that_carries_data_is_not_exempt_whatever_else_is(repo):
    _put_node(repo, "formula", "54")
    assert has(problems(repo), "steps: tool 54 is in no step and not in no_effect")


def test_no_effect_entries_need_a_tool_and_a_reason(repo):
    edit(repo, lambda o: o["no_effect"].append({"tool_id": "9", "reason": ""}))
    found = problems(repo)
    assert has(found, 'no_effect[0].reason: is ""; expected a non-empty reason')
    edit(repo, lambda o: o["no_effect"].__setitem__(0, "x"))
    assert has(problems(repo), 'no_effect[0]: is "x"; expected an object {tool_id, reason}')


def test_every_output_tool_must_be_inside_a_step(repo):
    edit(repo, lambda o: o["steps"][4]["tools"].remove("8"))
    edit(repo, lambda o: o["no_effect"].append({"tool_id": "8", "reason": "x"}))
    assert has(problems(repo), "outputs[1] (tool 8): is in no step; expected every output's tool inside a step")


# --- mechanical fields agree with a fresh derivation -----------------------------------------------------

def test_workflow_must_be_the_workflow(repo):
    edit(repo, lambda o: o.__setitem__("workflow", "wf_0002"))
    assert has(problems(repo), 'workflow: is "wf_0002"; expected "wf_0001"')


def test_a_tool_id_written_as_a_number_is_named(repo):
    edit(repo, lambda o: o["outputs"][0].__setitem__("tool_id", 7))
    assert has(problems(repo), 'outputs[0] (tool 7).tool_id: is 7; expected "7"')


def test_a_changed_logical_name_is_named(repo):
    edit(repo, lambda o: o["inputs"][0].__setitem__("logical", "ORDER"))
    assert has(problems(repo), 'inputs[0] (tool 1).logical: is "ORDER"; expected "ORDERS"')
    edit(repo, lambda o: o["inputs"][0].pop("logical"))
    assert has(problems(repo), 'inputs[0] (tool 1).logical: is missing; expected "ORDERS"')


def test_a_changed_type_is_named(repo):
    edit(repo, lambda o: o["inputs"][0]["columns"][1].__setitem__("type", "VARCHAR(5)"))
    assert has(problems(repo), 'inputs[0] (tool 1).columns[1] (CUSTOMER).type: is "VARCHAR(5)"; expected "VARCHAR(50)"')
    edit(repo, lambda o: o["outputs"][0]["columns"][2].pop("type"))
    assert has(problems(repo), 'outputs[0] (tool 7).columns[2] (TOTAL_NET).type: is missing; expected "FLOAT"')


def test_the_type_maps_unsized_varchar_is_accepted_for_a_variable_string(repo):
    edit(repo, lambda o: o["inputs"][0]["columns"][1].__setitem__("type", "VARCHAR"))
    assert problems(repo) == []
    edit(repo, lambda o: o["inputs"][0]["columns"][0].__setitem__("type", "VARCHAR"))
    assert has(problems(repo), "columns[0] (ORDER_ID).type: is \"VARCHAR\"; expected \"NUMBER(38,0)\"")


def test_columns_must_be_the_dags_in_order(repo):
    edit(repo, lambda o: o["inputs"][0]["columns"].reverse())
    assert has(problems(repo), "inputs[0] (tool 1).columns: are named [STATUS, ORDER_DATE, QTY, AMOUNT_TXT, REGION, "
                               "CUSTOMER, ORDER_ID]; expected [ORDER_ID, CUSTOMER, REGION, AMOUNT_TXT, QTY, "
                               "ORDER_DATE, STATUS]")
    edit(repo, lambda o: o["inputs"][0]["columns"].reverse())
    edit(repo, lambda o: o["outputs"][1]["columns"].pop())
    assert has(problems(repo), "outputs[1] (tool 8).columns: are named [")


def test_an_overwrite_target_may_not_carry_extra_columns(repo):
    edit(repo, lambda o: o["outputs"][0]["columns"].append(
        {"name": "EXTRA", "type": "VARCHAR(1)", "nullable": True, "meaning": "x"}))
    assert has(problems(repo), "outputs[0] (tool 7).columns: are named [")


def test_an_appending_target_may_carry_target_only_columns_after_the_streams_own(repo):
    mappings = read_yaml(repo.wf(WF, "intake", "mappings.yaml"))
    mappings["outputs"]["out/sales_summary.yxdb"]["mode"] = "append"
    write_yaml(repo.wf(WF, "intake", "mappings.yaml"), mappings)
    edit(repo, lambda o: o["outputs"][0].__setitem__("write_mode", "append"))
    edit(repo, lambda o: o["outputs"][0]["columns"].append(
        {"name": "LOADED", "type": "VARCHAR(1)", "nullable": True, "meaning": "set by a PostSQL"}))
    assert problems(repo) == []
    edit(repo, lambda o: o["outputs"][0]["columns"][-1].__setitem__("type", "1; DROP"))
    assert has(problems(repo), "outputs[0] (tool 7).columns[6] (LOADED).type: is \"1; DROP\"; expected a Snowflake "
                               "column type")


def test_an_entry_the_dag_does_not_show_and_a_missing_one_are_both_named(repo):
    edit(repo, lambda o: o["outputs"].append({"tool_id": "99", "logical": "NOPE", "columns": []}))
    assert has(problems(repo), "outputs: tool 99 is not an Output tool of parsed/dag.json; expected only: 7, 8")
    edit(repo, lambda o: o["outputs"].pop())
    edit(repo, lambda o: o["outputs"].pop(0))
    found = problems(repo)
    assert has(found, "outputs: has no entry for tool 7; expected one entry per Output tool: 7, 8")
    edit(repo, lambda o: o.__setitem__("inputs", []))
    assert has(problems(repo), "inputs: has no entry for tool 1; expected one entry per Input tool: 1")


def test_entries_must_be_in_tool_id_order(repo):
    edit(repo, lambda o: o["outputs"].reverse())
    assert has(problems(repo), "outputs: are in the order 8, 7; expected 7, 8")


def test_write_mode_pre_sql_and_post_sql_are_mechanical(repo):
    edit(repo, lambda o: o["outputs"][0].update(write_mode="append", pre_sql="DELETE FROM x", post_sql="x"))
    found = problems(repo)
    assert has(found, 'outputs[0] (tool 7).write_mode: is "append"; expected "overwrite"')
    assert has(found, 'outputs[0] (tool 7).pre_sql: is "DELETE FROM x"; expected null')
    assert has(found, 'outputs[0] (tool 7).post_sql: is "x"; expected null')


def test_an_update_insert_outputs_keys_are_mechanical_and_an_overwrite_outputs_are_not(repo):
    edit(repo, lambda o: o["outputs"][0].__setitem__("keys", ["TOTAL_NET"]))
    assert problems(repo) == []
    mappings = read_yaml(repo.wf(WF, "intake", "mappings.yaml"))
    entry = mappings["outputs"]["out/sales_summary.yxdb"]
    entry.update(mode="merge", keys=["region", "size_band"])
    write_yaml(repo.wf(WF, "intake", "mappings.yaml"), mappings)
    found = problems(repo)
    assert has(found, 'outputs[0] (tool 7).write_mode: is "overwrite"; expected "update_insert"')
    assert has(found, 'outputs[0] (tool 7).keys: is ["TOTAL_NET"]; expected ["REGION", "SIZE_BAND"]')


def test_without_intake_the_logical_names_must_be_null(tmp_path):
    repo = build(tmp_path)
    repo.wf(WF, "intake", "mappings.yaml").unlink()
    found = problems(repo)
    assert has(found, 'inputs[0] (tool 1).logical: is "ORDERS"; expected null')
    edit(repo, lambda o: [e.__setitem__("logical", None) for e in o["inputs"] + o["outputs"]])
    assert problems(repo) == []
    assert has(problems(repo, ready=True), "inputs[0] (tool 1).logical: is null")


# --- judgment, in the vocabularies of lib/judgment.py ---------------------------------------------------

def test_an_entrys_keys_are_a_list_of_its_own_columns(repo):
    edit(repo, lambda o: o["inputs"][0].__setitem__("keys", "ORDER_ID"))
    assert has(problems(repo), 'inputs[0] (tool 1).keys: is "ORDER_ID"; expected a list of its column names')
    edit(repo, lambda o: o["inputs"][0].__setitem__("keys", ["ZZZ"]))
    assert has(problems(repo), "inputs[0] (tool 1).keys: names ZZZ, which is not one of its columns; expected a "
                               "subset of ORDER_ID, CUSTOMER")
    edit(repo, lambda o: o["inputs"][0].pop("keys"))
    assert has(problems(repo), "inputs[0] (tool 1).keys: is missing; expected a list of its column names")
    edit(repo, lambda o: o["inputs"][0].__setitem__("keys", ["order_id"]))
    assert problems(repo) == [], "keys are compared to column names whatever their case"


def test_every_column_has_a_boolean_nullable(repo):
    edit(repo, lambda o: o["inputs"][0]["columns"][0].__setitem__("nullable", "yes"))
    assert has(problems(repo), 'inputs[0] (tool 1).columns[0] (ORDER_ID).nullable: is "yes"; expected true or false')
    edit(repo, lambda o: o["outputs"][0]["columns"][1].pop("nullable"))
    assert has(problems(repo), "outputs[0] (tool 7).columns[1] (SIZE_BAND).nullable: is missing; expected true or false")


def test_a_column_is_an_object_with_a_name(repo):
    edit(repo, lambda o: o["outputs"][0]["columns"].__setitem__(0, "REGION"))
    assert has(problems(repo), 'outputs[0] (tool 7).columns[0]: is "REGION"; expected a {name, type, nullable, meaning} object')


def test_every_output_column_says_what_it_means(repo):
    edit(repo, lambda o: o["outputs"][0]["columns"][0].__setitem__("meaning", ""))
    assert has(problems(repo), 'outputs[0] (tool 7).columns[0] (REGION).meaning: is ""; expected what the column '
                               "means, in words")
    edit(repo, lambda o: o["outputs"][0]["columns"][0].pop("meaning"))
    assert has(problems(repo), "outputs[0] (tool 7).columns[0] (REGION).meaning: is missing")


def test_an_output_has_a_grain(repo):
    edit(repo, lambda o: o["outputs"][1].__setitem__("grain", " "))
    assert has(problems(repo), 'outputs[1] (tool 8).grain: is " "; expected a non-empty sentence')


@pytest.mark.parametrize("value", ["weird", "1:n", None, 1])
def test_row_relation_is_one_of_the_vocabulary(repo, value):
    edit(repo, lambda o: o["outputs"][0].__setitem__("row_relation", value))
    assert has(problems(repo), 'outputs[0] (tool 7).row_relation: is ') and \
        has(problems(repo), 'expected one of "1:1", "filter", "aggregate", "expand"')


@pytest.mark.parametrize("value", ["1:1", "filter", "aggregate", "expand"])
def test_every_row_relation_the_contracts_allowed_is_allowed(repo, value):
    edit(repo, lambda o: o["outputs"][0].__setitem__("row_relation", value))
    assert problems(repo) == []


def test_ordering_has_exactly_keys_a_boolean_and_dependent_columns(repo):
    edit(repo, lambda o: o["outputs"][0].__setitem__("ordering", {"sort": ["A"]}))
    found = problems(repo)
    assert has(found, "outputs[0] (tool 7).ordering.keys: is missing; expected a list of column names")
    assert has(found, "outputs[0] (tool 7).ordering.alteryx_deterministic: is missing; expected true or false")
    assert has(found, "outputs[0] (tool 7).ordering.order_dependent_columns: is missing")
    assert has(found, "outputs[0] (tool 7).ordering.sort: is not an ordering field; expected only keys, "
                      "alteryx_deterministic and order_dependent_columns")
    edit(repo, lambda o: o["outputs"][0].__setitem__("ordering", "TOTAL_NET"))
    assert has(problems(repo), 'outputs[0] (tool 7).ordering: is "TOTAL_NET"; expected {"keys": [<column>...]')


def test_the_skeletons_undecided_ordering_is_a_problem(repo):
    edit(repo, lambda o: o["outputs"][0]["ordering"].__setitem__("alteryx_deterministic", None))
    assert has(problems(repo), "outputs[0] (tool 7).ordering.alteryx_deterministic: is null; expected true or false")


def test_ordering_keys_may_be_any_column_the_workflow_carries(repo):
    edit(repo, lambda o: o["outputs"][0]["ordering"].__setitem__("keys", ["CUSTOMER", "amount"]))
    assert problems(repo) == [], "an ordering key may be dropped before the output"
    edit(repo, lambda o: o["outputs"][0]["ordering"].__setitem__("keys", ["NOPE"]))
    assert has(problems(repo), "outputs[0] (tool 7).ordering.keys: names NOPE, which is not a column of wf_0001; "
                               "expected a column of its inputs, its tools' anchors or its outputs")


def test_order_dependent_columns_are_the_outputs_own(repo):
    edit(repo, lambda o: o["outputs"][0]["ordering"].__setitem__("order_dependent_columns", ["TOTAL_NET"]))
    assert problems(repo) == []
    edit(repo, lambda o: o["outputs"][0]["ordering"].__setitem__("order_dependent_columns", ["CUSTOMER"]))
    assert has(problems(repo), "outputs[0] (tool 7).ordering.order_dependent_columns: names CUSTOMER, which is not an "
                               "output column; expected one of REGION, SIZE_BAND, TOTAL_NET")


@pytest.mark.parametrize("tolerances, expected", [
    ("x", 'outputs[0] (tool 7).tolerances: is "x"; expected an object keyed by output column'),
    ({"NOPE": {"float_abs": 1}}, "outputs[0] (tool 7).tolerances.NOPE: names no output column; expected one of REGION"),
    ({"TOTAL_NET": {"float_abs": -1}}, 'outputs[0] (tool 7).tolerances.TOTAL_NET: is {"float_abs": -1}; expected {"float_abs"'),
    ({"TOTAL_NET": {"float_abs": True}}, "outputs[0] (tool 7).tolerances.TOTAL_NET: is"),
    ({"TOTAL_NET": {"abs": 1}}, "outputs[0] (tool 7).tolerances.TOTAL_NET: is"),
    ({"TOTAL_NET": 1}, "outputs[0] (tool 7).tolerances.TOTAL_NET: is 1; expected"),
])
def test_tolerances_are_keyed_by_output_column_with_float_abs_and_float_rel(repo, tolerances, expected):
    edit(repo, lambda o: o["outputs"][0].__setitem__("tolerances", tolerances))
    assert has(problems(repo), expected)


def test_tolerances_may_be_empty_and_may_carry_either_or_both(repo):
    for tolerances in ({}, {"TOTAL_NET": {"float_rel": 0.001}}, {"total_net": {"float_abs": 0, "float_rel": 1e-9}}):
        edit(repo, lambda o: o["outputs"][0].__setitem__("tolerances", tolerances))
        assert problems(repo) == []


@pytest.mark.parametrize("value, expected", [
    ("trim:REGION", None), ("upper:region", None), ("TRIM:Region", None),
    ("lower:REGION", 'outputs[0] (tool 7).normalizations[0]: is "lower:REGION"; expected "trim:<COLUMN>" or "upper:<COLUMN>"'),
    ("trim", "normalizations[0]: is \"trim\"; expected"),
    (5, "normalizations[0]: is 5; expected"),
    ("trim:NOPE", "outputs[0] (tool 7).normalizations[0]: names NOPE, which is not an output column; expected one of REGION"),
])
def test_normalizations_are_trim_or_upper_on_an_output_column(repo, value, expected):
    edit(repo, lambda o: o["outputs"][0].__setitem__("normalizations", [value]))
    found = problems(repo)
    assert (found == []) if expected is None else has(found, expected)


def test_normalizations_are_a_list(repo):
    edit(repo, lambda o: o["outputs"][0].__setitem__("normalizations", "trim:REGION"))
    assert has(problems(repo), 'outputs[0] (tool 7).normalizations: is "trim:REGION"; expected a list of')


# --- parity risks, rules, questions ----------------------------------------------------------------------

def test_a_parity_risk_is_exactly_a_tool_a_class_from_the_vocabulary_and_a_note(repo):
    edit(repo, lambda o: o["parity_risks"].append("x"))
    assert has(problems(repo), 'parity_risks[1]: is "x"; expected {"tool_id": "<a tool of wf_0001>"')
    edit(repo, lambda o: o["parity_risks"].pop())
    edit(repo, lambda o: o["parity_risks"][0].update(tool_id="99", **{"class": "NOPE"}, note=" ", extra=1))
    found = problems(repo)
    assert has(found, 'parity_risks[0].tool_id: is "99"; expected a tool of wf_0001: 1, 2, 3, 4, 5, 6, 7, 8')
    assert has(found, 'parity_risks[0].class: is "NOPE"; expected one of ROUNDING, ORDERING, NULL_SEMANTICS')
    assert has(found, 'parity_risks[0].note: is " "; expected a non-empty sentence')
    assert has(found, "parity_risks[0].extra: is not a parity-risk field; expected only tool_id, class and note")


def test_a_parity_risk_may_name_a_tool_inside_a_macro_or_a_data_less_one(repo):
    dag = read_json(repo.wf(WF, "parsed", "dag.json"))
    dag["nodes"].append({"tool_id": "60", "type": "macro", "config": {}, "in_anchors": [], "out_anchors": [],
                         "meta": {}, "sub_dag": {"nodes": [{"tool_id": "5", "type": "filter", "config": {},
                                                             "meta": {}}], "edges": []}})
    write_json(repo.wf(WF, "parsed", "dag.json"), dag)
    edit(repo, lambda o: (o["steps"][0]["tools"].append("60"),
                          o["parity_risks"].append({"tool_id": "60/5", "class": "LOGIC", "note": "inner filter"})))
    assert problems(repo) == []


def test_rules_are_non_empty_sentences(repo):
    edit(repo, lambda o: o["rules"].append(""))
    assert has(problems(repo), 'rules[1]: is ""; expected a non-empty sentence')
    edit(repo, lambda o: o["rules"].__setitem__(1, 5))
    assert has(problems(repo), "rules[1]: is 5; expected a non-empty sentence")


def _question(qid="O1", **over):
    question = {"id": qid, "tools": ["3"], "question": "Is WEST deliberate?", "assumption": "Deliberate."}
    question.update(over)
    return question


def test_an_open_question_is_valid_without_ready_and_blocks_ready(repo):
    edit(repo, lambda o: o["questions"].append(_question()))
    assert problems(repo) == []
    assert has(problems(repo, ready=True), "questions: 1 question is still open (O1); expected none: every question "
                                          "answered and moved to answered")
    edit(repo, lambda o: o["questions"].append(_question("O2")))
    assert has(problems(repo, ready=True), "questions: 2 questions are still open (O1, O2)")


@pytest.mark.parametrize("qid", ["o1", "O0", "O01", "Q1", "O", "O1\n", "1", 1, None])
def test_a_question_id_is_o_then_a_number_without_a_leading_zero(repo, qid):
    edit(repo, lambda o: o["questions"].append(_question(qid)))
    assert has(problems(repo), "questions[0].id: is ") and has(problems(repo), "expected O<N> such as O1")


def test_question_ids_are_unique_across_open_and_answered_questions(repo):
    edit(repo, lambda o: (o["questions"].append(_question("O1")),
                          o["answered"].append({"id": "O1", "question": "q", "answer": "a"})))
    assert has(problems(repo), 'answered[0].id: "O1" is used by more than one question; expected unique question ids')
    edit(repo, lambda o: o["questions"].append(_question("O1")))
    assert has(problems(repo), 'questions[1].id: "O1" is used by more than one question')


def test_a_question_has_tools_a_question_and_an_assumption(repo):
    edit(repo, lambda o: o["questions"].append(_question(tools=["99"], question="", assumption=5)))
    found = problems(repo)
    assert has(found, 'questions[0].tools[0]: is "99", not a tool of parsed/dag.json; expected a tool id of the DAG')
    assert has(found, 'questions[0].question: is ""; expected a non-empty string')
    assert has(found, "questions[0].assumption: is 5; expected a non-empty string")
    edit(repo, lambda o: o["questions"].__setitem__(0, "x"))
    assert has(problems(repo), 'questions[0]: is "x"; expected an object {id, tools, question, assumption}')


def test_an_answered_question_has_an_id_the_question_and_the_answer(repo):
    edit(repo, lambda o: o["answered"].append({"id": "O1", "question": "", "answer": None}))
    found = problems(repo)
    assert has(found, 'answered[0].question: is ""; expected a non-empty string')
    assert has(found, "answered[0].answer: is null; expected a non-empty string")
    assert problems(repo, ready=True) != [] and not has(problems(repo, ready=True), "still open")


# --- --ready ----------------------------------------------------------------------------------------------

def test_ready_needs_every_logical_name(repo):
    edit(repo, lambda o: o["outputs"][1].__setitem__("logical", None))
    found = problems(repo, ready=True)
    assert has(found, "outputs[1] (tool 8).logical: is null")
    assert has(found, "expected the logical name intake mapped to it")
    assert has(problems(repo), 'outputs[1] (tool 8).logical: is null; expected "EXCLUDED_ORDERS"'), \
        "a derivable name is mechanical with or without --ready"


# --- objective.md -----------------------------------------------------------------------------------------

def test_objective_md_is_checked_for_its_fixed_headings_when_it_exists(repo):
    path = repo.wf(WF, "objective.md")
    path.write_text(MD.replace("## Business rules\nx\n\n", ""), encoding="utf-8")
    assert has(problems(repo), "objective.md: heading '## Business rules' is missing; expected the fixed headings")
    path.write_text(MD.replace("# wf_0001 — Sales", "# wf_0002 — Sales"), encoding="utf-8")
    assert has(problems(repo), "objective.md: the first heading is '# wf_0002 — Sales summary by region and size band'; "
                               "expected '# wf_0001 — <title>'")
    path.write_text(MD.replace("## Purpose", "## Why"), encoding="utf-8")
    assert has(problems(repo), "objective.md: heading '## Purpose' is missing")
    path.unlink()
    assert problems(repo) == [], "a missing objective.md is the orchestrator's missing-output check, not this one"


def test_the_headings_must_come_in_the_fixed_order(repo):
    text = MD.replace("## Inputs\nx\n\n", "").replace("## Outputs", "## Inputs\nx\n\n## Outputs")
    repo.wf(WF, "objective.md").write_text(text, encoding="utf-8")
    assert problems(repo) == []
    swapped = MD.replace("## Steps", "## TMP").replace("## Outputs", "## Steps").replace("## TMP", "## Outputs")
    repo.wf(WF, "objective.md").write_text(swapped, encoding="utf-8")
    assert has(problems(repo), "objective.md: the headings are out of order")


# --- the command line ---------------------------------------------------------------------------------------

def test_exit_0_and_a_summary_when_it_passes(repo, capsys):
    assert oc.main([WF, "--root", str(repo.root)]) == 0
    assert capsys.readouterr().out.strip() == "wf_0001: objective passes every check"
    assert oc.main([WF, "--ready", "--root", str(repo.root)]) == 0
    assert "ready" in capsys.readouterr().out


def test_exit_1_prints_every_problem_on_stderr_one_per_line(repo, capsys):
    edit(repo, lambda o: o.update(title="", tier="T9"))
    assert oc.main([WF, "--root", str(repo.root)]) == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        "title: is \"\"; expected a non-empty string",
        'tier: is "T9"; expected one of "T1", "T2", "T3"']
    assert "2 problems" in captured.out


def test_a_missing_objective_is_exit_1(tmp_path, capsys):
    prepare_objective_workflow(tmp_path, WF, canned=False)
    assert oc.main([WF, "--root", str(tmp_path)]) == 1
    assert "objective.json: is missing" in capsys.readouterr().err


def test_without_a_parsed_dag_it_is_exit_2(tmp_path, capsys):
    assert oc.main([WF, "--root", str(tmp_path)]) == 2
    assert "parsed/dag.json" in capsys.readouterr().err


def test_a_workflow_id_that_is_not_one_is_exit_2(tmp_path):
    assert oc.main(["../x", "--root", str(tmp_path)]) == 2


def test_the_check_writes_nothing(repo):
    before = {p: p.read_bytes() for p in repo.wf(WF).rglob("*") if p.is_file()}
    edit(repo, lambda o: o.update(title=""))
    before[repo.wf(WF, "objective.json")] = repo.wf(WF, "objective.json").read_bytes()
    problems(repo, ready=True)
    oc.main([WF, "--ready", "--root", str(repo.root)])
    after = {p: p.read_bytes() for p in repo.wf(WF).rglob("*") if p.is_file()}
    assert after == before
