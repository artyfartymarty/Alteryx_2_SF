"""`lib.validation`: how the per-output `compare.py` reports of one golden set combine into one report
(`combine_outputs`, with the duplicate-`checks`-key disambiguation of ruling R-B1), the report of a build that
raised (`build_fail_report`), the shared `ordered_rows` helper behind the idempotency snapshots, and the report
files `validate_build.py` writes (objective-first design §4.8).
"""
from __future__ import annotations

import json

import pytest

from lib import validation as v
from lib.backend import DuckDBBackend
from lib.paths import Repo

OK = {"verdict": "PASS", "checks": {"row_count": "PASS"}, "diff_clusters": [],
      "normalizations_applied": [], "needs_human": False, "truncated": False}


def test_two_targets_on_one_stream_keep_both_checks():
    outputs = [({"stream": "5_Output", "kind": "target", "tool_id": "6"}, OK),
               ({"stream": "5_Output", "kind": "target", "tool_id": "7"}, OK),
               ({"stream": "3_J", "kind": "work"}, OK)]
    report = v.combine_outputs("normal", outputs, language="sql")
    assert set(report["checks"]) == {"5_Output:target:6", "5_Output:target:7", "3_J:work"}


def test_ordered_rows_sorts_by_every_column():
    b = DuckDBBackend()
    b.execute("CREATE TABLE MIG_WORK.T AS SELECT * FROM (VALUES (2, 'b'), (1, NULL), (1, 'a')) AS x(A, B)")
    assert v.ordered_rows(b, "MIG_WORK.T")[0][0] == 1
    b.close()


# --- the workflow-level build report (objective-first design §4.8) -------------------------------------------

CLUSTER = {"class": "LOGIC", "columns": ["REGION"], "scope": "columns", "count": 2}
FAILED = {**OK, "verdict": "FAIL", "checks": {"schema": "PASS", "set_diff": "FAIL"}, "diff_clusters": [CLUSTER],
          "normalizations_applied": ["trim"], "needs_human": True, "truncated": False}


def test_a_build_report_carries_the_language_and_the_worst_output_verdict():
    outputs = [({"stream": "SALES", "kind": "target", "logical": "SALES"}, OK),
               ({"stream": "EXCLUDED", "kind": "target", "logical": "EXCLUDED"}, FAILED)]
    report = v.combine_outputs("normal", outputs, language="sql")
    assert "segment" not in report and report["language"] == "sql"
    assert list(report) == ["golden_set", "language", "verdict", "checks", "diff_clusters", "normalizations_applied",
                            "idempotent", "idempotency_diff", "runtime_ms", "credits", "needs_human", "truncated"]
    assert report["normalizations_applied"] == ["trim"]
    assert report["verdict"] == "FAIL" and report["needs_human"] is True
    assert set(report["checks"]) == {"SALES:target", "EXCLUDED:target"}
    assert report["diff_clusters"] == [{**CLUSTER, "stream": "EXCLUDED"}]       # tagged with its output
    assert report["golden_set"] == "normal" and report["idempotent"] is None and report["credits"] is None


def test_a_build_fail_report_is_a_domain_failure_that_needs_no_human():
    report = v.build_fail_report("edge", RuntimeError("Binder Error: no column X"), language="python")
    assert "segment" not in report
    assert (report["verdict"], report["error"], report["language"], report["golden_set"]) == (
        "FAIL", "Binder Error: no column X", "python", "edge")
    assert report["needs_human"] is False and report["idempotent"] is None and report["diff_clusters"] == []


def test_a_missing_output_table_names_the_table_and_what_was_run():
    report = v.missing_table_report("MIGDB.MIG_WORK.T")
    assert report["verdict"] == "FAIL" and "MIGDB.MIG_WORK.T" in report["error"]
    assert report["error"].endswith("after running the build file")
    assert v.missing_table_report("MIGDB.MIG_WORK.T", ran="the build")["error"].endswith("after running the build")


def test_build_paths_and_tables_name_the_output_by_its_logical_and_tool():
    assert v.build_output_table("MIGDB", "SALES_SUMMARY") == "MIGDB.MIG_WORK.SALES_SUMMARY"
    assert v.build_output_table("SBX", "X") == "SBX.MIG_WORK.X"
    with pytest.raises(ValueError, match="logical"):
        v.build_output_table("MIGDB", "sales; DROP")
    repo = Repo("root")
    path = v.golden_output_path(repo, "wf_0001", "normal", {"tool_id": "7", "logical": "SALES_SUMMARY"})
    assert path == repo.wf("wf_0001", "golden", "outputs", "normal", "7.csv")
    with pytest.raises(ValueError, match="tool_id"):
        v.golden_output_path(repo, "wf_0001", "normal", {"logical": "SALES_SUMMARY"})


def test_stale_build_reports_are_cleared_and_nothing_else(tmp_path):
    repo = Repo(tmp_path)
    directory = repo.wf("wf_0001")
    directory.mkdir(parents=True)
    keep = ["compile_check.json", "review.json", "manifest.json"]
    for name in ("validation.json", "validation.normal.json", "validation.old_set.json", *keep):
        (directory / name).write_text("{}", encoding="utf-8")
    (directory / "dbt").mkdir()
    (directory / "dbt" / "validation.json").write_text("{}", encoding="utf-8")
    v.clear_stale_build_reports(repo, "wf_0001")
    assert sorted(path.name for path in directory.iterdir() if path.is_file()) == sorted(keep)
    assert (directory / "dbt" / "validation.json").is_file()                 # the dbt export's own report
    v.clear_stale_build_reports(repo, "wf_0404")                             # no such workflow: nothing to do


def _set_report(golden_set: str, base: dict, idempotent: bool | None, diff: list[str] | None = None) -> dict:
    """One golden set's report as a validator hands it over: its own idempotency already on it."""
    report = v.combine_outputs(golden_set, [({"stream": "T", "kind": "target"}, base)], language="sql")
    report["idempotent"], report["idempotency_diff"] = idempotent, diff or []
    return report


def test_write_build_reports_keeps_each_sets_own_idempotency_and_ands_them_at_the_top(tmp_path):
    repo = Repo(tmp_path)
    reports = {"normal": _set_report("normal", OK, True),
               "edge": _set_report("edge", FAILED, False, ["MIGDB.MIG_WORK.T"])}
    result = v.write_build_reports(repo, "wf_0001", ["normal", "edge"], reports, language="sql")
    assert result["verdict"] == "FAIL" and result["language"] == "sql" and "segment" not in result
    assert result["sets"] == {"normal": "PASS", "edge": "FAIL"} and result["needs_human"] is True
    assert result["idempotent"] is False and result["idempotency_diff"] == ["MIGDB.MIG_WORK.T"]
    written = {name: json.loads((repo.wf("wf_0001") / name).read_text(encoding="utf-8"))
               for name in ("validation.json", "validation.normal.json", "validation.edge.json")}
    assert written["validation.json"] == result
    assert written["validation.normal.json"]["idempotent"] is True and written["validation.normal.json"]["idempotency_diff"] == []
    assert written["validation.edge.json"]["idempotent"] is False
    assert written["validation.edge.json"]["idempotency_diff"] == ["MIGDB.MIG_WORK.T"]
    for name in ("validation.normal.json", "validation.edge.json"):
        assert written[name]["language"] == "sql"
        assert "sets" not in written[name]                                   # only the top-level report has it


def test_the_top_level_idempotent_is_the_and_over_the_sets_and_none_claims_nothing():
    sets = ["a", "b", "c"]
    make = lambda *flags: {name: _set_report(name, OK, flag, ["T"] if flag is False else []) for name, flag in zip(sets, flags)}
    assert v.idempotency_of(sets, make(True, True, True)) == (True, [])
    assert v.idempotency_of(sets, make(True, False, True)) == (False, ["T"])
    assert v.idempotency_of(sets, make(True, None, True)) == (None, []), "a set that never compared claims nothing"
    assert v.idempotency_of(sets, make(None, False, True)) == (False, ["T"]), "one set that diverged is enough"
    assert v.idempotency_of(sets, make(False, False, True)) == (False, ["T"]), "a table is named once"
    assert v.idempotency_of([], {}) == (None, [])


def test_the_report_writer_and_the_stale_clearer_take_another_directory(tmp_path):
    """`validate_dbt.py` writes the same reports into `workflows/<wf>/dbt/`; the workflow's own are not touched."""
    repo = Repo(tmp_path)
    directory = repo.wf("wf_0001")
    project = directory / "dbt"
    project.mkdir(parents=True)
    (directory / "validation.json").write_text('{"keep": true}', encoding="utf-8")
    (project / "validation.old_set.json").write_text("{}", encoding="utf-8")
    (project / "dbt_project.yml").write_text("name: x\n", encoding="utf-8")

    v.clear_stale_build_reports(repo, "wf_0001", directory=project)
    assert sorted(path.name for path in project.iterdir()) == ["dbt_project.yml"]
    assert (directory / "validation.json").read_text(encoding="utf-8") == '{"keep": true}'

    reports = {"normal": v.combine_outputs("normal", [({"stream": "T", "kind": "target"}, OK)], language="sql")}
    reports["normal"]["idempotent"] = True
    result = v.write_build_reports(repo, "wf_0001", ["normal"], reports, language="sql", directory=project)
    assert result["verdict"] == "PASS" and result["idempotent"] is True
    assert sorted(path.name for path in project.glob("validation*.json")) == ["validation.json", "validation.normal.json"]
    assert json.loads((project / "validation.json").read_text(encoding="utf-8")) == result
    assert (directory / "validation.json").read_text(encoding="utf-8") == '{"keep": true}'
