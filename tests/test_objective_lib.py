"""`lib/objective.py`: where a workflow's objective and build file are, and the two views other scripts
need of them -- the segment-contract shape `compare.py` still reads (`contract_view`) and the tool ids the
objective has to account for (`dag_tool_ids`). Design §4.2 and §4.8.

Nothing here has run against a real Snowflake account or Alteryx: the dags come from the seed samples'
`.yxmd` files through the parser.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

import compare as cmp
import parse
from lib import io, objective
from lib.backend import DuckDBBackend
from lib.paths import Repo
from lib.vocab import TARGET_WRITE_MODES

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _objective() -> dict:
    """A hand-written objective for a two-output workflow, in the shape of design §4.2."""
    return {
        "workflow": "wf_0001",
        "title": "Sales summary by region and size band",
        "purpose": "Totals each region's net order value by size band.",
        "tier": "T1",
        "language": "sql",
        "language_reason": None,
        "inputs": [{"tool_id": "1", "logical": "ORDERS",
                    "columns": [{"name": "ORDER_ID", "type": "NUMBER(38,0)", "nullable": True},
                                {"name": "REGION", "type": "VARCHAR(20)", "nullable": True}],
                    "keys": []}],
        "outputs": [
            {"tool_id": "7", "logical": "SALES_SUMMARY", "write_mode": "overwrite", "pre_sql": None,
             "post_sql": None, "grain": "one row per region",
             "columns": [{"name": "REGION", "type": "VARCHAR(20)", "nullable": False, "meaning": "region"},
                         {"name": "TOTAL_NET", "type": "FLOAT", "nullable": True, "meaning": "net value"}],
             "keys": ["REGION"], "row_relation": "aggregate",
             "ordering": {"keys": ["TOTAL_NET"], "alteryx_deterministic": True, "order_dependent_columns": []},
             "tolerances": {"TOTAL_NET": {"float_abs": 0.01}}, "normalizations": ["trim:REGION"]},
            {"tool_id": "8", "logical": "EXCLUDED_ORDERS", "write_mode": "append", "pre_sql": None,
             "post_sql": None, "grain": "one row per order",
             "columns": [{"name": "ORDER_ID", "type": "NUMBER(38,0)", "nullable": True}],
             "keys": [], "row_relation": "filter"},
        ],
        "steps": [{"id": "s1", "name": "Read", "does": "reads", "tools": ["1"], "feeds": ["out:7", "out:8"]}],
        "no_effect": [],
        "rules": [],
        "parity_risks": [{"tool_id": "3", "class": "NULL_SEMANTICS", "note": "NULL goes to the False side"}],
        "questions": [],
        "answered": [],
    }


# --- vocabularies -----------------------------------------------------------------------------

def test_the_vocabularies_are_the_ones_the_design_names():
    assert objective.WRITE_MODES == ("overwrite", "append", "truncate_append", "update_insert")
    assert objective.LANGUAGES == ("sql", "python")
    assert objective.TIERS == ("T1", "T2", "T3")


def test_the_write_modes_are_the_canonical_forms_the_pipeline_already_names():
    assert set(objective.WRITE_MODES) == set(TARGET_WRITE_MODES.values())


@pytest.mark.parametrize("text, ok", [("s1", True), ("s12", True), ("s100", True), ("s0", False), ("s01", False),
                                      ("S1", False), ("s", False), ("1", False), ("s1 ", False),
                                      ("s1\n", False), ("seg_01", False)])
def test_step_ids(text, ok):
    # `fullmatch`: the pattern's `$` would also accept a trailing newline under `match`.
    assert (objective.STEP_ID.fullmatch(text) is not None) is ok


@pytest.mark.parametrize("text, ok", [("O1", True), ("O12", True), ("O0", False), ("O01", False), ("o1", False),
                                      ("Q1", False), ("O", False), ("O1\n", False)])
def test_question_ids(text, ok):
    assert (objective.QUESTION_ID.fullmatch(text) is not None) is ok


# --- where things are, loading ----------------------------------------------------------------

def test_objective_path_is_under_the_workflow_folder(tmp_path):
    repo = Repo(tmp_path)
    assert objective.objective_path(repo, "wf_0001") == tmp_path.resolve() / "workflows" / "wf_0001" / "objective.json"


def test_load_reads_the_objective(tmp_path):
    repo = Repo(tmp_path)
    io.write_json(objective.objective_path(repo, "wf_0001"), _objective())
    assert objective.load(repo, "wf_0001") == _objective()


def test_load_of_a_missing_objective_is_file_not_found_naming_the_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="objective.json"):
        objective.load(Repo(tmp_path), "wf_0001")


@pytest.mark.parametrize("text", ["[1, 2]", '"text"', "null", "3"])
def test_load_of_something_that_is_not_a_json_object_is_a_value_error(tmp_path, text):
    repo = Repo(tmp_path)
    path = objective.objective_path(repo, "wf_0001")
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=r"objective\.json.*JSON object"):
        objective.load(repo, "wf_0001")


def test_load_of_invalid_json_is_a_value_error_naming_the_file(tmp_path):
    repo = Repo(tmp_path)
    path = objective.objective_path(repo, "wf_0001")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=r"objective\.json"):
        objective.load(repo, "wf_0001")


def test_build_path_is_none_until_a_build_file_exists(tmp_path):
    repo = Repo(tmp_path)
    repo.wf("wf_0001").mkdir(parents=True)
    assert objective.build_path(repo, "wf_0001") is None


@pytest.mark.parametrize("name", ["build.sql", "build.py"])
def test_build_path_is_the_build_file_that_exists(tmp_path, name):
    repo = Repo(tmp_path)
    (repo.wf("wf_0001") / name).parent.mkdir(parents=True)
    (repo.wf("wf_0001") / name).write_text("x", encoding="utf-8")
    assert objective.build_path(repo, "wf_0001") == repo.wf("wf_0001", name)


def test_build_path_refuses_two_build_files(tmp_path):
    repo = Repo(tmp_path)
    repo.wf("wf_0001").mkdir(parents=True)
    for name in ("build.sql", "build.py"):
        (repo.wf("wf_0001") / name).write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match=r"build\.sql.*build\.py"):
        objective.build_path(repo, "wf_0001")


# --- contract_view ----------------------------------------------------------------------------

def test_contract_view_is_the_segment_contract_shape_for_one_output():
    obj = _objective()
    output = obj["outputs"][0]
    view = objective.contract_view(obj, output)
    entry = {**output, "kind": "target", "stream": "SALES_SUMMARY"}
    assert view == {
        "workflow": "wf_0001", "segment": None, "target": "sql",
        "inputs": obj["inputs"], "outputs": [entry], "output": entry,
        "row_relation": "aggregate",
        "ordering": output["ordering"], "tolerances": {"TOTAL_NET": {"float_abs": 0.01}},
        "normalizations": ["trim:REGION"],
        "parity_risks": obj["parity_risks"],
    }


def test_contract_view_defaults_what_the_output_leaves_out():
    obj = _objective()
    view = objective.contract_view(obj, obj["outputs"][1])
    assert view["ordering"] == {} and view["tolerances"] == {} and view["normalizations"] == []
    assert view["row_relation"] == "filter"
    del obj["parity_risks"]
    assert objective.contract_view(obj, obj["outputs"][1])["parity_risks"] == []
    obj["outputs"][1]["ordering"] = None
    obj["outputs"][1]["tolerances"] = None
    obj["outputs"][1]["normalizations"] = None
    view = objective.contract_view(obj, obj["outputs"][1])
    assert (view["ordering"], view["tolerances"], view["normalizations"]) == ({}, {}, [])
    del obj["outputs"][1]["row_relation"]
    assert objective.contract_view(obj, obj["outputs"][1])["row_relation"] is None


def test_contract_view_keeps_a_kind_or_stream_the_output_already_has():
    obj = _objective()
    obj["outputs"][0].update({"kind": "work", "stream": "7_Output"})
    entry = objective.contract_view(obj, obj["outputs"][0])["output"]
    assert (entry["kind"], entry["stream"]) == ("work", "7_Output")


def test_contract_view_names_the_output_by_its_logical_when_it_has_no_stream():
    """`compare.py` says "keys not unique: <stream> declares keys ..." and every cluster is tagged with the
    stream, so a view whose stream is None would print 'the output' for every output of a workflow."""
    obj = _objective()
    assert "stream" not in obj["outputs"][0]
    assert objective.contract_view(obj, obj["outputs"][0])["output"]["stream"] == "SALES_SUMMARY"
    assert objective.contract_view(obj, obj["outputs"][1])["output"]["stream"] == obj["outputs"][1]["logical"]
    obj["outputs"][0]["stream"] = None
    assert objective.contract_view(obj, obj["outputs"][0])["outputs"][0]["stream"] == "SALES_SUMMARY"
    obj["outputs"][0]["logical"] = None            # intake has not named it yet: nothing to name it by
    assert objective.contract_view(obj, obj["outputs"][0])["output"]["stream"] is None


def test_contract_view_target_is_the_objectives_language():
    obj = _objective()
    obj.update({"language": "python", "language_reason": "Python tool"})
    assert objective.contract_view(obj, obj["outputs"][0])["target"] == "python"


def test_contract_view_does_not_share_state_with_the_objective():
    obj = _objective()
    before = copy.deepcopy(obj)
    view = objective.contract_view(obj, obj["outputs"][0])
    view["output"]["columns"].append({"name": "X"})
    view["inputs"][0]["columns"].clear()
    view["normalizations"].append("upper:REGION")
    view["parity_risks"].clear()
    assert obj == before


@pytest.mark.parametrize("missing", ["workflow", "language", "inputs"])
def test_contract_view_names_the_objective_field_it_needs(missing):
    obj = _objective()
    del obj[missing]
    with pytest.raises(ValueError, match=missing):
        objective.contract_view(obj, _objective()["outputs"][0])


def test_contract_view_has_every_key_compare_reads():
    """`compare.compare` reads `output`/`outputs[].stream`, `segment`, `tolerances`, `ordering` and
    `normalizations` from the contract, and `stream`, `write_mode`, `columns` and `keys` from the output."""
    obj = _objective()
    view = objective.contract_view(obj, obj["outputs"][0])
    assert {"output", "outputs", "segment", "tolerances", "ordering", "normalizations"} <= set(view)
    assert {"stream", "write_mode", "columns", "keys"} <= set(view["output"])
    assert cmp.select_output(view) is view["output"]
    assert cmp.select_output(view, None)["logical"] == "SALES_SUMMARY"
    assert view["segment"] is None


# compare.compare through the view, on the DuckDB double -------------------------------------------

_TOLERANCES = {"float_abs": 1e-6, "float_rel": 1e-9, "timestamp_precision": "milliseconds",
               "rounding": {"abs": 0.01}}
_FIELDS = [{"name": "REGION", "type": "V_String", "size": 20, "scale": None},
           {"name": "TOTAL_NET", "type": "Double", "size": 8, "scale": None}]


def _compare(view: dict, expected_rows: list, actual_rows: list) -> dict:
    backend = DuckDBBackend()
    backend.load_table("MIG_COMPARE.EXP", {"fields": _FIELDS, "rows": expected_rows})
    backend.load_table("MIG_WORK.ACT", {"fields": _FIELDS, "rows": actual_rows})
    return cmp.compare(backend, "MIG_COMPARE.EXP", "MIG_WORK.ACT", view, _TOLERANCES)


def test_compare_accepts_the_view_of_an_objective_output():
    obj = _objective()
    view = objective.contract_view(obj, obj["outputs"][0])
    report = _compare(view, [["EAST", 10.0], ["WEST", 20.0]], [["WEST", 20.0], ["EAST", 10.0]])
    assert report["verdict"] == "PASS" and report["segment"] is None
    assert report["checks"]["counts"] == {"expected": 2, "actual": 2, "verdict": "PASS"}


def test_compare_reads_the_outputs_columns_and_keys_through_the_view():
    obj = _objective()
    view = objective.contract_view(obj, obj["outputs"][0])
    report = _compare(view, [["EAST", 10.0], ["WEST", 20.0]], [["EAST", 10.0], ["WEST", 99.0]])
    (cluster,) = report["diff_clusters"]
    assert report["verdict"] == "FAIL" and cluster["columns"] == ["TOTAL_NET"]
    assert cluster["example_rows"][0]["key"] == {"REGION": "WEST"}


def test_compare_applies_the_outputs_normalizations_and_tolerances_through_the_view():
    obj = _objective()
    view = objective.contract_view(obj, obj["outputs"][0])
    report = _compare(view, [["EAST", 10.0]], [["EAST ", 10.004]])
    assert report["verdict"] == "PASS"
    assert report["normalizations_applied"] == ["trim:REGION"]
    # The same rows without the output's own normalization and tolerance are different.
    plain = copy.deepcopy(obj)
    plain["outputs"][0]["normalizations"] = []
    plain["outputs"][0]["tolerances"] = {}
    assert _compare(objective.contract_view(plain, plain["outputs"][0]),
                    [["EAST", 10.0]], [["EAST ", 10.004]])["verdict"] == "FAIL"


def test_compare_reads_the_outputs_order_dependent_columns_through_the_view():
    obj = _objective()
    obj["outputs"][0]["keys"] = ["TOTAL_NET"]
    obj["outputs"][0]["ordering"]["order_dependent_columns"] = ["REGION"]
    view = objective.contract_view(obj, obj["outputs"][0])
    swapped = _compare(view, [["EAST", 10.0], ["WEST", 20.0]], [["WEST", 10.0], ["EAST", 20.0]])
    assert [cluster["class"] for cluster in swapped["diff_clusters"]] == ["ORDERING"]
    # Without the declaration the same rows are a plain logic difference.
    obj["outputs"][0]["ordering"]["order_dependent_columns"] = []
    plain = _compare(objective.contract_view(obj, obj["outputs"][0]),
                     [["EAST", 10.0], ["WEST", 20.0]], [["WEST", 10.0], ["EAST", 20.0]])
    assert [cluster["class"] for cluster in plain["diff_clusters"]] == ["LOGIC"]


# --- dag_tool_ids -----------------------------------------------------------------------------

def _dag(workflow: str, document: str) -> dict:
    dag, _ = parse.parse_file(SAMPLES / workflow / "source" / document)
    return dag


def test_dag_tool_ids_of_wf_0002_is_every_node_in_dag_order_containers_and_comments_included():
    dag = _dag("wf_0002", "customer_orders.yxmd")
    assert objective.dag_tool_ids(dag) == ["100", "1", "2", "200", "210", "3", "4", "5", "6", "7", "8", "9",
                                           "10", "11", "300"]
    assert objective.dag_tool_ids(dag) == [node["tool_id"] for node in dag["nodes"]]


def test_dag_tool_ids_leaves_a_macros_internal_tools_out_and_keeps_the_macro_tool():
    dag = _dag("wf_0004", "inventory.yxmd")
    (macro,) = [node for node in dag["nodes"] if node["type"] == "macro"]
    internal = {node["tool_id"] for node in macro["sub_dag"]["nodes"]}
    assert "10" in internal                                   # the macro's own interface tool
    ids = objective.dag_tool_ids(dag)
    assert ids == ["1", "2", "3", "4", "5", "6", "7"]
    assert macro["tool_id"] in ids and "10" not in ids


@pytest.mark.parametrize("workflow, document", [
    ("wf_0001", "sales_summary.yxmd"), ("wf_0003", "gl_period_close.yxmd"), ("wf_0005", "vendor_dedupe.yxmd"),
    ("wf_0006", "subscription_revenue.yxmd"), ("wf_0007", "regional_targets.yxmd")])
def test_dag_tool_ids_are_unique_strings_for_every_sample(workflow, document):
    ids = objective.dag_tool_ids(_dag(workflow, document))
    assert ids and all(isinstance(tool_id, str) for tool_id in ids) and len(ids) == len(set(ids))


def test_dag_tool_ids_of_a_dag_with_no_nodes_is_empty():
    assert objective.dag_tool_ids({"nodes": []}) == []
    assert objective.dag_tool_ids({}) == []


def test_dag_tool_ids_names_a_node_without_an_id():
    with pytest.raises(ValueError, match=r"tool_id"):
        objective.dag_tool_ids({"nodes": [{"type": "filter"}]})
