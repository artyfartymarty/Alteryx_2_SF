"""`validate_build.py`: golden-data parity validation of a workflow's ONE build file (objective-first design §4.8).

Three layers:

* a small synthetic workflow (`wf_0009`: ITEMS in, ITEMS_OUT out) for every behaviour of the script itself -- the
  report shape, a wrong build, a compile or runtime error, an output the build never writes, idempotency, the
  `needs_human` rule and the advisories that come through `compare.py`, approvals, usage errors that write
  nothing, the exit codes, a Python build in the Snowpark sandbox, and `--backend snowflake` against the fakes
  of `tests/fake_snowflake.py`;
* the seven samples: every canned build PASSes every golden set of its sample, `wf_0005` (tier T3) has nothing to
  validate;
* `samples/<wf>/broken/`: every broken build FAILs with the cluster its `broken.json` names.

Nothing here has run against a real Snowflake account or Alteryx: locally "parity" means DuckDB (or the Snowpark
Local Testing Framework) agrees with the Alteryx simulator.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import compile_check
import validate_build as vb
from lib import snowflake_conn, snowpark_sandbox
from lib.io import read_json, save_manifest, write_json, write_yaml
from lib.paths import Repo
from lib.typed_csv import write_table
from tests import build_fixtures as bf
from tests.fake_snowflake import FakeConnectorModule, FakeSession
from tests.helpers import SAMPLES, prepare_objective_workflow

ROOT = Path(__file__).resolve().parents[1]
WF = bf.WF

FIELDS = [{"name": "ID", "type": "Int32", "size": 4, "scale": None},
          {"name": "NOTE", "type": "V_String", "size": 20, "scale": None}]
SCORE = {"name": "SCORE", "type": "Double", "size": 8, "scale": None}
#: golden input rows by set; the golden output is the rows with a NOTE (`kept`)
SETS = {"normal": [[1, "keep"], [2, None], [3, "x"]], "edge": [[4, "y"], [5, None], [6, ""]], "empty": []}


def kept(rows):
    return [row for row in rows if row[1] is not None]


def make(root, build=None, *, name="build.sql", obj=None, sets=None, expected=kept, fields=FIELDS,
         out_fields=None, mappings=True, manifest=True) -> Repo:
    """A tmp `wf_0009`: objective, build file, `intake/mappings.yaml`, golden inputs and outputs for every set, and
    a manifest naming them. `expected` turns a set's input rows into its golden output rows."""
    repo = bf.make(root, build, obj, name=name)
    sets = SETS if sets is None else sets
    for golden_set, rows in sets.items():
        write_table(repo.wf(WF, "golden", "inputs", golden_set, "1.csv"), {"fields": fields, "rows": rows})
        write_table(repo.wf(WF, "golden", "outputs", golden_set, "7.csv"),
                    {"fields": out_fields or fields, "rows": expected(rows)})
    if mappings:
        write_yaml(repo.wf(WF, "intake", "mappings.yaml"), {
            "sources": {"in/items.yxdb": {"snowflake": "SALES.RAW.ITEMS", "logical": "ITEMS", "tool_ids": ["1"]}},
            "outputs": {"out/items_out.yxdb": {"snowflake": "SALES.CURATED.ITEMS_OUT", "logical": "ITEMS_OUT",
                                               "mode": "overwrite", "keys": [], "tool_ids": ["7"]}}})
    if manifest:
        save_manifest(repo, {"id": WF, "status": {}, "metrics": {}, "golden_sets": list(sets)})
    return repo


def run(repo: Repo, *args: str) -> int:
    return vb.main([WF, "--root", str(repo.root), *args])


def report(repo: Repo, name: str = "validation.json") -> dict:
    return read_json(repo.wf(WF, name))


def reports_on_disk(repo: Repo) -> list[str]:
    return sorted(path.name for path in repo.wf(WF).glob("validation*.json"))


def without_runtime(value: dict) -> dict:
    return {key: item for key, item in value.items() if key != "runtime_ms"}


# --- a correct build ------------------------------------------------------------------------------------------


def test_a_correct_build_passes_every_set_and_is_idempotent(tmp_path, capsys):
    repo = make(tmp_path)
    assert run(repo) == 0
    top = report(repo)
    assert top["verdict"] == "PASS" and top["needs_human"] is False and top["truncated"] is False
    assert top["sets"] == {"normal": "PASS", "edge": "PASS", "empty": "PASS"}
    assert top["idempotent"] is True and top["idempotency_diff"] == [] and top["language"] == "sql"
    assert reports_on_disk(repo) == ["validation.edge.json", "validation.empty.json", "validation.json",
                                     "validation.normal.json"]
    assert capsys.readouterr().out.strip() == (
        f"{WF}: PASS {{'normal': 'PASS', 'edge': 'PASS', 'empty': 'PASS'}} (idempotent=True)")


def test_the_report_is_the_segment_report_without_a_segment_and_with_a_language(tmp_path):
    repo = make(tmp_path)
    run(repo)
    one = report(repo, "validation.normal.json")
    assert "segment" not in one and "segment" not in report(repo)
    assert set(one) == {"golden_set", "language", "verdict", "checks", "diff_clusters", "normalizations_applied",
                        "idempotent", "idempotency_diff", "runtime_ms", "credits", "needs_human", "truncated"}
    assert one["golden_set"] == "normal" and one["language"] == "sql" and one["credits"] is None
    assert set(one["checks"]) == {"ITEMS_OUT:target"}                  # keyed by the output's logical name
    assert one["checks"]["ITEMS_OUT:target"]["schema"] == "PASS" and one["runtime_ms"] >= 0
    top = report(repo)
    assert set(top) == set(one) | {"sets"}
    assert {key: value for key, value in top.items() if key not in ("sets", "runtime_ms")} == {
        key: value for key, value in one.items() if key != "runtime_ms"}      # the body is the first set's


def test_set_names_only_the_sets_given_and_leaves_no_report_for_the_others(tmp_path):
    repo = make(tmp_path)
    assert run(repo, "--set", "edge", "--set", "normal", "--set", "edge") == 0
    assert report(repo)["sets"] == {"edge": "PASS", "normal": "PASS"}
    assert reports_on_disk(repo) == ["validation.edge.json", "validation.json", "validation.normal.json"]


def test_the_golden_set_is_loaded_the_way_a_run_would_see_it(tmp_path):
    """`{{src}}` reads the view schema by logical name; a build that reads a table nobody mapped fails."""
    repo = make(tmp_path, bf.GOOD_SQL.replace("{{src}}.ITEMS", "{{src}}.NOT_MAPPED"))
    assert run(repo) == 1
    assert "NOT_MAPPED" in report(repo)["error"]


# --- a wrong build, an error, an output that is never written -----------------------------------------------------


def test_a_wrong_filter_fails_and_names_the_output_the_column_and_the_class(tmp_path, capsys):
    repo = make(tmp_path, bf.GOOD_SQL.replace("WHERE NOTE IS NOT NULL", "WHERE ID <> 3"))
    assert run(repo) == 1
    top = report(repo)
    assert top["verdict"] == "FAIL" and top["sets"]["normal"] == "FAIL" and top["sets"]["empty"] == "PASS"
    clusters = report(repo, "validation.normal.json")["diff_clusters"]
    assert {c["stream"] for c in clusters} == {"ITEMS_OUT"}                 # every cluster names its output
    assert ("NULL_SEMANTICS", ["NOTE"]) in [(c["class"], c["columns"]) for c in clusters]
    assert capsys.readouterr().out.startswith(f"{WF}: FAIL ")


def test_a_compile_error_is_that_sets_fail_report_not_a_usage_error(tmp_path, capsys):
    repo = make(tmp_path, bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept", "SELECT ID, NOTE FROM kept WHERE ((("))
    assert run(repo) == 1
    top = report(repo)
    assert top["verdict"] == "FAIL" and set(top["sets"].values()) == {"FAIL"}
    one = report(repo, "validation.normal.json")
    assert "cannot parse" in one["error"] and "line " in one["error"] and one["diff_clusters"] == []
    assert one["needs_human"] is False and one["idempotent"] is None and top["idempotent"] is None
    assert "cannot parse" in capsys.readouterr().err


def test_a_runtime_error_names_the_line_of_the_statement(tmp_path):
    repo = make(tmp_path, bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept", "SELECT ID, NO_SUCH_COLUMN FROM kept"))
    assert run(repo) == 1
    error = report(repo, "validation.edge.json")["error"]
    assert error.startswith("line 10: ") and "NO_SUCH_COLUMN" in error.upper()     # the CREATE of ITEMS_OUT


def test_an_output_the_build_never_writes_is_a_fail_naming_the_table(tmp_path):
    repo = make(tmp_path, bf.GOOD_SQL.replace("CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS",
                                              "CREATE OR REPLACE TEMPORARY TABLE not_the_output AS"))
    assert run(repo) == 1
    one = report(repo, "validation.normal.json")
    assert one["verdict"] == "FAIL" and one["needs_human"] is False
    assert one["error"] == "output table MIGDB.MIG_WORK.ITEMS_OUT does not exist after running the build file"
    assert one["idempotent"] is False and one["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    assert report(repo)["error"] == one["error"]


def test_a_build_of_the_wrong_language_is_a_fail_on_every_set(tmp_path):
    repo = make(tmp_path, "def main(session, src, tgt):\n    return 'OK'\n", name="build.py")
    assert run(repo) == 1
    top = report(repo)
    assert top["sets"] == {"normal": "FAIL", "edge": "FAIL", "empty": "FAIL"} and top["language"] == "python"
    assert "build.py is a python build but objective.json says language 'sql'" in top["error"]
    assert "write build.sql and remove build.py" in top["error"]


def test_a_statement_that_reads_the_hosts_files_is_refused_by_the_locked_connection(tmp_path):
    secret = tmp_path / "host_secret.csv"
    secret.write_text("ID,NOTE\n1,leak\n", encoding="utf-8")
    sql = bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;",
                              f"SELECT ID, NOTE FROM read_csv_auto('{secret.as_posix()}');")
    repo = make(tmp_path / "wf", sql)
    assert run(repo) == 1
    one = report(repo, "validation.normal.json")
    assert one["verdict"] == "FAIL"
    # the report says why: the connection refused the access (an unlocked one would read the file and fail on a
    # difference of rows instead, with no `error` at all)
    assert any(sign in one.get("error", "") for sign in ("disabled by configuration", "Permission Error")), one
    assert "host_secret.csv" in one["error"] and "leak" not in one["error"]


# --- idempotency ------------------------------------------------------------------------------------------------


SCORED = bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT ID, NOTE, CAST(ID AS DOUBLE) + (ABS(RANDOM()) % 100) * 0.0001 AS SCORE FROM kept;")


def scored_objective() -> dict:
    obj = bf.objective()
    obj["outputs"][0]["columns"].append({"name": "SCORE", "type": "FLOAT", "nullable": True})
    obj["outputs"][0]["tolerances"] = {"SCORE": {"float_abs": 0.01}}      # RANDOM()'s noise is within it
    return obj


def scored(root, build: str) -> Repo:
    return make(root, build, obj=scored_objective(), out_fields=[*FIELDS, SCORE],
                expected=lambda rows: [[row[0], row[1], float(row[0])] for row in kept(rows)])


def test_a_deterministic_build_with_a_float_column_is_idempotent(tmp_path):
    repo = scored(tmp_path, SCORED.replace(" + (ABS(RANDOM()) % 100) * 0.0001", ""))
    assert run(repo) == 0 and report(repo)["idempotent"] is True


def test_a_build_that_depends_on_input_row_order_fails_idempotency(tmp_path):
    """The re-run loads every input in reversed row order: a window with no ORDER BY numbers the rows
    differently, though each run's own values are within tolerance of the golden output."""
    repo = scored(tmp_path, SCORED.replace(" + (ABS(RANDOM()) % 100) * 0.0001", " + ROW_NUMBER() OVER () * 0.0001"))
    assert run(repo) == 1
    one = report(repo, "validation.normal.json")
    assert one["diff_clusters"] == [] and one["idempotent"] is False
    assert one["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]


def test_random_in_a_build_fails_idempotency_though_every_value_is_within_tolerance(tmp_path):
    repo = scored(tmp_path, SCORED)
    assert run(repo) == 1
    top = report(repo)
    one = report(repo, "validation.normal.json")
    assert one["diff_clusters"] == [] and one["checks"]["ITEMS_OUT:target"]["schema"] == "PASS"   # compare() saw no diff
    assert one["verdict"] == "FAIL" and one["idempotent"] is False
    assert one["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    # every set is re-run on its own: the set with rows differs again, the empty one has nothing to differ in
    assert top["idempotent"] is False and top["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    assert report(repo, "validation.edge.json")["idempotent"] is False
    assert report(repo, "validation.edge.json")["verdict"] == "FAIL"
    assert report(repo, "validation.empty.json")["idempotent"] is True
    assert report(repo, "validation.empty.json")["verdict"] == "PASS"
    assert top["sets"] == {"normal": "FAIL", "edge": "FAIL", "empty": "PASS"}


def test_idempotency_is_checked_on_every_set_not_only_the_first(tmp_path):
    """A `LIMIT 1` with no ORDER BY is deterministic on a one-row set and order-dependent on the next one."""
    first_only = bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT ID, NOTE FROM kept LIMIT 1;")
    sets = {"single": [[1, "a"]], "several": [[1, "a"], [2, "b"], [3, "c"], [4, "d"]]}
    repo = make(tmp_path, first_only, sets=sets, expected=lambda rows: rows[:1])
    assert run(repo) == 1
    top = report(repo)
    assert report(repo, "validation.single.json")["idempotent"] is True
    assert report(repo, "validation.single.json")["verdict"] == "PASS"
    several = report(repo, "validation.several.json")
    assert several["idempotent"] is False and several["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    assert several["verdict"] == "FAIL"
    assert top["idempotent"] is False and top["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    assert top["sets"] == {"single": "PASS", "several": "FAIL"}


def test_an_append_build_is_not_mistaken_for_non_idempotent(tmp_path):
    """Each run starts from the same state in its own fresh backend, so the rows an `append` adds on top of
    `targets_before` are the same both times; running twice without a reset is never the test."""
    obj = bf.objective(write_mode="append")
    sql = bf.GOOD_SQL.replace("CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS\nSELECT ID, NOTE FROM kept;",
                              "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\nSELECT ID, NOTE FROM kept;")
    repo = make(tmp_path, sql, obj=obj, expected=lambda rows: [[0, "before"], *kept(rows)])
    for golden_set in SETS:
        write_table(repo.wf(WF, "golden", "targets_before", golden_set, "ITEMS_OUT.csv"),
                    {"fields": FIELDS, "rows": [[0, "before"]]})
    assert run(repo) == 0
    top = report(repo)
    assert top["sets"] == {"normal": "PASS", "edge": "PASS", "empty": "PASS"} and top["idempotent"] is True


# --- what compare.py says comes through unchanged ----------------------------------------------------------------


MERGE_SQL = bf.GOOD_SQL.replace(
    "CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS\nSELECT ID, NOTE FROM kept;",
    "MERGE INTO {{tgt}}.ITEMS_OUT AS T USING kept AS S ON T.ID = S.ID\n"
    "WHEN MATCHED THEN UPDATE SET NOTE = S.NOTE\n"
    "WHEN NOT MATCHED THEN INSERT (ID, NOTE) VALUES (S.ID, S.NOTE);")


def test_needs_human_comes_through_from_compare_even_on_a_pass(tmp_path):
    """The keys of an `update_insert` output are its MERGE keys, and a MERGE on a key that repeats fails on
    Snowflake. When the golden rows repeat them `compare.py` compares the output keyless, passes it, and asks for a
    human to fix the objective's keys: the script passes both on, per set and on top, and the exit code is 0."""
    sets = {"normal": [[1, "a"], [2, "b"]]}
    repo = make(tmp_path, MERGE_SQL, obj=bf.objective(write_mode="update_insert"), sets=sets,
                expected=lambda rows: [[9, "z"], [9, "z"], *kept(rows)])
    write_table(repo.wf(WF, "golden", "targets_before", "normal", "ITEMS_OUT.csv"), {"fields": FIELDS, "rows": [[9, "z"], [9, "z"]]})
    assert run(repo) == 0
    one = report(repo, "validation.normal.json")
    assert one["verdict"] == "PASS" and one["needs_human"] is True and report(repo)["needs_human"] is True
    assert "MERGE keys" in one["checks"]["ITEMS_OUT:target"]["keys_not_unique"]["note"]


def test_the_nullability_advisory_comes_through_and_does_not_fail(tmp_path):
    """The golden rows are the reference: a NULL in a column the objective calls NOT NULL means the analyzer's
    nullability judgment is wrong, not the data, so `compare.py` says so as an advisory and still compares every row."""
    obj = bf.objective()
    obj["outputs"][0]["columns"][1]["nullable"] = False
    repo = make(tmp_path, bf.GOOD_SQL.replace(" WHERE NOTE IS NOT NULL", ""), obj=obj, sets={"normal": SETS["normal"]},
                expected=lambda rows: rows)
    assert run(repo) == 0
    assert report(repo)["checks"]["ITEMS_OUT:target"]["nullability_contract"]["columns"] == ["NOTE"]
    assert report(repo)["verdict"] == "PASS"


def test_a_null_in_a_not_null_column_of_the_actual_output_is_a_fail(tmp_path):
    obj = bf.objective()
    obj["outputs"][0]["columns"][1]["nullable"] = False
    repo = make(tmp_path, bf.GOOD_SQL.replace(" WHERE NOTE IS NOT NULL", ""), obj=obj, sets={"normal": SETS["normal"]})
    assert run(repo) == 1
    assert ("NULL_SEMANTICS", ["NOTE"]) in [(c["class"], c["columns"]) for c in report(repo, "validation.normal.json")["diff_clusters"]]


def test_keys_not_unique_advisory_comes_through_and_does_not_fail(tmp_path):
    rows = [[1, "a"], [1, "a"], [2, "b"]]
    repo = make(tmp_path, sets={"normal": rows})
    assert run(repo) == 0
    advisory = report(repo)["checks"]["ITEMS_OUT:target"]["keys_not_unique"]
    assert advisory["keys"] == ["ID"] and advisory["duplicate_groups"] == 1
    assert report(repo)["verdict"] == "PASS" and report(repo)["needs_human"] is False


def _rounding_repo(tmp_path, *, approvals, classes=("ROUNDING",)) -> Repo:
    sql = bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT ID, NOTE, CAST(ID AS DOUBLE) + 0.005 AS SCORE FROM kept;")
    obj = bf.objective()
    obj["outputs"][0]["columns"].append({"name": "SCORE", "type": "FLOAT", "nullable": True})
    repo = make(tmp_path, sql, obj=obj, out_fields=[*FIELDS, SCORE], sets={"normal": SETS["normal"]},
                expected=lambda rows: [[row[0], row[1], float(row[0])] for row in kept(rows)])
    write_yaml(repo.global_mappings, {"accepted_diff_classes": list(classes)})
    manifest = read_json(repo.wf(WF, "manifest.json"))
    manifest["accepted_diffs"] = approvals
    write_json(repo.wf(WF, "manifest.json"), manifest)
    return repo


def test_an_approved_class_over_the_columns_passes_with_an_accepted_diff(tmp_path):
    repo = _rounding_repo(tmp_path, approvals=[{"class": "ROUNDING", "columns": ["SCORE"], "approver": "t", "date": "2026-10-06"}])
    assert run(repo) == 0
    assert report(repo)["verdict"] == "PASS_WITH_ACCEPTED_DIFF"
    assert [c["class"] for c in report(repo)["diff_clusters"]] == ["ROUNDING"]


def test_an_unapproved_diff_or_one_approved_for_a_retired_segment_is_a_fail(tmp_path):
    """Nothing is a segment any more: an approval that names one (a manifest from before the rewrite) does not
    cover a workflow-level comparison, so it cannot quietly silence a diff."""
    repo = _rounding_repo(tmp_path / "none", approvals=[])
    assert run(repo) == 1 and report(repo)["verdict"] == "FAIL"
    repo = _rounding_repo(tmp_path / "segment", approvals=[{"segment": "seg_01", "class": "ROUNDING", "columns": ["SCORE"]}])
    assert run(repo) == 1 and report(repo)["verdict"] == "FAIL"
    repo = _rounding_repo(tmp_path / "class", classes=(), approvals=[{"class": "ROUNDING", "columns": ["SCORE"]}])
    assert run(repo) == 1


# --- usage errors: exit 2, nothing written ------------------------------------------------------------------------


def _drop(repo: Repo, *parts: str):
    repo.wf(WF, *parts).unlink()


def _t3(repo: Repo):
    obj = read_json(repo.wf(WF, "objective.json"))
    obj["tier"] = "T3"
    write_json(repo.wf(WF, "objective.json"), obj)


def _edit_objective(repo: Repo, change):
    obj = read_json(repo.wf(WF, "objective.json"))
    change(obj)
    write_json(repo.wf(WF, "objective.json"), obj)


USAGE = {
    "no objective": (lambda repo: _drop(repo, "objective.json"), "objective.json"),
    "tier T3": (_t3, "tier T3"),
    "no build file": (lambda repo: _drop(repo, "build.sql"), "no build file to validate"),
    "both build files": (lambda repo: repo.wf(WF, "build.py").write_text("x = 1\n", encoding="utf-8"), "both build.sql and build.py"),
    "no mappings": (lambda repo: _drop(repo, "intake", "mappings.yaml"), "intake/mappings.yaml"),
    "no outputs": (lambda repo: _edit_objective(repo, lambda obj: obj.update(outputs=[])), "no outputs[]"),
    "an output without a tool id": (lambda repo: _edit_objective(repo, lambda obj: obj["outputs"][0].pop("tool_id")), "tool_id"),
    "an output without a logical name": (lambda repo: _edit_objective(repo, lambda obj: obj["outputs"][0].update(logical=None)), "logical name"),
    "an output with a name that is not an identifier": (
        lambda repo: _edit_objective(repo, lambda obj: obj["outputs"][0].update(logical="items out")), "items out"),
    "a missing golden output": (lambda repo: _drop(repo, "golden", "outputs", "edge", "7.csv"), "golden output of tool 7"),
    "a missing golden input": (lambda repo: _drop(repo, "golden", "inputs", "edge", "1.csv"), "golden input for tool 1"),
    "no golden sets recorded": (lambda repo: write_json(repo.wf(WF, "manifest.json"), {"id": WF}), "no golden sets"),
}


@pytest.mark.parametrize("what", sorted(USAGE))
def test_a_missing_or_malformed_prerequisite_is_exit_2_and_writes_nothing(tmp_path, capsys, what):
    repo = make(tmp_path)
    mutate, mention = USAGE[what]
    mutate(repo)
    # a report from an earlier run must not survive a run that cannot validate anything
    for stale in ("validation.json", "validation.normal.json", "validation.old_set.json"):
        write_json(repo.wf(WF, stale), {"verdict": "PASS"})
    assert run(repo) == 2
    err = capsys.readouterr().err
    assert f"cannot validate {WF}: " in err and mention in err      # the message names the thing that is wrong
    assert reports_on_disk(repo) == []


def test_an_unknown_set_is_exit_2_naming_it_and_the_known_ones(tmp_path, capsys):
    repo = make(tmp_path)
    assert run(repo, "--set", "no_such_set") == 2
    err = capsys.readouterr().err
    assert "no golden set 'no_such_set'" in err and "normal, edge, empty" in err
    assert reports_on_disk(repo) == []


def test_snowflake_options_without_the_snowflake_backend_are_exit_2(tmp_path, capsys):
    repo = make(tmp_path)
    assert run(repo, "--connection", "sandbox") == 2
    assert "--backend snowflake" in capsys.readouterr().err and reports_on_disk(repo) == []


def test_an_unknown_workflow_is_exit_2(tmp_path, capsys):
    assert vb.main(["wf_9999", "--root", str(tmp_path)]) == 2
    assert "wf_9999" in capsys.readouterr().err


def test_an_unexpected_crash_is_exit_2_with_no_report(tmp_path, monkeypatch, capsys):
    repo = make(tmp_path)
    monkeypatch.setattr(vb, "_run_set", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the disk went away")))
    assert run(repo) == 2
    assert "RuntimeError" in capsys.readouterr().err and reports_on_disk(repo) == []


def test_the_script_runs_from_the_command_line_with_exit_codes_0_1_and_2(tmp_path):
    def cli(repo, *extra):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_build.py"), WF, "--root", str(repo.root), *extra],
                              capture_output=True, text=True, cwd=ROOT)
    good = cli(make(tmp_path / "good"))
    assert good.returncode == 0 and "PASS" in good.stdout and "Traceback" not in good.stderr
    wrong = cli(make(tmp_path / "wrong", bf.GOOD_SQL.replace("WHERE NOTE IS NOT NULL", "WHERE ID <> 3")))
    assert wrong.returncode == 1 and "FAIL" in wrong.stdout
    bad = cli(make(tmp_path / "bad"), "--set", "nope")
    assert bad.returncode == 2 and "Traceback" not in bad.stderr
    with_usage = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_build.py")], capture_output=True, text=True, cwd=ROOT)
    assert with_usage.returncode == 2


# --- a build.py, in the Snowpark Local Testing Framework inside the runtime sandbox ---------------------------------


GOOD_PY = '''# wf_0009 - Items. Written from objective.md.
from snowflake.snowpark.types import IntegerType, StringType, StructField, StructType


def main(session, src: str, tgt: str) -> str:
    # step s1
    pdf = session.table(f"{src}.ITEMS").to_pandas()
    pdf = pdf[pdf["NOTE"].notna()]
    # step s2
    rows = [[int(row["ID"]), row["NOTE"]] for _, row in pdf.iterrows()]
    schema = StructType([StructField("ID", IntegerType()), StructField("NOTE", StringType(20))])
    session.create_dataframe(rows, schema=schema).write.mode("overwrite").save_as_table(f"{tgt}.ITEMS_OUT")
    return "OK"
'''


def py(root, build: str = GOOD_PY, **kwargs) -> Repo:
    return make(root, build, name="build.py", obj=bf.objective(language="python"), **kwargs)


def test_a_python_build_passes_every_set_including_the_empty_one(tmp_path):
    repo = py(tmp_path)
    assert run(repo) == 0
    top = report(repo)
    assert top["verdict"] == "PASS" and top["language"] == "python" and top["idempotent"] is True
    assert top["sets"] == {"normal": "PASS", "edge": "PASS", "empty": "PASS"}
    assert report(repo, "validation.empty.json")["language"] == "python"


def test_a_wrong_python_build_fails_with_the_same_cluster_a_sql_one_would(tmp_path):
    repo = py(tmp_path, GOOD_PY.replace('pdf["NOTE"].notna()', 'pdf["ID"] != 3'), sets={"normal": SETS["normal"]})
    assert run(repo) == 1
    clusters = report(repo, "validation.normal.json")["diff_clusters"]
    assert ("NULL_SEMANTICS", ["NOTE"], "ITEMS_OUT") in [(c["class"], c["columns"], c["stream"]) for c in clusters]


#: the same filter, with a row number taken from the order the input rows arrive in (within the SCORE tolerance of the
#: golden output, whichever order it is)
ROW_ORDER_PY = GOOD_PY.replace(
    "IntegerType, StringType,", "DoubleType, IntegerType, StringType,").replace(
    '    rows = [[int(row["ID"]), row["NOTE"]] for _, row in pdf.iterrows()]',
    '    rows = [[int(row["ID"]), row["NOTE"], float(row["ID"]) + n * 0.0001]\n'
    '            for n, (_, row) in enumerate(pdf.iterrows(), start=1)]').replace(
    'StructField("NOTE", StringType(20))]', 'StructField("NOTE", StringType(20)), StructField("SCORE", DoubleType())]')


def test_a_python_build_that_depends_on_input_row_order_fails_idempotency(tmp_path):
    """The re-run loads every input in reversed row order, as the SQL re-run does: a row number taken from the order
    the rows arrive in differs between the runs, though each run's own values are within tolerance of the golden
    output."""
    assert ROW_ORDER_PY != GOOD_PY and "enumerate" in ROW_ORDER_PY and "DoubleType()" in ROW_ORDER_PY
    repo = make(tmp_path, ROW_ORDER_PY, name="build.py", obj={**scored_objective(), "language": "python",
                                                              "language_reason": "row-sequential carry-over"},
                out_fields=[*FIELDS, SCORE], sets={"normal": SETS["normal"]},
                expected=lambda rows: [[row[0], row[1], float(row[0])] for row in kept(rows)])
    assert run(repo) == 1
    one = report(repo, "validation.normal.json")
    assert one["diff_clusters"] == [] and "error" not in one       # the first run is right: only the re-run differs
    assert one["idempotent"] is False and one["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    assert one["verdict"] == "FAIL" and report(repo)["idempotent"] is False


def test_a_python_build_that_raises_is_that_sets_fail_naming_the_exception(tmp_path):
    repo = py(tmp_path, GOOD_PY.replace('    pdf = pdf[pdf["NOTE"].notna()]\n', '    raise ValueError("no rows today")\n'),
              sets={"normal": SETS["normal"]})
    assert run(repo) == 1
    one = report(repo, "validation.normal.json")
    assert one["verdict"] == "FAIL" and one["error"] == "the build raised: ValueError: no rows today"
    assert one["idempotent"] is None and one["needs_human"] is False


def test_a_python_build_that_never_saves_an_output_is_a_fail_naming_the_table(tmp_path):
    write = '    session.create_dataframe(rows, schema=schema).write.mode("overwrite").save_as_table(f"{tgt}.ITEMS_OUT")'
    guarded = GOOD_PY.replace(write, "    if len(rows) > 100:\n    " + write)       # the write is there, and never reached
    assert guarded != GOOD_PY
    repo = py(tmp_path, guarded, sets={"normal": SETS["normal"]})
    assert run(repo) == 1
    assert report(repo, "validation.normal.json")["error"] == (
        "output table MIGDB.MIG_WORK.ITEMS_OUT does not exist after running the build file")


def test_a_python_build_that_breaks_a_snowpark_rule_is_refused_before_it_runs(tmp_path, monkeypatch):
    def never(**kwargs):
        raise AssertionError("the sandbox must not be started for a build the rules refuse")
    monkeypatch.setattr(snowpark_sandbox, "run_golden_build", never)
    repo = py(tmp_path, GOOD_PY.replace("from snowflake", "import os\nfrom snowflake", 1))
    assert run(repo) == 1
    top = report(repo)
    assert set(top["sets"].values()) == {"FAIL"} and top["idempotent"] is None
    assert "build.py breaks the Snowpark rules" in top["error"] and "rule:imports" in top["error"]
    assert "was not imported or run" in top["error"]


def test_a_python_build_with_a_missing_golden_input_is_a_usage_error_not_a_fail(tmp_path, capsys):
    repo = py(tmp_path)
    repo.wf(WF, "golden", "inputs", "edge", "1.csv").unlink()
    assert run(repo) == 2
    assert "golden input for tool 1" in capsys.readouterr().err and reports_on_disk(repo) == []


# --- --backend snowflake, against the fakes (nothing here touches a real account) ----------------------------------


DB = "MIGDB_SANDBOX"
SB = ["--backend", "snowflake", "--connection", "sandbox", "--sandbox-database", DB]
SECRET = "hunter2-secret"
SCHEMAS = ["MIG_GOLDEN", "MIG_WORK", "MIG_COMPARE"]


@pytest.fixture
def fake(monkeypatch):
    module = FakeConnectorModule()
    monkeypatch.setitem(sys.modules, "snowflake.connector", module)
    monkeypatch.delenv(snowflake_conn.CONNECTION_ENV, raising=False)
    monkeypatch.delenv(snowflake_conn.SANDBOX_DB_ENV, raising=False)
    return module


def allow(root, databases=(DB,)) -> None:
    write_json(Path(root) / "orchestrator.config.json", {"policy": {"sandboxDatabases": list(databases)}})


def fresh(view_schema: str) -> list[str]:
    return [f"USE DATABASE {DB}", *(f"CREATE OR REPLACE SCHEMA {DB}.{s}" for s in [*SCHEMAS, view_schema])]


def sessions(monkeypatch) -> list[FakeSession]:
    built: list[FakeSession] = []

    def build(name):
        built.append(FakeSession(name))
        return built[-1]
    monkeypatch.setattr(snowflake_conn, "snowpark_session", build)
    return built


def test_a_sql_build_on_snowflake_is_run_statement_by_statement_in_a_fresh_sandbox_per_run(tmp_path, fake):
    repo = make(tmp_path / "sf")
    allow(repo.root)

    assert run(repo, *SB, "--set", "normal") == 0

    sql = fake.sql()
    starts = [i for i, text in enumerate(sql) if text == f"USE DATABASE {DB}"]
    passes = [sql[a:b] for a, b in zip(starts, [*starts[1:], len(sql)])]
    assert len(passes) == 2 and all(p[:5] == fresh("MIG_GOLDEN_WF0009_NORMAL") for p in passes)   # the run, then the re-run
    for one in passes:
        # the session parameter goes to the account as written; each statement has its placeholders rendered
        assert sum("ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1" in text for text in one) == 1
        # (a statement is sent with the comment lines before it, its `-- step` marker among them)
        assert any("CREATE OR REPLACE TEMPORARY TABLE kept AS" in text and
                   f"FROM {DB}.MIG_GOLDEN_WF0009_NORMAL.ITEMS" in text for text in one)
        assert any(f"CREATE OR REPLACE TABLE {DB}.MIG_WORK.ITEMS_OUT AS" in text for text in one)
        assert not any("{{" in text for text in one)
    assert fake.connect_calls == [{"connection_name": "sandbox"}] * 2
    assert all(connection.closed for connection in fake.connections)

    local = make(tmp_path / "local")
    assert run(local, "--set", "normal") == 0
    for name in ("validation.json", "validation.normal.json"):
        on_snowflake = report(repo, name)
        assert on_snowflake["verdict"] == "PASS" and on_snowflake["idempotent"] is True
        assert without_runtime(on_snowflake) == without_runtime(report(local, name))


def test_a_python_build_on_snowflake_runs_in_a_session_built_from_the_connection(tmp_path, fake, monkeypatch):
    repo = py(tmp_path / "sf", sets={"normal": SETS["normal"]})
    allow(repo.root)
    built = sessions(monkeypatch)

    assert run(repo, *SB) == 0

    top = report(repo)
    assert top["verdict"] == "PASS" and top["idempotent"] is True and top["language"] == "python"
    assert [session.connection_name for session in built] == ["sandbox", "sandbox"]       # the run, then the re-run
    for session in built:
        assert session.statements == fresh("MIG_GOLDEN_WF0009_NORMAL") and session.closed
    assert fake.connect_calls == []

    local = py(tmp_path / "local", sets={"normal": SETS["normal"]})
    assert run(local) == 0
    assert without_runtime(top) == without_runtime(report(local))


def test_a_connector_error_is_redacted_before_it_reaches_a_report(tmp_path, fake, capsys):
    repo = make(tmp_path)
    allow(repo.root)
    fake.account.fail[f"{DB}.MIG_WORK.ITEMS_OUT"] = f"Uncaught exception: connect(password={SECRET})"

    assert run(repo, *SB, "--set", "normal") == 1                        # a domain FAIL, as locally

    one = report(repo, "validation.normal.json")
    assert one["verdict"] == "FAIL" and "Uncaught exception" in one["error"]
    assert SECRET not in one["error"] and "password=" in one["error"]
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_no_credential_reaches_argv_logs_or_reports(tmp_path, fake, monkeypatch, capsys):
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", SECRET)
    repo = make(tmp_path)
    allow(repo.root)
    assert run(repo, *SB, "--set", "normal") == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    for path in repo.wf(WF).rglob("*.json"):
        assert SECRET not in path.read_text(encoding="utf-8"), path
    assert SECRET not in repr(fake.statements) and SECRET not in repr(fake.connect_calls)
    for flag in ("--password", "--token", "--private-key-file"):
        with pytest.raises(SystemExit) as refused:
            vb.build_parser().parse_args([WF, *SB, flag, "x"])
        assert refused.value.code == 2
        assert f"unrecognized arguments: {flag}" in capsys.readouterr().err


def test_a_sandbox_database_outside_the_policy_is_refused_before_anything_connects(tmp_path, fake, monkeypatch, capsys):
    built = sessions(monkeypatch)
    repo = make(tmp_path)
    allow(repo.root, ["MIGDB"])
    for database in ("ANALYTICS", "migdb"):
        assert run(repo, "--backend", "snowflake", "--connection", "sandbox", "--sandbox-database", database) == 2
        err = capsys.readouterr().err
        assert database in err and ("sandboxDatabases" in err or "identifier" in err), err
    assert fake.connect_calls == [] and built == [] and reports_on_disk(repo) == []


def test_snowflake_without_a_connection_name_is_exit_2(tmp_path, fake, capsys):
    repo = make(tmp_path)
    allow(repo.root)
    assert run(repo, "--backend", "snowflake", "--sandbox-database", DB) == 2
    assert "MIG_SNOWFLAKE_CONNECTION" in capsys.readouterr().err and fake.connect_calls == []


def test_the_duckdb_default_imports_nothing_snowflake(tmp_path):
    repo = make(tmp_path)
    probe = ("import sys; sys.path[:0] = ['scripts', '.']; import validate_build as vb; "
             f"rc = vb.main(['{WF}', '--root', {str(repo.root)!r}, '--set', 'normal']); "
             "print(rc, sorted(m for m in sys.modules if m.startswith(('snowflake.', 'lib.snowflake'))))")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "0 []"


# --- the samples: every canned build passes every golden set ------------------------------------------------------

BUILDS = {"wf_0001": "build.sql", "wf_0002": "build.sql", "wf_0003": "build.sql", "wf_0004": "build.sql",
          "wf_0006": "build.py", "wf_0007": "build.sql"}
GOLDEN_SETS = ["normal", "period_end", "empty", "edge"]


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    """`prepared(wf_id)`: the sample built through intake with its canned objective, once per module; a test copies a
    build file in (and removes it again), and every validation rewrites the reports."""
    cache: dict[str, Repo] = {}

    def get(wf_id: str) -> Repo:
        if wf_id not in cache:
            cache[wf_id] = prepare_objective_workflow(tmp_path_factory.mktemp(wf_id), wf_id)
        repo = cache[wf_id]
        for stale in [*repo.wf(wf_id).glob("build.*"), *repo.wf(wf_id).glob("validation*.json"), repo.wf(wf_id, "compile_check.json")]:
            stale.unlink(missing_ok=True)
        return repo
    return get


@pytest.mark.e2e
@pytest.mark.parametrize("wf_id", sorted(BUILDS))
def test_every_canned_build_passes_every_golden_set_of_its_sample(prepared, wf_id, capsys):
    repo = prepared(wf_id)
    shutil.copy(SAMPLES / wf_id / "canned" / BUILDS[wf_id], repo.wf(wf_id, BUILDS[wf_id]))

    rc = vb.main([wf_id, "--root", str(repo.root)])

    top = read_json(repo.wf(wf_id, "validation.json"))
    assert rc == 0, top
    assert top["sets"] == {name: "PASS" for name in GOLDEN_SETS}, top
    assert top["verdict"] == "PASS" and top["needs_human"] is False and top["truncated"] is False
    assert top["idempotent"] is True and top["idempotency_diff"] == []
    assert top["language"] == ("python" if BUILDS[wf_id].endswith(".py") else "sql")
    for name in GOLDEN_SETS:
        one = read_json(repo.wf(wf_id, f"validation.{name}.json"))
        assert one["verdict"] == "PASS" and one["diff_clusters"] == [] and "error" not in one
    assert capsys.readouterr().out.startswith(f"{wf_id}: PASS ")


def test_wf_0005_is_tier_t3_so_there_is_nothing_to_validate(tmp_path, capsys):
    repo = prepare_objective_workflow(tmp_path, "wf_0005")
    assert vb.main(["wf_0005", "--root", str(repo.root)]) == 2
    assert "tier T3" in capsys.readouterr().err
    assert not list(repo.wf("wf_0005").glob("validation*.json"))


# --- samples/<wf>/broken/: every broken build fails with the cluster broken.json names -------------------------------

BROKEN = sorted(path.parent.parent.name for path in SAMPLES.glob("wf_*/broken/broken.json"))


def cases(wf_id: str) -> list[dict]:
    return read_json(SAMPLES / wf_id / "broken" / "broken.json")


def test_every_sample_with_a_canned_build_has_broken_builds():
    assert BROKEN == sorted(BUILDS)


@pytest.mark.parametrize("wf_id", sorted(BUILDS))
def test_broken_json_lists_exactly_the_whole_file_builds_of_the_directory(wf_id):
    directory = SAMPLES / wf_id / "broken"
    listed = [case["file"] for case in cases(wf_id)]
    on_disk = sorted(path.name for path in directory.iterdir() if path.name != "broken.json")
    assert listed == sorted(listed) == on_disk and len(set(listed)) == len(listed)
    extension = Path(BUILDS[wf_id]).suffix
    assert {Path(name).suffix for name in listed} == {extension}, "a broken build is the canned build's own language"
    for case in cases(wf_id):
        assert case["file"][:2].isdigit() and case["golden_set"] in GOLDEN_SETS
        assert set(case["expect"]) == {"class", "columns", "output", "scope"} and isinstance(case["compiles"], bool)
        assert set(case) <= {"file", "golden_set", "expect", "compiles", "deterministic", "note"}
        text = (directory / case["file"]).read_text(encoding="utf-8")
        assert text.startswith(("-- BROKEN ON PURPOSE", "# BROKEN ON PURPOSE")) and "\r" not in text
    assert cases(wf_id)[0]["compiles"] is True, "the orchestrator's mock serves the first build: it must get past compile_check"


def test_no_sample_keeps_the_old_per_segment_variants():
    assert not list(SAMPLES.glob("wf_*/broken_sql")), "the segment procedures' broken variants are gone: samples/<wf>/broken/ holds the whole-file ones"


def broken_params():
    return [pytest.param(wf_id, case["file"], id=f"{wf_id}/{case['file']}") for wf_id in sorted(BUILDS) for case in cases(wf_id)]


@pytest.mark.parametrize("wf_id, file", broken_params())
def test_a_broken_build_fails_with_the_cluster_its_broken_json_names(prepared, wf_id, file):
    case = next(entry for entry in cases(wf_id) if entry["file"] == file)
    repo = prepared(wf_id)
    suffix = Path(file).suffix
    shutil.copy(SAMPLES / wf_id / "broken" / file, repo.wf(wf_id, f"build{suffix}"))

    compiled = compile_check.compile_check(repo, wf_id)
    assert compiled["ok"] is case["compiles"], compiled["errors"]

    rc = vb.main([wf_id, "--root", str(repo.root), "--set", case["golden_set"]])

    assert rc == 1
    top = read_json(repo.wf(wf_id, "validation.json"))
    assert top["verdict"] == "FAIL" and top["sets"] == {case["golden_set"]: "FAIL"} and top["needs_human"] is False
    if case.get("deterministic", True):
        assert top["idempotent"] is True, "a broken build is wrong, not random"
    expect = case["expect"]
    clusters = read_json(repo.wf(wf_id, f"validation.{case['golden_set']}.json"))["diff_clusters"]
    found = [(c["class"], c["columns"], c["stream"], c["scope"]) for c in clusters]
    if case.get("deterministic", True):
        assert (expect["class"], expect["columns"], expect["output"], expect["scope"]) in found, found
    else:
        # The build is not repeatable (a MERGE with two source rows for one target row keeps either), so which
        # columns differ varies from run to run: what holds every time is a cluster of the named class on the
        # named output.
        assert any(cluster == expect["class"] and stream == expect["output"] for cluster, _, stream, _ in found), found


# --- the sandbox's failure reasons, as a report carries them -----------------------------------------------------


@pytest.mark.parametrize("kind, expected", [
    ("run", "the build raised: ValueError: boom"),
    ("sandbox", "the sandbox refused a host operation: ValueError: boom"),
    ("timeout", "sandbox: timeout (ValueError: boom)"),
    ("exited", "sandbox: agent code exited (ValueError: boom)"),
    ("read_back", "ValueError: boom"),
])
def test_a_sandbox_failure_is_reported_as_one_message_naming_the_build(kind, expected):
    result = snowpark_sandbox.ChildResult(kind, error="ValueError: boom")
    assert snowpark_sandbox.failure_reason(result, "the build") == expected
    # the default names the build, which is what every caller here reports
    assert snowpark_sandbox.failure_reason(snowpark_sandbox.ChildResult("run", error="x")) == "the build raised: x"
