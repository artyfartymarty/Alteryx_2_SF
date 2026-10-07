"""`validate_dbt.py`: the dbt project `export_dbt.py` wrote, run on a fresh DuckDB sandbox per golden set with
dbt-duckdb and judged against the golden outputs by the unchanged `compare.py` (objective-first design D9, §4.10).

Three layers, as for `validate_build.py`:

* a small synthetic workflow (`wf_0009`: ITEMS in, ITEMS_OUT out) for every behaviour of the script itself -- the
  report shape and where it lands, a wrong build, a model that does not run, a test that fails, idempotency and row
  order, a project that is not the export, a build with no dbt form, the skips, the usage errors that write nothing,
  the exit codes, and the translation of the models to DuckDB;
* the samples: every canned SQL build's exported project PASSes every golden set of its sample, a Python sample
  and the tier-T3 one are skipped;
* the translation helpers on their own.

Each `dbt build` costs a few seconds, so a test that does not need a second set asks for one. Nothing here has run
on Snowflake or Alteryx: locally "parity" means dbt-duckdb agrees with the Alteryx simulator.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import export_dbt
import validate_build as vb
import validate_dbt as vd
from lib import dbt_project
from lib.backend import BackendError, DuckDBBackend
from lib.dbt_project import DbtResult
from lib.io import read_json, save_manifest, write_json, write_yaml
from lib.paths import Repo
from lib.typed_csv import write_table
from tests import build_fixtures as bf
from tests.helpers import SAMPLES, prepare_objective_workflow

WF = bf.WF
SQL_SAMPLES = ["wf_0001", "wf_0002", "wf_0003", "wf_0004", "wf_0007"]

FIELDS = [{"name": "ID", "type": "Int32", "size": 4, "scale": None},
          {"name": "NOTE", "type": "V_String", "size": 20, "scale": None}]
#: golden input rows by set; the golden output is the rows with a NOTE (`kept`)
SETS = {"normal": [[1, "keep"], [2, None], [3, "x"]], "edge": [[4, "y"], [5, None], [6, ""]], "empty": []}


def kept(rows):
    return [row for row in rows if row[1] is not None]


def make(root, build=None, *, obj=None, sets=None, expected=kept, export=True, mappings=True, manifest=True) -> Repo:
    """A tmp `wf_0009`: objective, build file, `intake/mappings.yaml`, golden inputs and outputs for every set and a
    manifest naming them -- then (with `export`) the dbt project exported from it."""
    repo = bf.make(root, build, obj)
    sets = {"normal": SETS["normal"]} if sets is None else sets
    for golden_set, rows in sets.items():
        write_table(repo.wf(WF, "golden", "inputs", golden_set, "1.csv"), {"fields": FIELDS, "rows": rows})
        write_table(repo.wf(WF, "golden", "outputs", golden_set, "7.csv"), {"fields": FIELDS, "rows": expected(rows)})
    if mappings:
        write_yaml(repo.wf(WF, "intake", "mappings.yaml"), {
            "sources": {"in/items.yxdb": {"snowflake": "SALES.RAW.ITEMS", "logical": "ITEMS", "tool_ids": ["1"]}},
            "outputs": {"out/items_out.yxdb": {"snowflake": "SALES.CURATED.ITEMS_OUT", "logical": "ITEMS_OUT",
                                               "mode": "overwrite", "keys": [], "tool_ids": ["7"]}}})
    if manifest:
        save_manifest(repo, {"id": WF, "status": {}, "metrics": {}, "golden_sets": list(sets)})
    if export:
        export_dbt.export(repo, WF)
    return repo


def run(repo: Repo, *args: str) -> int:
    return vd.main([WF, "--root", str(repo.root), *args])


def project(repo: Repo) -> Path:
    return repo.wf(WF, "dbt")


def report(repo: Repo, name: str = "validation.json") -> dict:
    return read_json(project(repo) / name)


def reports_on_disk(repo: Repo) -> list[str]:
    return sorted(path.name for path in project(repo).glob("validation*.json"))


def edit(repo: Repo, build: str) -> None:
    """Replaces the build file and exports again, so the project on disk is the export of THIS build."""
    repo.wf(WF, "build.sql").write_text(build, encoding="utf-8", newline="\n")
    export_dbt.export(repo, WF)


# --- a correct project -------------------------------------------------------------------------------------------


def test_a_correct_project_passes_every_set_and_is_idempotent(tmp_path, capsys):
    repo = make(tmp_path, sets=SETS)
    assert run(repo) == 0
    top = report(repo)
    assert top["verdict"] == "PASS" and top["needs_human"] is False and top["truncated"] is False
    assert top["sets"] == {"normal": "PASS", "edge": "PASS", "empty": "PASS"}
    assert top["idempotent"] is True and top["idempotency_diff"] == [] and top["language"] == "sql"
    assert reports_on_disk(repo) == ["validation.edge.json", "validation.empty.json", "validation.json",
                                     "validation.normal.json"]
    assert capsys.readouterr().out.strip() == (
        f"{WF}: PASS {{'normal': 'PASS', 'edge': 'PASS', 'empty': 'PASS'}} (idempotent=True)")


def test_the_report_is_validate_builds_report_and_lands_in_the_dbt_directory(tmp_path):
    repo = make(tmp_path)
    write_json(repo.wf(WF, "validation.json"), {"verdict": "PASS", "from": "validate_build.py"})
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
    # the build's own report, one directory up, is not this script's to touch
    assert read_json(repo.wf(WF, "validation.json")) == {"verdict": "PASS", "from": "validate_build.py"}
    assert not list(repo.wf(WF).glob("validation.*.json"))


def test_the_logs_and_sandboxes_are_kept_and_the_rerun_sandbox_is_not(tmp_path):
    repo = make(tmp_path, sets={"normal": SETS["normal"], "edge": SETS["edge"]})
    run(repo)
    for name in ("validate_normal.log", "validate_edge.log", "validate_normal_rerun.log", "validate_edge_rerun.log"):
        assert (project(repo) / "logs" / name).is_file(), name
    assert (project(repo) / "logs" / "validate_edge_rerun.log").is_file(), "every set is run twice"
    log = (project(repo) / "logs" / "validate_normal.log").read_text(encoding="utf-8")
    assert "\x1b[" not in log and "\r\n" not in log and str(tmp_path) not in log
    assert repo.wf(WF, "dbt_sandbox_normal.duckdb").is_file() and repo.wf(WF, "dbt_sandbox_edge.duckdb").is_file()
    assert not list(repo.wf(WF).glob("*_rerun.duckdb*"))


def test_validation_does_not_change_the_delivered_project_which_stays_snowflake_sql(tmp_path):
    repo = make(tmp_path, build=bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT ID, IFF(NOTE IS NULL, 'n', NOTE) AS NOTE FROM kept;"))
    before = {path.relative_to(project(repo)).as_posix(): path.read_bytes()
              for path in project(repo).rglob("*") if path.is_file()}
    assert run(repo) == 0                      # DuckDB has no IFF: the run was on a translated copy
    after = {path.relative_to(project(repo)).as_posix(): path.read_bytes()
             for path in project(repo).rglob("*")
             if path.is_file() and not path.name.startswith("validation") and "logs" not in path.parts}
    assert after == before
    assert b"IFF(NOTE IS NULL, 'n', NOTE)" in before["models/items_out.sql"]


def test_set_names_only_the_sets_given_and_leaves_no_report_for_the_others(tmp_path):
    repo = make(tmp_path, sets=SETS)
    assert run(repo, "--set", "edge", "--set", "normal", "--set", "edge") == 0
    assert report(repo)["sets"] == {"edge": "PASS", "normal": "PASS"}
    assert reports_on_disk(repo) == ["validation.edge.json", "validation.json", "validation.normal.json"]


def test_a_dropped_golden_set_loses_its_stale_report(tmp_path):
    repo = make(tmp_path, sets={"normal": SETS["normal"], "edge": SETS["edge"]})
    run(repo)
    assert (project(repo) / "validation.edge.json").is_file()
    run(repo, "--set", "normal")
    assert not (project(repo) / "validation.edge.json").exists()


# --- a wrong project, an error, a failed test ---------------------------------------------------------------------


def test_wrong_logic_fails_names_the_output_and_the_class_and_exits_1(tmp_path, capsys):
    repo = make(tmp_path)
    edit(repo, bf.GOOD_SQL.replace("WHERE NOTE IS NOT NULL", "WHERE ID <> 3"))
    assert run(repo) == 1
    top = report(repo)
    assert top["verdict"] == "FAIL" and top["sets"] == {"normal": "FAIL"}
    assert top["diff_clusters"] and all(cluster["stream"] == "ITEMS_OUT" for cluster in top["diff_clusters"])
    captured = capsys.readouterr()
    assert captured.out.startswith(f"{WF}: FAIL {{'normal': 'FAIL'}}")
    first = captured.err.splitlines()[0]
    assert first.startswith("  normal: ITEMS_OUT: ") and "differs from the golden output" in first


def test_a_model_that_fails_to_run_is_a_domain_fail_naming_it_never_a_traceback(tmp_path, capsys):
    repo = make(tmp_path)
    edit(repo, bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT NO_SUCH_COLUMN FROM kept;"))
    assert run(repo) == 1
    top = report(repo)
    assert top["verdict"] == "FAIL" and top["diff_clusters"] == []
    assert "dbt build exited 1 on golden set normal: failed models: items_out" in top["error"]
    assert "NO_SUCH_COLUMN" in top["error"] and str(tmp_path) not in top["error"]
    assert top["idempotent"] is None and top["idempotency_diff"] == [], "no second run after a failed first"
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err.splitlines()[0].startswith("  normal: dbt build exited 1 on golden set normal: failed models: items_out")


def test_a_dbt_test_that_fails_is_still_a_fail_naming_it():
    """The generated tests only warn, so a failed one means something else made it (a status of `fail` or `error`)."""
    result = DbtResult(1, "", [{"name": "not_null_items_out_ID", "unique_id": "test.mig_wf_0009.not_null_items_out_ID.ab",
                                "status": "fail", "message": "Got 1 result, configured to fail if != 0"}], None)
    error = vd._run_error(result, "normal")
    assert "failed tests: not_null_items_out_ID" in error and "failed models" not in error
    assert "Got 1 result" in error


def test_a_golden_null_in_a_not_null_column_and_a_repeating_key_pass_both_validators(tmp_path, capsys):
    """compare.py makes `nullability_contract` and `keys_not_unique` advisories, so validate_build.py passes; the dbt
    tests that state the same contract (`not_null`, `unique`) are warnings, so validate_dbt.py passes too, naming
    the tests that warned."""
    obj = bf.objective(keys=("ID",))
    obj["outputs"][0]["columns"][0]["nullable"] = False                # ID: NOT NULL, and the one-column key
    sets = {"clean": [[1, "a"], [2, "b"]], "null": [[None, "c"], [3, "d"]], "repeat": [[4, "e"], [4, "f"]]}
    repo = make(tmp_path, obj=obj, sets=sets)
    schema = (project(repo) / "models" / "schema.yml").read_text(encoding="utf-8")
    assert "- not_null:" in schema and "- unique:" in schema and schema.count("severity: warn\n") == 2

    assert vb.main([WF, "--root", str(repo.root)]) == 0
    built = read_json(repo.wf(WF, "validation.json"))
    assert built["verdict"] == "PASS" and built["sets"] == {"clean": "PASS", "null": "PASS", "repeat": "PASS"}
    nullable = read_json(repo.wf(WF, "validation.null.json"))["checks"]["ITEMS_OUT:target"]
    assert nullable["nullability_contract"]["columns"] == ["ID"]
    repeated = read_json(repo.wf(WF, "validation.repeat.json"))["checks"]["ITEMS_OUT:target"]
    assert repeated["keys_not_unique"]["keys"] == ["ID"]
    capsys.readouterr()

    assert run(repo) == 0
    top = report(repo)
    assert top["verdict"] == "PASS" and top["sets"] == {"clean": "PASS", "null": "PASS", "repeat": "PASS"}
    assert top["idempotent"] is True and top["needs_human"] is False
    assert [(a["golden_set"], a["test"]) for a in top["advisories"]] == [
        ("null", "not_null_items_out_ID"), ("repeat", "unique_items_out_ID")]
    assert all(a["kind"] == "dbt_test_warned" and "configured to warn" in a["message"] for a in top["advisories"])
    assert "advisories" not in report(repo, "validation.clean.json")
    assert [a["test"] for a in report(repo, "validation.null.json")["advisories"]] == ["not_null_items_out_ID"]
    assert [a["test"] for a in report(repo, "validation.repeat.json")["advisories"]] == ["unique_items_out_ID"]
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"{WF}: PASS {{'clean': 'PASS', 'null': 'PASS', 'repeat': 'PASS'}} (idempotent=True)"
    assert [line.split(":")[0] for line in out[1:]] == ["  advisory"] * 2
    assert "dbt test not_null_items_out_ID warned on golden set null" in out[1]
    assert "dbt test unique_items_out_ID warned on golden set repeat" in out[2]


def test_an_output_table_dbt_never_made_is_a_fail_naming_it(tmp_path, monkeypatch):
    """A project that exported cleanly makes every output; this is the guard for one that somehow did not."""
    repo = make(tmp_path)
    monkeypatch.setattr(vd, "_tables", lambda job: ["MIGDB.MIG_WORK.NOT_THERE"])
    assert run(repo) == 1
    top = report(repo)
    assert top["verdict"] == "FAIL"
    assert "output table MIGDB.MIG_WORK.NOT_THERE does not exist after running the dbt project" in top["error"]


# --- idempotency ---------------------------------------------------------------------------------------------------


def test_a_non_deterministic_project_is_not_idempotent(tmp_path):
    repo = make(tmp_path, build=bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT ID, NOTE || CAST(RANDOM() AS VARCHAR) AS NOTE FROM kept;"))
    run(repo)
    top = report(repo)
    assert top["idempotent"] is False and top["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    assert top["verdict"] == "FAIL"


def test_idempotency_is_checked_on_every_set_not_only_the_first(tmp_path):
    """A `LIMIT 1` with no ORDER BY is deterministic on a one-row set and order-dependent on the next one."""
    first_only = bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT ID, NOTE FROM kept LIMIT 1;")
    sets = {"single": [[1, "a"]], "several": [[1, "a"], [2, "b"], [3, "c"], [4, "d"]]}
    repo = make(tmp_path, build=first_only, sets=sets, expected=lambda rows: rows[:1])
    assert run(repo) == 1
    top = report(repo)
    assert report(repo, "validation.single.json")["idempotent"] is True
    assert report(repo, "validation.single.json")["verdict"] == "PASS"
    several = report(repo, "validation.several.json")
    assert several["idempotent"] is False and several["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    assert several["verdict"] == "FAIL"
    assert top["idempotent"] is False and top["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    assert top["sets"] == {"single": "PASS", "several": "FAIL"}


def test_a_project_that_depends_on_row_order_is_not_idempotent(tmp_path):
    rows = [[1, "a"], [2, "b"], [3, "c"], [4, "d"]]
    first_only = bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT ID, NOTE FROM kept LIMIT 1;")
    # Alteryx's "first record" is the first in file order: ID 1, which is what the first run returns
    repo = make(tmp_path, build=first_only, sets={"normal": rows}, expected=lambda rows: [rows[0]])
    assert run(repo) == 1
    top = report(repo)
    assert top["idempotent"] is False and top["idempotency_diff"] == ["MIGDB.MIG_WORK.ITEMS_OUT"]
    assert top["verdict"] == "FAIL" and top["diff_clusters"] == [], "the judged (file-order) run matched the golden output"


# --- the delivered project is what is judged ---------------------------------------------------------------------------


def _edit_a_model(p: Path) -> None:
    model = p / "models" / "items_out.sql"
    model.write_text(model.read_text(encoding="utf-8").replace("NOTE FROM", "NOTE, NOTE AS X FROM"), encoding="utf-8",
                     newline="\n")


def _delete_a_model(p: Path) -> None:
    (p / "models" / "kept.sql").unlink()


def _add_a_model(p: Path) -> None:
    (p / "models" / "extra.sql").write_text("select 1\n", encoding="utf-8")


def _add_a_macro(p: Path) -> None:
    (p / "macros").mkdir()
    (p / "macros" / "m.sql").write_text("{% macro m() %}1{% endmacro %}\n", encoding="utf-8")


def _change_the_profile(p: Path) -> None:
    (p / "profiles.yml").write_text("x: 1\n", encoding="utf-8")


@pytest.mark.parametrize("what, change, expect", [
    ("a model edited by hand", _edit_a_model, "dbt/models/items_out.sql differs from the file export_dbt.py writes"),
    ("a model deleted", _delete_a_model, "dbt/models/kept.sql is missing"),
    ("a file nobody exported", _add_a_model, "dbt/models/extra.sql is not a file export_dbt.py writes"),
    ("a macro", _add_a_macro, "dbt/macros/m.sql is not a file export_dbt.py writes"),
    ("a changed profile", _change_the_profile, "dbt/profiles.yml differs from the file export_dbt.py writes"),
])
def test_a_project_that_is_not_the_export_is_a_fail_and_dbt_never_starts(tmp_path, capsys, what, change, expect):
    repo = make(tmp_path)
    change(project(repo))
    assert run(repo) == 1
    top = report(repo)
    assert top["verdict"] == "FAIL" and top["sets"] == {"normal": "FAIL"}
    assert f"the dbt project on disk is not the export of build.sql: {expect}; run python scripts/export_dbt.py {WF}" in top["error"]
    assert not (project(repo) / "logs").exists() and not list(repo.wf(WF).glob("dbt_sandbox_*")), "no dbt process, no sandbox"
    assert capsys.readouterr().err.splitlines()[0].startswith("  normal: the dbt project on disk is not the export of build.sql")


def test_a_build_changed_after_the_export_is_a_stale_project(tmp_path):
    repo = make(tmp_path)
    repo.wf(WF, "build.sql").write_text(bf.GOOD_SQL.replace("NOTE IS NOT NULL", "ID > 0"), encoding="utf-8", newline="\n")
    assert run(repo) == 1
    assert "dbt/models/kept.sql differs from the file export_dbt.py writes" in report(repo)["error"]


def test_files_dbt_and_a_person_leave_in_the_project_are_not_the_project(tmp_path):
    repo = make(tmp_path)
    for name in ("dbt_sandbox.duckdb", "dbt_sandbox.duckdb.wal"):
        (project(repo) / name).write_bytes(b"x")
    (project(repo) / "target").mkdir()
    (project(repo) / "target" / "manifest.json").write_text("{}", encoding="utf-8")
    assert run(repo) == 0


def test_a_build_with_no_dbt_form_is_a_fail_naming_its_line(tmp_path, capsys):
    repo = make(tmp_path)
    build = bf.GOOD_SQL.replace("-- step s2", "CALL cleanup();\n\n-- step s2")
    repo.wf(WF, "build.sql").write_text(build, encoding="utf-8", newline="\n")
    line = build.splitlines().index("CALL cleanup();") + 1
    assert run(repo) == 1
    error = report(repo)["error"]
    assert error.startswith(f"build.sql line {line}: no dbt form for `CALL cleanup()`")
    assert f"run python scripts/export_dbt.py {WF} once the build has a dbt form" in error
    assert capsys.readouterr().err.splitlines()[0].startswith(f"  normal: build.sql line {line}: no dbt form for ")


# --- the translation of the models to DuckDB -------------------------------------------------------------------------


def test_a_model_snowflake_sql_the_double_cannot_express_is_a_fail_naming_the_model(tmp_path):
    repo = make(tmp_path, build=bf.GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT ID, TO_CHAR(ID, 'Q') AS NOTE FROM kept;"))
    assert run(repo) == 1
    error = report(repo)["error"]
    assert error.startswith("models/items_out.sql cannot be expressed in DuckDB: ")
    assert not (project(repo) / "logs").exists()


def test_translate_gives_duckdb_sql_and_leaves_what_dbt_resolves_alone():
    translator = DuckDBBackend()
    try:
        sql = ("SELECT IFF(NOTE IS NULL, 'n', 'y') AS FLAG, TRY_TO_NUMBER(NOTE, 38, 0) AS N\n"
               "FROM {{ source('wf_0009', 'ITEMS') }} i JOIN {{ ref('kept') }} k ON i.ID = k.ID "
               "WHERE NOTE <> '{{ '{' }}#'")
        assert vd.translate(translator, sql) == (
            "SELECT CASE WHEN NOTE IS NULL THEN 'n' ELSE 'y' END AS FLAG, TRY_CAST(NOTE AS DECIMAL(38, 0)) AS N "
            "FROM {{ source('wf_0009', 'ITEMS') }} AS i JOIN {{ ref('kept') }} AS k ON i.ID = k.ID "
            "WHERE NOTE <> '{{ '{' }}#'")
        assert vd.translate(translator, "UPDATE {{ this }} SET NOTE = 'Y' WHERE NOTE IS NULL") == (
            "UPDATE {{ this }} SET NOTE = 'Y' WHERE NOTE IS NULL")
        with pytest.raises(BackendError, match="unsupported TO_CHAR/TO_VARCHAR format element 'Q'"):
            vd.translate(translator, "SELECT TO_CHAR(ID, 'Q') FROM {{ ref('kept') }}")
    finally:
        translator.close()


def test_the_translated_copy_holds_every_delivered_file_but_the_models(tmp_path):
    repo = make(tmp_path)
    models = export_dbt.export(repo, WF, write=False).models
    vd.write_duckdb_project(project(repo), models, tmp_path / "copy")
    assert sorted(path.relative_to(tmp_path / "copy").as_posix() for path in (tmp_path / "copy").rglob("*") if path.is_file()) == [
        "dbt_project.yml", "models/items_out.sql", "models/kept.sql", "models/schema.yml", "models/sources.yml", "profiles.yml"]
    for name in ("dbt_project.yml", "profiles.yml", "models/schema.yml", "models/sources.yml"):
        assert (tmp_path / "copy" / name).read_bytes() == (project(repo) / name).read_bytes()
    assert (tmp_path / "copy" / "models" / "items_out.sql").read_text(encoding="utf-8").startswith(
        "{{ config(materialized='table', alias='ITEMS_OUT') }}\n-- step s2\n")


# --- the skips ----------------------------------------------------------------------------------------------------------


def test_program_dbt_false_skips_the_validation_and_writes_nothing(tmp_path, capsys):
    repo = make(tmp_path)
    write_yaml(repo.global_mappings, {"program": {"dbt": False}})
    export_dbt.export(repo, WF)                       # removes the project
    assert run(repo) == 0
    assert capsys.readouterr().out.strip().endswith("dbt validation skipped: program.dbt is false in mappings/global.yaml")
    assert not project(repo).exists() and not list(repo.wf(WF).glob("dbt_sandbox_*"))


def test_a_stale_report_is_removed_even_when_the_validation_is_skipped(tmp_path):
    repo = make(tmp_path)
    run(repo)
    assert (project(repo) / "validation.json").is_file()
    write_yaml(repo.global_mappings, {"program": {"dbt": False}})
    assert run(repo) == 0
    assert not (project(repo) / "validation.json").exists()


def test_a_python_workflow_is_skipped(tmp_path, capsys):
    obj = bf.objective(language="python")
    repo = bf.make(tmp_path, "def main(session, src, tgt):\n    return 'OK'\n", obj, name="build.py")
    assert run(repo) == 0
    assert "dbt validation skipped: the objective's language is python" in capsys.readouterr().out
    assert not project(repo).exists()


# --- usage errors: nothing written, a stale report cleared, exit 2 ------------------------------------------------------


def stale(repo: Repo) -> Path:
    write_json(project(repo) / "validation.json", {"stale": True})
    return project(repo) / "validation.json"


def test_usage_errors_leave_no_report_and_exit_2(tmp_path, capsys):
    repo = make(tmp_path)
    marker = stale(repo)

    (project(repo) / "dbt_project.yml").unlink()                         # no dbt project
    assert run(repo) == 2 and not marker.exists()
    err = capsys.readouterr().err
    assert "has no dbt project to validate" in err and "run python scripts/export_dbt.py wf_0009 first" in err
    export_dbt.export(repo, WF)

    marker = stale(repo)
    save_manifest(repo, {"id": WF, "status": {}, "metrics": {}, "golden_sets": []})     # no golden sets
    assert run(repo) == 2 and not marker.exists()
    assert "has no golden sets to validate against" in capsys.readouterr().err

    assert run(repo, "--set", "nope") == 2                                # an unknown set
    assert "has no golden set 'nope'" in capsys.readouterr().err

    save_manifest(repo, {"id": WF, "status": {}, "metrics": {}, "golden_sets": ["normal"]})
    repo.wf(WF, "golden", "outputs", "normal", "7.csv").unlink()          # a golden output missing
    marker = stale(repo)
    assert run(repo) == 2 and not marker.exists()
    assert "golden output of tool 7 (ITEMS_OUT) of wf_0009/normal is missing" in capsys.readouterr().err
    assert not (project(repo) / "logs").exists() and not list(repo.wf(WF).glob("dbt_sandbox_*"))


def test_no_objective_and_no_mappings_are_usage_errors(tmp_path, capsys):
    repo = make(tmp_path)
    assert vd.main(["wf_9999", "--root", str(tmp_path)]) == 2
    assert "wf_9999 has no objective.json" in capsys.readouterr().err

    repo.wf(WF, "intake", "mappings.yaml").unlink()
    marker = stale(repo)
    assert run(repo) == 2 and not marker.exists()
    assert "has no intake/mappings.yaml" in capsys.readouterr().err


def test_an_invalid_workflow_id_is_a_usage_error(tmp_path, capsys):
    assert vd.main(["../escape", "--root", str(tmp_path)]) == 2
    assert "invalid workflow id" in capsys.readouterr().err


def test_dbt_unavailable_is_a_usage_error_before_anything_runs(tmp_path, monkeypatch, capsys):
    repo = make(tmp_path)

    def boom():
        raise dbt_project.DbtUnavailable("no dbt console script beside this interpreter")

    monkeypatch.setattr(vd.dbt_project, "dbt_executable", boom)
    marker = stale(repo)
    assert run(repo) == 2 and not marker.exists()
    assert "no dbt console script beside this interpreter" in capsys.readouterr().err
    assert not list(repo.wf(WF).glob("dbt_sandbox_*"))


def test_an_unexpected_exception_is_exit_2_with_a_traceback_and_no_report(tmp_path, monkeypatch, capsys):
    repo = make(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(vd, "_run_set", boom)
    marker = stale(repo)
    assert run(repo) == 2 and not marker.exists()
    assert "RuntimeError: boom" in capsys.readouterr().err


def test_an_unknown_argument_is_a_usage_error():
    with pytest.raises(SystemExit) as raised:
        vd.main(["wf_0001", "--backend", "snowflake"])
    assert raised.value.code == 2, "there is no --backend: nothing here connects to Snowflake"


def test_the_script_runs_from_the_command_line_with_the_exit_codes_it_documents(tmp_path):
    repo = make(tmp_path)
    script = str(Path(vd.__file__))
    done = subprocess.run([sys.executable, script, WF, "--root", str(tmp_path)], capture_output=True, text=True,
                          encoding="utf-8")
    assert done.returncode == 0 and done.stdout.strip() == f"{WF}: PASS {{'normal': 'PASS'}} (idempotent=True)", done.stderr
    done = subprocess.run([sys.executable, script, "wf_9999", "--root", str(tmp_path)], capture_output=True, text=True,
                          encoding="utf-8")
    assert done.returncode == 2 and "Traceback" not in done.stderr
    edit(repo, bf.GOOD_SQL.replace("WHERE NOTE IS NOT NULL", "WHERE ID <> 3"))
    done = subprocess.run([sys.executable, script, WF, "--root", str(tmp_path)], capture_output=True, text=True,
                          encoding="utf-8")
    assert done.returncode == 1 and done.stderr.startswith("  normal: ITEMS_OUT: ")


# --- first_problem, on its own --------------------------------------------------------------------------------------------


def test_first_problem_is_one_line_for_each_kind_of_failure():
    base = {"golden_set": "normal", "verdict": "FAIL", "error": None, "idempotent": True, "idempotency_diff": [],
            "diff_clusters": []}
    assert vd.first_problem({**base, "verdict": "PASS"}) is None
    assert vd.first_problem({**base, "verdict": "PASS_WITH_ACCEPTED_DIFF"}) is None
    assert vd.first_problem({**base, "error": "dbt build exited 1\nsecond line"}) == "normal: dbt build exited 1"
    assert vd.first_problem({**base, "idempotent": False, "idempotency_diff": ["MIGDB.MIG_WORK.A", "MIGDB.MIG_WORK.B"]}) == (
        "normal: not idempotent: MIGDB.MIG_WORK.A, MIGDB.MIG_WORK.B hold other rows on a second run")
    cluster = {"class": "NULL_SEMANTICS", "columns": ["REGION", "QTY"], "count": 3, "stream": "SALES_SUMMARY"}
    assert vd.first_problem({**base, "diff_clusters": [cluster]}) == (
        "normal: SALES_SUMMARY: NULL_SEMANTICS in REGION, QTY (3 rows) differs from the golden output")
    assert vd.first_problem(base) == "normal: FAIL"


# --- the samples ---------------------------------------------------------------------------------------------------------


def stage_sample(root: Path, wf_id: str) -> Repo:
    """The sample built through intake with its simulator golden data and canned objective, plus its canned build."""
    repo = prepare_objective_workflow(root, wf_id)
    canned = SAMPLES / wf_id / "canned"
    for name in ("build.sql", "build.py"):
        if (canned / name).is_file():
            shutil.copy(canned / name, repo.wf(wf_id, name))
    return repo


@pytest.mark.e2e
@pytest.mark.parametrize("wf_id", SQL_SAMPLES)
def test_every_canned_sql_builds_exported_project_passes_every_golden_set(tmp_path, wf_id, capsys):
    repo = stage_sample(tmp_path, wf_id)
    known = read_json(repo.wf(wf_id, "manifest.json"))["golden_sets"]
    assert export_dbt.main([wf_id, "--root", str(tmp_path)]) == 0
    assert vd.main([wf_id, "--root", str(tmp_path)]) == 0, capsys.readouterr().err
    top = read_json(repo.wf(wf_id, "dbt", "validation.json"))
    assert top["verdict"] == "PASS" and top["idempotent"] is True and top["needs_human"] is False
    assert top["sets"] == {golden_set: "PASS" for golden_set in known} and len(known) >= 3
    assert top["language"] == "sql" and top["diff_clusters"] == []
    for golden_set in known:
        assert read_json(repo.wf(wf_id, "dbt", f"validation.{golden_set}.json"))["verdict"] == "PASS"


@pytest.mark.parametrize("wf_id, reason", [
    ("wf_0006", "the objective's language is python"),
    ("wf_0005", "the objective says tier T3"),
])
def test_the_python_sample_and_the_t3_sample_are_skipped_by_both_scripts(tmp_path, capsys, wf_id, reason):
    repo = Repo(tmp_path)
    repo.wf(wf_id).mkdir(parents=True)
    shutil.copy(SAMPLES / wf_id / "canned" / "objective.json", repo.wf(wf_id, "objective.json"))
    assert export_dbt.main([wf_id, "--root", str(tmp_path)]) == 0
    assert vd.main([wf_id, "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"dbt export skipped: {reason}" in out and f"dbt validation skipped: {reason}" in out
    assert not repo.wf(wf_id, "dbt").exists()
