"""`objective_scaffold.py`: the mechanical fields of a workflow's objective, derived by code
(objective-first design §4.2, "Mechanical").

An objective has two kinds of field. The MECHANICAL ones follow from files the pipeline already
wrote -- `parsed/dag.json` and `intake/mappings.yaml` -- and the scaffold derives them: the workflow id,
each input's tool id, logical name and columns (name and type from the parsed RecordInfo), each
output's tool id, logical name, write mode, PreSQL, PostSQL, columns and, for an update/insert, its
keys. The JUDGMENT ones are the analyzer's. `--prefill` writes a skeleton (mechanical fields filled,
judgment fields present and empty); `--apply` re-applies the mechanical fields over what the analyzer
wrote and leaves every judgment field exactly as written.

The derivations are the ones the per-segment contract scaffold always made, moved into `lib/mechanical.py`; the
tests below pin their results on the seed samples, and a synthetic workflow shows the shapes the samples
do not.

Nothing here has run on Snowflake or Alteryx: every file is built locally from `samples/`.
"""
from __future__ import annotations

import copy
import json

import pytest

import objective_scaffold as osc
from lib.io import read_json, read_yaml, write_json, write_yaml
from lib.paths import Repo
from tests.helpers import prepare_objective_workflow


def _col(name, type_, meaning=None):
    column = {"name": name, "type": type_, "nullable": True}
    if meaning is not None:
        column["meaning"] = meaning
    return column


WF_0001_INPUT_COLUMNS = [_col("ORDER_ID", "NUMBER(38,0)"), _col("CUSTOMER", "VARCHAR(50)"),
                         _col("REGION", "VARCHAR(20)"), _col("AMOUNT_TXT", "VARCHAR(20)"),
                         _col("QTY", "NUMBER(38,0)"), _col("ORDER_DATE", "DATE"), _col("STATUS", "VARCHAR(10)")]
WF_0001_SUMMARY_COLUMNS = [_col("REGION", "VARCHAR(20)", ""), _col("SIZE_BAND", "VARCHAR(10)", ""),
                           _col("TOTAL_NET", "FLOAT", ""), _col("ORDERS", "NUMBER(38,0)", ""),
                           _col("PRICED_ORDERS", "NUMBER(38,0)", ""), _col("LAST_ORDER", "DATE", "")]
WF_0001_EXCLUDED_COLUMNS = [_col("CUSTOMER", "VARCHAR(10)", ""), _col("ORDER_STATUS", "VARCHAR(10)", ""),
                            _col("ORDER_ID", "NUMBER(38,0)", ""), _col("REGION", "VARCHAR(20)", ""),
                            _col("AMOUNT_TXT", "VARCHAR(20)", ""), _col("QTY", "NUMBER(38,0)", ""),
                            _col("ORDER_DATE", "DATE", "")]


def _empty_judgment() -> dict:
    return {"grain": "", "row_relation": None,
            "ordering": {"keys": [], "alteryx_deterministic": None, "order_dependent_columns": []},
            "tolerances": {}, "normalizations": []}


WF_0001_SKELETON = {
    "workflow": "wf_0001",
    "title": "", "purpose": "", "tier": None, "language": None, "language_reason": None,
    "inputs": [{"tool_id": "1", "logical": "ORDERS", "columns": WF_0001_INPUT_COLUMNS, "keys": []}],
    "outputs": [
        {"tool_id": "7", "logical": "SALES_SUMMARY", "write_mode": "overwrite", "pre_sql": None, "post_sql": None,
         "grain": "", "columns": WF_0001_SUMMARY_COLUMNS, "keys": [], "row_relation": None,
         "ordering": _empty_judgment()["ordering"], "tolerances": {}, "normalizations": []},
        {"tool_id": "8", "logical": "EXCLUDED_ORDERS", "write_mode": "overwrite", "pre_sql": None, "post_sql": None,
         "grain": "", "columns": WF_0001_EXCLUDED_COLUMNS, "keys": [], "row_relation": None,
         "ordering": _empty_judgment()["ordering"], "tolerances": {}, "normalizations": []},
    ],
    "steps": [], "no_effect": [], "rules": [], "parity_risks": [], "questions": [], "answered": [],
}


@pytest.fixture
def wf1(tmp_path) -> Repo:
    """wf_0001 parsed, with intake READY from the sample's own answers and no objective yet."""
    return prepare_objective_workflow(tmp_path, "wf_0001", canned=False)


def objective_of(repo: Repo, wf_id: str = "wf_0001") -> dict:
    return read_json(repo.wf(wf_id, "objective.json"))


def analyzer_edit(repo: Repo, edit, wf_id: str = "wf_0001") -> dict:
    """Prefill, let `edit(objective)` play the analyzer, write the result back; returns it."""
    osc.prefill(repo, wf_id)
    objective = objective_of(repo, wf_id)
    edit(objective)
    write_json(repo.wf(wf_id, "objective.json"), objective)
    return objective


# --- --prefill -------------------------------------------------------------------------------------

def test_prefill_writes_the_wf_0001_skeleton_with_every_mechanical_field_filled(wf1):
    assert osc.prefill(wf1, "wf_0001") is True
    assert objective_of(wf1) == WF_0001_SKELETON


def test_the_skeleton_has_every_judgment_field_present_and_empty(wf1):
    osc.prefill(wf1, "wf_0001")
    objective = objective_of(wf1)
    for key in ("title", "purpose"):
        assert objective[key] == ""
    assert objective["tier"] is None and objective["language"] is None and objective["language_reason"] is None
    for key in ("steps", "no_effect", "rules", "parity_risks", "questions", "answered"):
        assert objective[key] == []
    for entry in objective["inputs"] + objective["outputs"]:
        assert all(column["nullable"] is True for column in entry["columns"])
    for output in objective["outputs"]:
        assert output["grain"] == "" and output["row_relation"] is None
        assert output["tolerances"] == {} and output["normalizations"] == [] and output["keys"] == []
        assert all(column["meaning"] == "" for column in output["columns"])


def test_prefill_keeps_the_analyzers_objective_when_there_is_one(wf1):
    analyzer_edit(wf1, lambda o: o.update(title="Mine"))
    before = wf1.wf("wf_0001", "objective.json").read_bytes()
    assert osc.prefill(wf1, "wf_0001") is False
    assert wf1.wf("wf_0001", "objective.json").read_bytes() == before


def test_the_file_is_utf8_lf_json_indent_2_with_a_trailing_newline(wf1):
    osc.prefill(wf1, "wf_0001")
    raw = wf1.wf("wf_0001", "objective.json").read_bytes()
    assert raw.endswith(b"}\n") and b"\r" not in raw
    assert raw.decode("utf-8") == json.dumps(WF_0001_SKELETON, indent=2) + "\n"


def test_logical_is_null_until_intake_has_named_it_and_a_note_says_so(wf1, capsys):
    wf1.wf("wf_0001", "intake", "mappings.yaml").unlink()
    assert osc.main(["wf_0001", "--prefill", "--root", str(wf1.root)]) == 0
    objective = objective_of(wf1)
    assert objective["inputs"][0]["logical"] is None
    assert [o["logical"] for o in objective["outputs"]] == [None, None]
    assert [o["write_mode"] for o in objective["outputs"]] == ["overwrite", "overwrite"], \
        "without a mapping the Output tool's own write mode stands"
    err = capsys.readouterr().err
    assert "note: inputs[0] (tool 1)" in err and "maps no logical name" in err
    assert "note: outputs[1] (tool 8)" in err


def test_wf_0003_output_has_its_update_keys_pre_and_post_sql_and_merge_becomes_update_insert(tmp_path):
    repo = prepare_objective_workflow(tmp_path, "wf_0003", canned=False)
    osc.prefill(repo, "wf_0003")
    (output,) = objective_of(repo, "wf_0003")["outputs"]
    assert (output["tool_id"], output["logical"], output["write_mode"]) == ("10", "GL_SUMMARY", "update_insert")
    assert output["keys"] == ["ACCT", "PERIOD"]
    assert output["pre_sql"] == "DELETE FROM dbo.GL_SUMMARY WHERE PERIOD < '2020-01'"
    assert output["post_sql"] == "UPDATE dbo.GL_SUMMARY SET LOADED_FLAG = 'Y' WHERE LOADED_FLAG IS NULL"
    assert [(c["name"], c["type"]) for c in output["columns"]] == [
        ("ACCT", "VARCHAR(20)"), ("PERIOD", "VARCHAR(7)"), ("TOTAL", "NUMBER(19,2)"), ("CLOSING_BAL", "FLOAT"),
        ("ENTRIES", "NUMBER(38,0)"), ("HAS_PERIOD_END", "VARCHAR(1)")]


def test_an_input_tool_the_mapping_names_is_an_input_even_when_it_is_not_an_input_type(tmp_path):
    repo = prepare_objective_workflow(tmp_path, "wf_0001", canned=False)
    mappings = read_yaml(repo.wf("wf_0001", "intake", "mappings.yaml"))
    mappings["sources"]["x/y.yxdb"] = {"snowflake": "SALES.RAW.EXTRA", "logical": "EXTRA", "tool_ids": ["2"]}
    write_yaml(repo.wf("wf_0001", "intake", "mappings.yaml"), mappings)
    osc.prefill(repo, "wf_0001")
    assert [(i["tool_id"], i["logical"]) for i in objective_of(repo)["inputs"]] == [("1", "ORDERS"), ("2", "EXTRA")]


# --- --apply ---------------------------------------------------------------------------------------

def test_apply_restores_a_tampered_type_and_keeps_the_judgment_meaning(wf1):
    def edit(objective):
        objective["outputs"][0]["columns"][2]["meaning"] = "net order value, summed"
        objective["outputs"][0]["columns"][2]["type"] = "NUMBER(10,2)"
        objective["inputs"][0]["columns"][1]["type"] = "VARCHAR(5)"
    analyzer_edit(wf1, edit)
    assert osc.main(["wf_0001", "--apply", "--root", str(wf1.root)]) == 0
    objective = objective_of(wf1)
    assert objective["outputs"][0]["columns"][2] == _col("TOTAL_NET", "FLOAT", "net order value, summed")
    assert objective["inputs"][0]["columns"][1]["type"] == "VARCHAR(50)"


def test_apply_leaves_every_judgment_field_as_the_analyzer_wrote_it(wf1):
    judgment = {
        "title": "Sales summary", "purpose": "Totals.", "tier": "T1", "language": "sql",
        "steps": [{"id": "s1", "name": "Read", "does": "reads", "tools": ["1", "2"], "feeds": ["out:7"]}],
        "no_effect": [{"tool_id": "9", "reason": "Browse"}],
        "rules": ["WEST is excluded."],
        "parity_risks": [{"tool_id": "3", "class": "NULL_SEMANTICS", "note": "NULL goes to False"}],
        "questions": [{"id": "O1", "tools": ["3"], "question": "Why?", "assumption": "Because."}],
        "answered": [{"id": "O2", "question": "Q", "answer": "A"}],
        "anything_else": {"the": ["analyzer", "added"]},
    }
    per_output = {"grain": "one row per region and band", "row_relation": "aggregate",
                  "ordering": {"keys": ["TOTAL_NET"], "alteryx_deterministic": True, "order_dependent_columns": []},
                  "tolerances": {"TOTAL_NET": {"float_abs": 0.01}}, "normalizations": ["trim:REGION"],
                  "keys": ["REGION", "SIZE_BAND"]}

    def edit(objective):
        objective.update(copy.deepcopy(judgment))
        objective["outputs"][0].update(copy.deepcopy(per_output))
        objective["inputs"][0]["keys"] = ["ORDER_ID"]
        objective["inputs"][0]["columns"][0]["nullable"] = False
        objective["outputs"][0]["columns"][0].update(meaning="the region", nullable=False)
        objective["outputs"][0]["columns"][1]["type"] = "TAMPERED"
    analyzer_edit(wf1, edit)
    osc.apply_file(wf1, "wf_0001")
    objective = objective_of(wf1)
    for key, value in judgment.items():
        assert objective[key] == value, f"--apply changed the judgment field {key}"
    for key, value in per_output.items():
        assert objective["outputs"][0][key] == value
    assert objective["inputs"][0]["keys"] == ["ORDER_ID"]
    assert objective["inputs"][0]["columns"][0]["nullable"] is False
    assert objective["outputs"][0]["columns"][0]["meaning"] == "the region"
    assert objective["outputs"][0]["columns"][1]["type"] == "VARCHAR(10)", "the mechanical type is restored"


def test_apply_changes_nothing_when_nothing_mechanical_moved_and_does_not_rewrite_the_file(wf1):
    analyzer_edit(wf1, lambda o: o.update(title="Mine", rules=["r"]))
    path = wf1.wf("wf_0001", "objective.json")
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    assert osc.apply_file(wf1, "wf_0001") is False
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_apply_restores_a_column_the_analyzer_dropped_and_removes_one_it_added(wf1):
    def edit(objective):
        columns = objective["outputs"][0]["columns"]
        del columns[2]                                           # TOTAL_NET dropped
        columns[0]["meaning"] = "kept"
        columns.append({"name": "INVENTED", "type": "VARCHAR(5)", "nullable": True, "meaning": "x"})
    analyzer_edit(wf1, edit)
    osc.apply_file(wf1, "wf_0001")
    columns = objective_of(wf1)["outputs"][0]["columns"]
    assert [c["name"] for c in columns] == [c["name"] for c in WF_0001_SUMMARY_COLUMNS]
    assert columns[0]["meaning"] == "kept"
    assert columns[2] == _col("TOTAL_NET", "FLOAT", ""), "a restored column gets the neutral judgment fields"


def test_apply_puts_columns_back_in_the_dags_order(wf1):
    analyzer_edit(wf1, lambda o: o["outputs"][0]["columns"].reverse())
    osc.apply_file(wf1, "wf_0001")
    assert [c["name"] for c in objective_of(wf1)["outputs"][0]["columns"]] == [c["name"] for c in WF_0001_SUMMARY_COLUMNS]


def test_apply_matches_a_surviving_column_by_name_whatever_its_case(wf1):
    def edit(objective):
        objective["outputs"][0]["columns"][0]["name"] = "region"
        objective["outputs"][0]["columns"][0]["meaning"] = "kept"
    analyzer_edit(wf1, edit)
    osc.apply_file(wf1, "wf_0001")
    assert objective_of(wf1)["outputs"][0]["columns"][0] == _col("REGION", "VARCHAR(20)", "kept")


def test_apply_restores_a_dropped_output_and_removes_an_invented_one(wf1):
    def edit(objective):
        objective["outputs"][0]["grain"] = "kept"
        del objective["outputs"][1]
        objective["outputs"].append({"tool_id": "99", "logical": "NOPE", "columns": []})
        del objective["inputs"][0]
    analyzer_edit(wf1, edit)
    osc.apply_file(wf1, "wf_0001")
    objective = objective_of(wf1)
    assert [o["tool_id"] for o in objective["outputs"]] == ["7", "8"]
    assert objective["outputs"][0]["grain"] == "kept"
    assert objective["outputs"][1] == WF_0001_SKELETON["outputs"][1]
    assert objective["inputs"] == WF_0001_SKELETON["inputs"]


def test_apply_finds_an_output_whose_tool_id_the_analyzer_lost_by_its_logical_name(wf1):
    def edit(objective):
        del objective["outputs"][0]["tool_id"]
        objective["outputs"][0]["grain"] = "kept"
    analyzer_edit(wf1, edit)
    osc.apply_file(wf1, "wf_0001")
    assert objective_of(wf1)["outputs"][0]["tool_id"] == "7"
    assert objective_of(wf1)["outputs"][0]["grain"] == "kept"


def test_apply_fills_the_logical_names_once_intake_has_named_them(wf1):
    mappings = wf1.wf("wf_0001", "intake", "mappings.yaml")
    saved = mappings.read_bytes()
    mappings.unlink()
    osc.prefill(wf1, "wf_0001")
    assert objective_of(wf1)["inputs"][0]["logical"] is None
    mappings.write_bytes(saved)
    assert osc.apply_file(wf1, "wf_0001") is True
    objective = objective_of(wf1)
    assert objective["inputs"][0]["logical"] == "ORDERS"
    assert [o["logical"] for o in objective["outputs"]] == ["SALES_SUMMARY", "EXCLUDED_ORDERS"]


def test_apply_overwrites_a_logical_name_the_analyzer_made_up(wf1):
    analyzer_edit(wf1, lambda o: o["inputs"][0].update(logical="MY_GUESS"))
    osc.apply_file(wf1, "wf_0001")
    assert objective_of(wf1)["inputs"][0]["logical"] == "ORDERS"


def test_apply_restores_write_mode_pre_sql_and_post_sql_from_the_output_tool(tmp_path):
    repo = prepare_objective_workflow(tmp_path, "wf_0003", canned=False)
    osc.prefill(repo, "wf_0003")
    objective = objective_of(repo, "wf_0003")
    objective["outputs"][0].update(write_mode="overwrite", pre_sql=None, post_sql="DROP TABLE x", keys=["ACCT"])
    write_json(repo.wf("wf_0003", "objective.json"), objective)
    osc.apply_file(repo, "wf_0003")
    output = objective_of(repo, "wf_0003")["outputs"][0]
    assert output["write_mode"] == "update_insert" and output["keys"] == ["ACCT", "PERIOD"]
    assert output["pre_sql"].startswith("DELETE FROM") and output["post_sql"].startswith("UPDATE")


def test_an_overwrite_outputs_keys_belong_to_the_analyzer(wf1):
    analyzer_edit(wf1, lambda o: o["outputs"][0].update(keys=["REGION", "SIZE_BAND"]))
    osc.apply_file(wf1, "wf_0001")
    assert objective_of(wf1)["outputs"][0]["keys"] == ["REGION", "SIZE_BAND"]


def test_a_target_that_keeps_its_rows_may_carry_target_only_columns_after_the_streams_own(tmp_path):
    repo = prepare_objective_workflow(tmp_path, "wf_0003", canned=False)

    def edit(objective):
        objective["outputs"][0]["columns"].append(_col("LOADED_FLAG", "VARCHAR(1)", "set by the PostSQL"))
    analyzer_edit(repo, edit, "wf_0003")
    assert osc.apply_file(repo, "wf_0003") is False, "LOADED_FLAG after the stream's columns is kept"
    assert objective_of(repo, "wf_0003")["outputs"][0]["columns"][-1]["name"] == "LOADED_FLAG"


def test_an_overwrite_target_may_not(wf1):
    analyzer_edit(wf1, lambda o: o["outputs"][0]["columns"].append(_col("EXTRA", "VARCHAR(1)", "x")))
    assert osc.apply_file(wf1, "wf_0001") is True
    assert "EXTRA" not in [c["name"] for c in objective_of(wf1)["outputs"][0]["columns"]]


def test_apply_is_idempotent(wf1):
    analyzer_edit(wf1, lambda o: o["outputs"][0]["columns"].reverse())
    osc.apply_file(wf1, "wf_0001")
    assert osc.apply_file(wf1, "wf_0001") is False


# --- the CLI ---------------------------------------------------------------------------------------

def test_apply_without_an_objective_is_exit_1(wf1, capsys):
    assert osc.main(["wf_0001", "--apply", "--root", str(wf1.root)]) == 1
    assert "objective.json" in capsys.readouterr().err


def test_an_apply_failure_is_the_first_stderr_line_even_when_there_are_notes(wf1, capsys):
    """The orchestrator records stderr's first line as the park reason: a note ahead of it would hide it."""
    wf1.wf("wf_0001", "intake", "mappings.yaml").unlink()          # the derivation now carries notes
    assert osc.main(["wf_0001", "--apply", "--root", str(wf1.root)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err and "objective.json" in err[0] and not err[0].startswith("note:")
    assert not any(line.startswith("note:") for line in err), "notes belong to a success only"


def test_apply_leaves_a_dropped_nullable_dropped_for_the_checker_to_name(wf1):
    osc.prefill(wf1, "wf_0001")
    path = wf1.wf("wf_0001", "objective.json")
    objective = read_json(path)
    del objective["outputs"][0]["columns"][0]["nullable"]
    write_json(path, objective)
    assert osc.main(["wf_0001", "--apply", "--root", str(wf1.root)]) == 0
    assert "nullable" not in read_json(path)["outputs"][0]["columns"][0]


def test_apply_to_an_objective_that_is_not_a_json_object_is_exit_1_and_writes_nothing(wf1, capsys):
    path = wf1.wf("wf_0001", "objective.json")
    path.write_text("[1, 2]\n", encoding="utf-8")
    assert osc.main(["wf_0001", "--apply", "--root", str(wf1.root)]) == 1
    assert "objective.json" in capsys.readouterr().err.splitlines()[0]
    assert path.read_text(encoding="utf-8") == "[1, 2]\n"
    path.write_text("{not json", encoding="utf-8")
    assert osc.main(["wf_0001", "--apply", "--root", str(wf1.root)]) == 1


def test_a_mode_is_required_and_only_one(wf1):
    with pytest.raises(SystemExit) as none_given:
        osc.main(["wf_0001", "--root", str(wf1.root)])
    assert none_given.value.code == 2
    with pytest.raises(SystemExit) as both_given:
        osc.main(["wf_0001", "--prefill", "--apply", "--root", str(wf1.root)])
    assert both_given.value.code == 2


@pytest.mark.parametrize("mode", ["--prefill", "--apply"])
def test_without_a_parsed_dag_it_is_exit_2_and_nothing_is_written(tmp_path, mode, capsys):
    assert osc.main(["wf_0001", mode, "--root", str(tmp_path)]) == 2
    assert "parsed/dag.json" in capsys.readouterr().err
    assert not (tmp_path / "workflows").exists()


def test_a_workflow_id_that_is_not_one_is_exit_2(tmp_path):
    assert osc.main(["../x", "--prefill", "--root", str(tmp_path)]) == 2


def test_prefill_reports_what_it_did_on_stdout(wf1, capsys):
    assert osc.main(["wf_0001", "--prefill", "--root", str(wf1.root)]) == 0
    assert "wrote" in capsys.readouterr().out
    assert osc.main(["wf_0001", "--prefill", "--root", str(wf1.root)]) == 0
    assert "kept" in capsys.readouterr().out


# --- a synthetic workflow: every shape the samples do not show --------------------------------------

WF = "wf_0100"


def _field(name, alteryx_type, size=None, scale=None):
    return {"name": name, "type": alteryx_type, "size": size, "scale": scale}


def _synthetic(tmp_path, *, mappings=None, write_mode="append", feed=True) -> Repo:
    """input 1 (Int, V_String, FixedDecimal, an unmappable type) -> output 2; a mapped source 3 that is a
    select; a macro-less workflow otherwise."""
    fields = [_field("Id", "Int64", 8), _field("Name", "V_String", 30), _field("AMT", "FixedDecimal", 19, 2),
              _field("Shape", "SpatialObj", 100)]
    dag = {
        "workflow": WF, "yxmd_version": "2023.1", "engine": "AMP", "constants": {}, "file_kind": "workflow",
        "source_file": "x.yxmd",
        "nodes": [
            {"tool_id": "1", "type": "input", "config": {}, "in_anchors": [], "out_anchors": ["Output"],
             "meta": {"Output": fields}},
            {"tool_id": "2", "type": "output", "config": {"write_mode": write_mode, "keys": ["Id"],
                                                          "pre_sql": None, "post_sql": None},
             "in_anchors": ["Input"], "out_anchors": [], "meta": {"Output": fields}},
        ],
        "edges": ([{"src": "1", "src_anchor": "Output", "dst": "2", "dst_anchor": "Input", "dst_order": 1,
                    "wireless": False}] if feed else []),
    }
    repo = Repo(tmp_path)
    write_json(repo.wf(WF, "parsed", "dag.json"), dag)
    write_yaml(repo.wf(WF, "intake", "mappings.yaml"), mappings if mappings is not None else {
        "sources": {"a.yxdb": {"snowflake": "S.R.IN", "logical": "IN", "tool_ids": ["1"]}},
        "outputs": {"b.yxdb": {"snowflake": "A.C.OUT", "logical": "OUT", "tool_ids": ["2"], "mode": "append",
                               "keys": []}}})
    return repo


def test_a_variable_string_keeps_its_size_and_a_type_the_map_cannot_say_is_left_out_with_a_note(tmp_path, capsys):
    repo = _synthetic(tmp_path)
    assert osc.main([WF, "--prefill", "--root", str(tmp_path)]) == 0
    (inp,) = objective_of(repo, WF)["inputs"]
    assert [(c["name"], c.get("type")) for c in inp["columns"]] == [
        ("ID", "NUMBER(38,0)"), ("NAME", "VARCHAR(30)"), ("AMT", "NUMBER(19,2)"), ("SHAPE", None)]
    assert "type" not in inp["columns"][3]
    err = capsys.readouterr().err
    assert "note: " in err and "SHAPE" in err and "types_map" in err


def test_apply_keeps_the_analyzers_type_for_a_column_the_map_cannot_type(tmp_path):
    repo = _synthetic(tmp_path)
    osc.prefill(repo, WF)
    objective = objective_of(repo, WF)
    objective["inputs"][0]["columns"][3]["type"] = "GEOGRAPHY"
    write_json(repo.wf(WF, "objective.json"), objective)
    assert osc.apply_file(repo, WF) is False
    assert objective_of(repo, WF)["inputs"][0]["columns"][3]["type"] == "GEOGRAPHY"


def test_the_type_maps_unsized_varchar_is_accepted_for_a_variable_string(tmp_path):
    repo = _synthetic(tmp_path)
    osc.prefill(repo, WF)
    objective = objective_of(repo, WF)
    objective["inputs"][0]["columns"][1]["type"] = "VARCHAR"
    write_json(repo.wf(WF, "objective.json"), objective)
    assert osc.apply_file(repo, WF) is False
    assert objective_of(repo, WF)["inputs"][0]["columns"][1]["type"] == "VARCHAR"


def test_merge_in_the_mapping_is_update_insert_and_takes_its_keys_in_upper_case(tmp_path):
    repo = _synthetic(tmp_path, mappings={
        "sources": {"a.yxdb": {"snowflake": "S.R.IN", "logical": "IN", "tool_ids": ["1"]}},
        "outputs": {"b.yxdb": {"snowflake": "A.C.OUT", "logical": "OUT", "tool_ids": ["2"], "mode": "merge",
                               "keys": ["id", "Name"]}}})
    osc.prefill(repo, WF)
    (out,) = objective_of(repo, WF)["outputs"]
    assert (out["write_mode"], out["keys"]) == ("update_insert", ["ID", "NAME"])


def notes_of(repo: Repo) -> str:
    return "\n".join(osc.derive(repo, WF).notes)


def test_an_update_insert_with_no_keys_anywhere_is_noted_and_its_keys_are_left_to_the_analyzer(tmp_path):
    repo = _synthetic(tmp_path, mappings={
        "sources": {}, "outputs": {"b.yxdb": {"snowflake": "A.C.OUT", "logical": "OUT", "tool_ids": ["2"],
                                              "mode": "merge"}}})
    dag = read_json(repo.wf(WF, "parsed", "dag.json"))
    dag["nodes"][1]["config"]["keys"] = []
    write_json(repo.wf(WF, "parsed", "dag.json"), dag)
    assert "outputs[0] (tool 2): write mode update_insert, but neither" in notes_of(repo)
    osc.prefill(repo, WF)
    assert objective_of(repo, WF)["outputs"][0]["keys"] == []


def test_without_a_mapping_mode_the_output_tools_own_write_mode_stands(tmp_path):
    repo = _synthetic(tmp_path, write_mode="truncate_append", mappings={"sources": {}, "outputs": {}})
    osc.prefill(repo, WF)
    assert objective_of(repo, WF)["outputs"][0]["write_mode"] == "truncate_append"


def test_an_output_with_no_write_mode_anywhere_is_noted_and_apply_leaves_the_analyzers_value(tmp_path):
    repo = _synthetic(tmp_path, write_mode=None, mappings={"sources": {}, "outputs": {}})
    assert "outputs[0] (tool 2): neither intake/mappings.yaml nor the Output tool names a write mode" \
        in notes_of(repo)
    osc.prefill(repo, WF)
    objective = objective_of(repo, WF)
    assert objective["outputs"][0]["write_mode"] is None
    objective["outputs"][0]["write_mode"] = "append"
    write_json(repo.wf(WF, "objective.json"), objective)
    osc.apply_file(repo, WF)
    assert objective_of(repo, WF)["outputs"][0]["write_mode"] == "append"


def test_an_output_nothing_feeds_takes_its_own_columns_and_is_noted(tmp_path):
    repo = _synthetic(tmp_path, feed=False)
    assert "no edge feeds Output tool 2" in notes_of(repo)
    osc.prefill(repo, WF)
    assert [c["name"] for c in objective_of(repo, WF)["outputs"][0]["columns"]] == ["ID", "NAME", "AMT", "SHAPE"]


def test_an_anchor_with_no_field_list_gives_no_columns_a_note_and_apply_leaves_the_analyzers(tmp_path):
    repo = _synthetic(tmp_path)
    dag = read_json(repo.wf(WF, "parsed", "dag.json"))
    dag["nodes"][0]["meta"] = {}
    write_json(repo.wf(WF, "parsed", "dag.json"), dag)
    assert "inputs[0] (tool 1): the DAG has no field list for it" in notes_of(repo)
    osc.prefill(repo, WF)
    objective = objective_of(repo, WF)
    assert objective["inputs"][0]["columns"] == []
    objective["inputs"][0]["columns"] = [_col("MINE", "VARCHAR(1)")]
    write_json(repo.wf(WF, "objective.json"), objective)
    assert osc.apply_file(repo, WF) is False
    assert objective_of(repo, WF)["inputs"][0]["columns"] == [_col("MINE", "VARCHAR(1)")]
