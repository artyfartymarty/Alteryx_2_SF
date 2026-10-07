"""`compile_check.py` on a `build.py` (objective-first design §4.5): the Snowpark static rules with the
`main(session, src, tgt)` entry point, `# step sN` markers, no `$$`, and the run of `main` on empty input
tables in the runtime sandbox (`run`, `outputs:columns`).

The rules themselves (every name the gate refuses, every evasion probe) are `test_snowpark_rules.py`'s;
this file pins the wiring: which check reports what, in what shape, and that a static error stops the run.
The runtime tests start a Snowpark Local Testing child process each, so there are few of them.
"""
from __future__ import annotations

import pytest

import compile_check as cc
from lib.io import read_json
from tests.build_fixtures import WF, check, make, named, objective

GOOD_PY = '''# wf_0009 - Items. Written from objective.md.
from snowflake.snowpark.functions import col


def main(session, src: str, tgt: str) -> str:
    # step s1
    items = session.table(f"{src}.ITEMS").filter(col("NOTE").is_not_null())
    # step s2
    items.select("ID", "NOTE").write.mode("overwrite").save_as_table(f"{tgt}.ITEMS_OUT")
    return "OK"
'''


def py(root, build: str = GOOD_PY, obj: dict | None = None) -> dict:
    return check(root, build, obj if obj is not None else objective(language="python"), name="build.py")


def only(report: dict, name: str) -> str:
    found = named(report, name)
    assert len(found) == 1, report["errors"]
    return found[0]


def test_a_good_build_py_compiles_runs_on_empty_inputs_and_has_no_lint(tmp_path):
    repo = make(tmp_path, GOOD_PY, objective(language="python"), name="build.py")
    report = cc.compile_check(repo, WF)
    assert report == {"ok": True, "errors": []}
    assert read_json(repo.wf(WF, "compile_check.json")) == report


def test_a_static_rule_error_is_one_line_with_its_rule_name_and_line_and_the_run_is_skipped(tmp_path):
    report = py(tmp_path, GOOD_PY.replace("import col", "import col\nimport os"))
    error = only(report, "rule:imports")
    assert error.startswith("rule:imports: line 3:") and "`os`" in error and "\n" not in error
    assert named(report, "run") == [] and named(report, "outputs:columns") == []


def test_the_entry_point_is_main_with_session_src_tgt(tmp_path):
    report = py(tmp_path, GOOD_PY.replace("def main(session, src: str, tgt: str)", "def main(session, src, tgt, extra)"))
    error = only(report, "rule:signature")
    assert "main(session, src, tgt)" in error and error.startswith("rule:signature: line 5")


def test_the_old_procedure_entry_point_is_not_accepted(tmp_path):
    report = py(tmp_path, GOOD_PY.replace("def main(session, src: str, tgt: str)",
                                          "def run(session, src_db, src_schema, tgt_db, tgt_schema, run_id)"))
    assert "main(session, src, tgt)" in only(report, "rule:signature")


@pytest.mark.parametrize("table, mention", [
    ('f"{src}.UNMAPPED"', "UNMAPPED"),
    ('f"{tgt}.ITEMS"', "ITEMS"),
    ('"SALES.RAW.ITEMS"', "SALES.RAW.ITEMS"),
])
def test_table_names_must_be_logicals_the_objective_declares(tmp_path, table, mention):
    report = py(tmp_path, GOOD_PY.replace('f"{src}.ITEMS"', table))
    assert mention in only(report, "rule:table_names")


def test_a_source_may_never_be_written(tmp_path):
    report = py(tmp_path, GOOD_PY.replace('f"{tgt}.ITEMS_OUT"', 'f"{src}.ITEMS"'))
    errors = named(report, "rule:table_names")
    assert errors and "is a source" in errors[0]


def test_every_output_is_written_in_its_write_mode(tmp_path):
    report = py(tmp_path, GOOD_PY.replace('mode("overwrite")', 'mode("append")'))
    error = only(report, "rule:write_mode")
    assert "ITEMS_OUT is overwrite" in error and "{tgt}.ITEMS_OUT" in error


def test_an_output_the_module_never_writes_is_named(tmp_path):
    report = py(tmp_path, GOOD_PY.replace('.write.mode("overwrite").save_as_table(f"{tgt}.ITEMS_OUT")', ".collect()"))
    assert "never writes" in only(report, "rule:write_mode")


def test_a_dollar_quote_is_refused_with_its_line(tmp_path):
    error = only(py(tmp_path, GOOD_PY.replace("# step s1", '# step s1\n    note = "$$"')), "python:dollar_quote")
    assert error.startswith("python:dollar_quote: line 7") and "$$" in error and "deploy.py" in error


def test_step_markers_use_hash_comments(tmp_path):
    report = py(tmp_path, GOOD_PY.replace("    # step s2\n", ""))
    assert "# step s2" in only(report, "steps:missing")
    report = py(tmp_path / "b", GOOD_PY.replace("# step s2", "# step s2, s9"))
    assert "s9" in only(report, "steps:unknown")
    report = py(tmp_path / "c", GOOD_PY.replace("# step s1", "# Step s1: read"))
    assert only(report, "steps:marker").startswith("steps:marker: line 6")


def test_python_has_no_lint_key(tmp_path):
    assert "lint" not in py(tmp_path)


# --- the run on empty inputs ---------------------------------------------------------------------------

def test_a_main_that_raises_on_empty_inputs_is_a_run_error(tmp_path):
    report = py(tmp_path, GOOD_PY.replace('    return "OK"', '    raise ValueError("boom")\n    return "OK"'))
    error = only(report, "run")
    assert "boom" in error and "\n" not in error


def test_an_output_with_the_wrong_columns_is_named(tmp_path):
    report = py(tmp_path, GOOD_PY.replace('select("ID", "NOTE")', 'select("NOTE", "ID")'))
    error = only(report, "outputs:columns")
    assert "ITEMS_OUT" in error and "[NOTE, ID]" in error and "[ID, NOTE]" in error


def test_an_append_target_exists_empty_before_main_runs(tmp_path):
    build = GOOD_PY.replace('mode("overwrite")', 'mode("append")')
    report = py(tmp_path, build, objective(language="python", write_mode="append"))
    assert report["ok"] is True, report["errors"]


def test_a_python_objective_with_a_build_py_missing_a_declared_input_column_fails_the_run(tmp_path):
    report = py(tmp_path, GOOD_PY.replace('col("NOTE")', 'col("MISSING_COLUMN")'))
    assert "MISSING_COLUMN" in only(report, "run") or "MISSING" in only(report, "run")


def test_a_sql_objective_with_a_build_py_is_a_language_error(tmp_path):
    report = check(tmp_path, GOOD_PY, objective(language="sql"), name="build.py")
    assert "language" in only(report, "language") and report["ok"] is False


# --- the sandbox entry the Python run uses -----------------------------------------------------------------

COUNT_MODULE = '''from snowflake.snowpark.types import LongType, StructField, StructType


def main(session, src: str, tgt: str) -> str:
    counted = session.table(f"{src}.T").count()
    schema = StructType([StructField("N", LongType())])
    session.create_dataframe([[counted]], schema=schema).write.mode("overwrite").save_as_table(f"{tgt}.COUNTED")
    return "OK"
'''


def _run_build(**kwargs):
    from lib import snowpark_sandbox
    columns = [{"name": "A", "type": "NUMBER(38,0)"}, {"name": "B", "type": "VARIANT"}]   # VARIANT: no mapping
    return snowpark_sandbox.run_build(
        source=COUNT_MODULE, display_path="build.py", src="MIGDB.MIG_COMPILE", tgt="MIGDB.MIG_WORK",
        tables=[{"fqn": "MIGDB.MIG_COMPILE.T", "columns": columns}], outputs=["MIGDB.MIG_WORK.COUNTED"], **kwargs)


def test_run_build_calls_main_on_empty_tables_and_reads_the_outputs_back():
    result = _run_build()
    assert result.kind == "ok", result.error
    assert result.outputs["MIGDB.MIG_WORK.COUNTED"]["rows"] == [[0]]


def test_run_build_can_put_one_row_of_nulls_in_each_table_instead():
    result = _run_build(null_row=True)
    assert result.kind == "ok", result.error
    assert result.outputs["MIGDB.MIG_WORK.COUNTED"]["rows"] == [[1]]
