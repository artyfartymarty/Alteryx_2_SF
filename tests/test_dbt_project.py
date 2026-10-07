"""scripts/lib/dbt_project.py: the one way dbt is run, and the names a local run uses."""
from __future__ import annotations

import subprocess
import sysconfig
from pathlib import Path

import pytest

from lib import dbt_project as dp
from lib.paths import Repo

PROFILES = """\
mini:
  target: local
  outputs:
    local:
      type: duckdb
      path: "{{ env_var('MIG_DBT_DUCKDB_PATH', 'dbt_sandbox.duckdb') }}"
      schema: "{{ var('tgt_schema') }}"
      threads: 1
"""
PROJECT = """\
name: mini
version: "1.0.0"
config-version: 2
profile: mini
model-paths: [models]
vars:
  src_schema: null
  tgt_schema: null
"""
SCHEMA = """\
version: 2
models:
  - name: good
    columns:
      - name: ID
        data_tests: [not_null]
"""


def mini_project(root: Path, models: dict[str, str], schema: str | None = None) -> Path:
    """A tiny dbt project -- the profile every exported project has, and the given models -- under `root/proj`."""
    project = root / "proj"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(PROJECT, encoding="utf-8", newline="\n")
    (project / "profiles.yml").write_text(PROFILES, encoding="utf-8", newline="\n")
    for name, sql in models.items():
        (project / "models" / f"{name}.sql").write_text(sql, encoding="utf-8", newline="\n")
    if schema is not None:
        (project / "models" / "schema.yml").write_text(schema, encoding="utf-8", newline="\n")
    return project


VARS = {"src_schema": "MIGDB__MIG_GOLDEN_X", "tgt_schema": "MIGDB__MIG_WORK"}


def test_local_vars_are_the_flattened_duckdb_schema_names():
    assert dp.local_vars("MIG_GOLDEN_WF0009_NORMAL") == {
        "src_schema": "MIGDB__MIG_GOLDEN_WF0009_NORMAL", "tgt_schema": "MIGDB__MIG_WORK"}


def test_sandbox_path_is_a_catalog_friendly_file_name(tmp_path):
    path = dp.sandbox_path(Repo(tmp_path), "wf_0009", "period_end", "_rerun")
    assert path == Repo(tmp_path).wf("wf_0009", "dbt_sandbox_period_end_rerun.duckdb")
    assert not path.name.startswith(".") and path.name.count(".") == 1


def test_the_project_lives_in_the_workflows_dbt_directory_and_is_named_after_it(tmp_path):
    assert dp.project_dir(Repo(tmp_path), "wf_0009") == Repo(tmp_path).wf("wf_0009", "dbt")
    assert dp.project_name("wf_0009") == "mig_wf_0009"
    assert dp.project_name("WF-12.b") == "mig_wf_12_b", "a dbt project name is letters, digits and underscores"


def test_node_names_come_from_the_unique_id_whether_it_is_a_model_or_a_test():
    assert dp._name_of("model.mini.good") == "good"
    assert dp._name_of("test.mini.not_null_good_ID.0a1b2c3d4e") == "not_null_good_ID"


def test_the_result_tells_failed_models_from_failed_tests_and_skipped_models():
    result = dp.DbtResult(1, "", [
        {"name": "a", "unique_id": "model.p.a", "status": "success", "message": ""},
        {"name": "b", "unique_id": "model.p.b", "status": "error", "message": "boom"},
        {"name": "c", "unique_id": "model.p.c", "status": "skipped", "message": ""},
        {"name": "not_null_a_ID", "unique_id": "test.p.not_null_a_ID.abc", "status": "fail", "message": "Got 2 results"},
        {"name": "unique_a_ID", "unique_id": "test.p.unique_a_ID.def", "status": "pass", "message": ""},
    ], None)
    assert result.failed_models == ["b"] and result.skipped_models == ["c"]
    assert result.failed_tests == ["not_null_a_ID"] and not result.ok


def test_run_dbt_runs_the_project_and_writes_nothing_into_it_but_the_log(tmp_path):
    project = mini_project(tmp_path, {"good": "select 1 as ID"})
    before = sorted(p.relative_to(project).as_posix() for p in project.rglob("*"))
    log = project / "logs" / "validate_normal.log"
    result = dp.run_dbt("build", project, vars=VARS, duckdb_path=tmp_path / "sandbox.duckdb", log_file=log)
    assert result.ok and result.code == 0 and result.failed_models == [] and result.failed_tests == []
    assert [r["name"] for r in result.results] == ["good"]
    after = sorted(p.relative_to(project).as_posix() for p in project.rglob("*"))
    assert after == sorted(before + ["logs", "logs/validate_normal.log"])
    text = log.read_text(encoding="utf-8")
    assert "\x1b[" not in text and "\r\n" not in text and str(tmp_path) not in result.output


def test_run_dbt_names_the_model_that_failed(tmp_path):
    project = mini_project(tmp_path, {"bad": "select NO_SUCH_COLUMN from (select 1 as ID)"})
    result = dp.run_dbt("build", project, vars=VARS, duckdb_path=tmp_path / "sandbox.duckdb")
    assert result.code == 1 and result.failed_models == ["bad"] and result.failed_tests == []
    assert "NO_SUCH_COLUMN" in next(r["message"] for r in result.results if r["name"] == "bad")


def test_run_dbt_names_the_test_that_failed_and_the_model_it_did_not_fail(tmp_path):
    project = mini_project(tmp_path, {"good": "select cast(null as integer) as ID"}, SCHEMA)
    result = dp.run_dbt("build", project, vars=VARS, duckdb_path=tmp_path / "sandbox.duckdb")
    assert result.code == 1 and result.failed_models == [] and result.failed_tests == ["not_null_good_ID"]
    assert "Got 1 result" in next(r["message"] for r in result.results if r["name"] == "not_null_good_ID")


def test_run_dbt_checks_nothing_about_the_projects_files(tmp_path):
    """The closed surface of the agent-written projects is gone: a script wrote every file, and a project is run as it is."""
    project = mini_project(tmp_path, {"good": "select {{ answer() }} as ID"})
    (project / "macros").mkdir()
    (project / "macros" / "answer.sql").write_text("{% macro answer() %}42{% endmacro %}\n", encoding="utf-8")
    result = dp.run_dbt("build", project, vars=VARS, duckdb_path=tmp_path / "sandbox.duckdb")
    assert result.ok, result.output


def test_run_dbt_turns_telemetry_and_colours_off(tmp_path, monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["env"], seen["cwd"] = args, kwargs["env"], kwargs["cwd"]
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    dp.run_dbt("parse", tmp_path, vars={"src_schema": "S", "tgt_schema": "T"}, duckdb_path=tmp_path / "x.duckdb")
    assert seen["env"]["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "false" and seen["env"]["DO_NOT_TRACK"] == "1"
    assert seen["env"][dp.DUCKDB_PATH_ENV] == str((tmp_path / "x.duckdb").resolve())
    for flag in ("--no-use-colors", "--target-path", "--log-path", "--profiles-dir", "--vars"):
        assert flag in seen["args"]
    assert seen["args"][1] == "parse" and seen["args"][seen["args"].index("--vars") + 1] == '{"src_schema": "S", "tgt_schema": "T"}'


def test_flags_are_passed_through_after_the_command(tmp_path, monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    dp.run_dbt("build", tmp_path, vars={}, duckdb_path=None, flags=["--full-refresh"])
    assert seen["args"][1] == "build" and "--full-refresh" in seen["args"]
    dp.run_dbt("build", tmp_path, vars={}, duckdb_path=None)
    assert "--full-refresh" not in seen["args"]


def _log_sandbox(tmp_path: Path) -> Path:
    """A DuckDB sandbox with an empty `MIGDB.MIG_WORK.LOG (WHO)` table, which a hook can insert into."""
    from lib.backend import DuckDBBackend
    sandbox = tmp_path / "sandbox.duckdb"
    backend = DuckDBBackend(str(sandbox))
    try:
        backend.execute("CREATE TABLE MIGDB.MIG_WORK.LOG (WHO VARCHAR)")
    finally:
        backend.close()
    return sandbox


def _logged(sandbox: Path) -> list[str]:
    from lib.backend import DuckDBBackend
    backend = DuckDBBackend(str(sandbox))
    try:
        return [row[0] for row in backend.query("SELECT WHO FROM MIGDB.MIG_WORK.LOG ORDER BY rowid")[1]]
    finally:
        backend.close()


def test_a_project_level_pre_hook_runs_before_a_models_own_pre_hook_and_a_guard_can_render_to_nothing(tmp_path):
    """What the exporter relies on for the build's ALTER SESSION: a `+pre-hook` in `dbt_project.yml` runs on the model's
    own connection BEFORE the `pre_hook` of its `config()` (dbt-core 1.12: `context_config.py` applies the project's
    configs first, then the model's call; `MergeBehavior.Append` is `project + model`), and a Jinja guard on the
    target type renders to nothing where it does not hold (the DuckDB double has no sessions)."""
    project = mini_project(tmp_path, {
        "good": "{{ config(materialized='table', pre_hook=\"insert into {{ this.schema }}.log values ('model')\") }}\n"
                "select 1 as ID"})
    yml = project / "dbt_project.yml"
    yml.write_text(yml.read_text(encoding="utf-8") + (
        "models:\n"
        "  +pre-hook:\n"
        "    - \"{% if target.type == 'duckdb' %}insert into {{ target.schema }}.log values ('project'){% endif %}\"\n"
        "    - \"{% if target.type == 'snowflake' %}insert into {{ target.schema }}.log values ('snowflake'){% endif %}\"\n"),
        encoding="utf-8", newline="\n")
    sandbox = _log_sandbox(tmp_path)
    result = dp.run_dbt("build", project, vars=VARS, duckdb_path=sandbox)
    assert result.ok, result.output
    assert _logged(sandbox) == ["project", "model"]


def test_full_refresh_false_keeps_an_incremental_model_out_of_a_full_refresh(tmp_path):
    """`dbt build --full-refresh` rebuilds an incremental model unless its config says `full_refresh=false`: the one
    that says so keeps the history the build relies on, the other is rebuilt."""
    kept = "{{ config(materialized='incremental', incremental_strategy='append', full_refresh=false) }}\nselect 1 as ID"
    rebuilt = "{{ config(materialized='incremental', incremental_strategy='append') }}\nselect 1 as ID"
    project = mini_project(tmp_path, {"kept": kept, "rebuilt": rebuilt})
    sandbox = tmp_path / "sandbox.duckdb"
    for flags in ([], [], ["--full-refresh"]):               # create, append, then a full refresh
        assert dp.run_dbt("build", project, vars=VARS, duckdb_path=sandbox, flags=flags).ok
    from lib.backend import DuckDBBackend
    backend = DuckDBBackend(str(sandbox))
    try:
        counts = {name: backend.query(f"SELECT COUNT(*) FROM MIGDB.MIG_WORK.{name}")[1][0][0] for name in ("kept", "rebuilt")}
    finally:
        backend.close()
    assert counts == {"kept": 3, "rebuilt": 1}


def test_a_missing_console_script_is_dbt_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(tmp_path))
    with pytest.raises(dp.DbtUnavailable, match="dbt"):
        dp.dbt_executable()
