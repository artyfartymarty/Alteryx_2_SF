"""`compile_check.py` on a `build.sql`: one test per named check of design §4.4 (a minimal bad file, one
named check saying so, in one line with its line number), the report of §4.6, and the CLI's exit codes.

The fixture is `tests/build_fixtures.py`: `wf_0009`, one input (ITEMS), one output (ITEMS_OUT), two steps.
Write-mode forms have their own file (`test_compile_check_write_mode.py`), the Snowpark path another
(`test_compile_check_python.py`), the seven canned builds a third (`test_canned_builds.py`).

Nothing here has run on Snowflake: passing means sqlglot's Snowflake parser and DuckDB accepted the file.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import compile_check as cc
from lib.io import read_json, write_yaml
from lib.paths import Repo
from tests import test_export_dbt as exd
from tests.build_fixtures import GOOD_SQL, WF, check, dbt_off, make, named, objective


def only(report: dict, name: str) -> str:
    found = named(report, name)
    assert len(found) == 1, report["errors"]
    return found[0]


# --- a file that satisfies every rule ---------------------------------------------------------------

def test_a_good_build_compiles_and_the_report_is_written(tmp_path):
    repo = make(tmp_path)
    report = cc.compile_check(repo, WF)
    assert report["ok"] is True and report["errors"] == []
    assert report["lint"]["statements"] == 3 and report["lint"]["ctes"] == 1 and report["lint"]["flags"] == []
    assert report["lint"]["lines"] == len(GOOD_SQL.splitlines())
    assert read_json(repo.wf(WF, "compile_check.json")) == report


def test_the_report_has_exactly_the_keys_of_design_4_6(tmp_path):
    assert sorted(check(tmp_path)) == ["errors", "lint", "ok"]
    assert sorted(check(tmp_path / "again")["lint"]) == ["ctes", "flags", "lines", "statements"]


def test_spaces_inside_the_braces_are_allowed(tmp_path):
    build = GOOD_SQL.replace("{{src}}", "{{ src }}").replace("{{tgt}}", "{{   tgt }}")
    assert check(tmp_path, build)["ok"] is True


# --- template:placeholder --------------------------------------------------------------------------

@pytest.mark.parametrize("build, mention", [
    (GOOD_SQL.replace("{{src}}.ITEMS", "{{schema}}.ITEMS"), "{{ schema }}"),
    (GOOD_SQL.replace("{{src}}.ITEMS", "{{ src.ITEMS"), "never closed"),
    (GOOD_SQL.replace("FROM {{src}}.ITEMS", "FROM {{src}}"), "is not followed by .<LOGICAL>"),
    (GOOD_SQL.replace("{{tgt}}.ITEMS_OUT", "{{tgt}}"), "is not followed by .<LOGICAL>"),
    (GOOD_SQL.replace("-- step s1", "-- step s1 {{database}}"), "{{ database }}"),
], ids=["unknown name", "unclosed", "bare src", "bare tgt", "unknown name in a comment"])
def test_template_placeholder_names_the_line_and_the_two_allowed_placeholders(tmp_path, build, mention):
    error = only(check(tmp_path, build), "template:placeholder")
    assert error.startswith("template:placeholder: line ") and mention in error
    assert "{{src}}" in error and "{{tgt}}" in error and "\n" not in error


def test_the_header_comment_may_name_both_placeholders_without_a_table(tmp_path):
    assert "{{src}} = source schema" in GOOD_SQL and check(tmp_path)["ok"] is True


# --- tables:source / tables:target / tables:temporary -----------------------------------------------

def test_tables_source_refuses_a_source_that_is_not_an_input_logical(tmp_path):
    error = only(check(tmp_path, GOOD_SQL.replace("{{src}}.ITEMS", "{{src}}.UNMAPPED")), "tables:source")
    assert "line 7" in error and "UNMAPPED" in error and "ITEMS" in error


def test_tables_source_refuses_a_table_named_without_the_placeholder(tmp_path):
    error = only(check(tmp_path, GOOD_SQL.replace("{{src}}.ITEMS", "MYDB.RAW.ITEMS")), "tables:source")
    assert "MYDB.RAW.ITEMS" in error and "{{src}}" in error


def test_tables_source_refuses_writing_a_source(tmp_path):
    build = GOOD_SQL + "\nINSERT INTO {{src}}.ITEMS (ID, NOTE) SELECT ID, NOTE FROM kept;\n"
    error = only(check(tmp_path, build), "tables:source")
    assert "only read" in error and "line 13" in error


def test_tables_target_refuses_a_target_that_is_not_an_output_logical(tmp_path):
    build = GOOD_SQL.replace("{{tgt}}.ITEMS_OUT", "{{tgt}}.OTHER")
    error = only(check(tmp_path, build), "tables:target")
    assert "OTHER" in error and "ITEMS_OUT" in error and "line 10" in error


def test_tables_target_refuses_a_table_written_without_the_placeholder(tmp_path):
    error = only(check(tmp_path, GOOD_SQL.replace("{{tgt}}.ITEMS_OUT", "ANALYTICS.CURATED.ITEMS_OUT")), "tables:target")
    assert "ANALYTICS.CURATED.ITEMS_OUT" in error and "{{tgt}}" in error


def test_a_target_may_be_read_after_it_is_written(tmp_path):
    build = GOOD_SQL + "\nCREATE OR REPLACE TEMPORARY TABLE again AS SELECT ID FROM {{tgt}}.ITEMS_OUT WHERE ID > 0;\n"
    assert named(check(tmp_path, build), "tables:target") == []


def test_a_target_read_before_it_is_written_is_refused(tmp_path):
    build = GOOD_SQL.replace("-- step s2\n", "-- step s2\nCREATE OR REPLACE TEMPORARY TABLE early AS SELECT ID FROM {{tgt}}.ITEMS_OUT;\n")
    error = only(check(tmp_path, build), "tables:target")
    assert "before it is written" in error and "line 10" in error


def test_an_overwrite_target_may_not_read_itself_in_its_own_write(tmp_path):
    build = GOOD_SQL.replace("FROM kept;", "FROM kept UNION ALL SELECT ID, NOTE FROM {{tgt}}.ITEMS_OUT;")
    assert "before it is written" in only(check(tmp_path, build), "tables:target")


def test_an_append_target_may_be_read_by_its_own_write(tmp_path):
    build = GOOD_SQL.replace("CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS\nSELECT ID, NOTE FROM kept;",
                             "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\n"
                             "SELECT ID, NOTE FROM kept WHERE ID NOT IN (SELECT ID FROM {{tgt}}.ITEMS_OUT);")
    assert named(check(tmp_path, build, objective(write_mode="append")), "tables:target") == []


def test_tables_temporary_refuses_a_name_read_before_any_create_temporary_table(tmp_path):
    error = only(check(tmp_path, GOOD_SQL.replace("FROM kept;", "FROM nothing;")), "tables:temporary")
    assert "nothing" in error and "line 11" in error and "CREATE" in error


def test_tables_temporary_refuses_a_temporary_table_created_after_it_is_read(tmp_path):
    build = GOOD_SQL.replace("-- step s2\n", "-- step s2\nCREATE OR REPLACE TEMPORARY TABLE early AS SELECT ID FROM later;\n"
                             ) + "\nCREATE OR REPLACE TEMPORARY TABLE later AS SELECT ID FROM kept;\n"
    assert "later" in only(check(tmp_path, build), "tables:temporary")


@pytest.mark.parametrize("create, name", [
    ("CREATE OR REPLACE TABLE kept AS", "kept"),                            # not TEMPORARY
    ("CREATE OR REPLACE TEMPORARY TABLE items AS", "items"),                # a logical name (ITEMS)
    ("CREATE OR REPLACE TEMPORARY TABLE items_out AS", "items_out"),        # a logical name (ITEMS_OUT)
    ("CREATE OR REPLACE TEMPORARY TABLE {{tgt}}.kept AS", "{{tgt}}.kept"),  # qualified
])
def test_tables_temporary_refuses_a_temporary_table_that_is_not_plain(tmp_path, create, name):
    build = GOOD_SQL.replace("CREATE OR REPLACE TEMPORARY TABLE kept AS", create)
    error = named(check(tmp_path, build), "tables:temporary")[0]
    assert error.startswith("tables:temporary: line 6") and name in error and "\n" not in error


def test_a_transient_table_is_not_one_of_the_statement_kinds(tmp_path):
    build = GOOD_SQL.replace("CREATE OR REPLACE TEMPORARY TABLE kept AS", "CREATE OR REPLACE TRANSIENT TABLE kept AS")
    assert "TRANSIENT" in only(check(tmp_path, build), "statement:kind")


def test_a_cte_name_is_not_a_table(tmp_path):
    build = GOOD_SQL.replace("SELECT ID, NOTE FROM kept;",
                             "WITH shaped AS (SELECT ID, NOTE FROM kept WHERE ID > 0) SELECT ID, NOTE FROM shaped;")
    report = check(tmp_path, build)
    assert report["ok"] is True, report["errors"]


# --- session:form ---------------------------------------------------------------------------------

@pytest.mark.parametrize("statement, mention", [
    ("ALTER SESSION SET TIMEZONE = CURRENT_DATE;", "TIMEZONE"),            # not a literal
    ("ALTER SESSION SET QUERY_TAG = 'x';", "QUERY_TAG"),                  # not on the list
    ("ALTER SESSION UNSET TIMEZONE;", "ALTER SESSION"),
    ("ALTER SESSION SET TIMEZONE;", "ALTER SESSION"),
    ("ALTER SESSION SET = 'UTC';", "ALTER SESSION"),
])
def test_session_form_names_the_bad_alter_session(tmp_path, statement, mention):
    build = GOOD_SQL.replace("ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;", statement)
    error = only(check(tmp_path, build), "session:form")
    assert error.startswith("session:form: line 3") and mention in error


def test_session_form_wants_alter_session_first(tmp_path):
    build = GOOD_SQL.replace("ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;\n", "") + \
        "\nALTER SESSION SET TIMEZONE = 'UTC';\n"
    error = only(check(tmp_path, build), "session:form")
    assert "first statement" in error and "line 12" in error


def test_a_second_alter_session_is_refused(tmp_path):
    build = GOOD_SQL.replace("WEEK_START = 1;", "WEEK_START = 1;\nALTER SESSION SET TIMEZONE = 'UTC';")
    assert "first statement" in only(check(tmp_path, build), "session:form")


def test_session_parameters_come_from_the_programs_session_block_too(tmp_path):
    (tmp_path / "mappings").mkdir()
    (tmp_path / "mappings" / "global.yaml").write_text("session:\n  TIMEZONE: UTC\n  QUERY_TAG: x\n", encoding="utf-8")
    build = GOOD_SQL.replace("WEEK_START = 1;", "QUERY_TAG = 'x';")
    assert named(check(tmp_path, build), "session:form") == []


def test_a_build_with_no_alter_session_is_fine(tmp_path):
    assert check(tmp_path, GOOD_SQL.replace("ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;\n", ""))["ok"]


# --- statement:kind -------------------------------------------------------------------------------

@pytest.mark.parametrize("statement", [
    "CALL some_proc('x')",
    "EXECUTE IMMEDIATE 'SELECT 1'",
    "LET total := 1",
    "DECLARE total INT",
    "CREATE OR REPLACE PROCEDURE p() RETURNS STRING LANGUAGE SQL AS $$ BEGIN RETURN 'x'; END; $$",
    "DROP TABLE kept",
    "SELECT ID FROM kept",
    "CREATE OR REPLACE VIEW v AS SELECT ID FROM kept",
    "CREATE OR REPLACE TEMPORARY TABLE other (ID NUMBER)",
    "USE DATABASE somewhere",
    "TRUNCATE TABLE kept",
    "DELETE FROM kept WHERE ID < 0",
    "UPDATE kept SET NOTE = 'x'",
])
def test_statement_kind_refuses_everything_outside_the_list(tmp_path, statement):
    error = only(check(tmp_path, GOOD_SQL + f"\n{statement};\n"), "statement:kind")
    assert error.startswith("statement:kind: line 13") and "\n" not in error


def test_the_allowed_statement_kinds_are_not_refused(tmp_path):
    build = GOOD_SQL + """
INSERT INTO kept (ID, NOTE) SELECT ID, NOTE FROM kept;
"""
    assert named(check(tmp_path, build), "statement:kind") == []


# --- external_access ------------------------------------------------------------------------------

@pytest.mark.parametrize("statement", [
    "COPY INTO @stage/out FROM kept",
    "PUT file:///tmp/x.csv @stage",
    "GET @stage/x.csv file:///tmp",
    "CREATE OR REPLACE STAGE s",
    "CREATE OR REPLACE FILE FORMAT f TYPE = CSV",
    "CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT * FROM read_csv('/etc/passwd')",
    "CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT * FROM '/etc/passwd'",
    "CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT * FROM @mystage/data.csv",
    "CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT SYSTEM$WHITELIST() AS U FROM kept",
    "SET x = 1",
])
def test_external_access_refuses_stages_files_and_the_network(tmp_path, statement):
    report = check(tmp_path, GOOD_SQL + f"\n{statement};\n")
    error = only(report, "external_access")
    assert error.startswith("external_access: line 13") and "\n" not in error
    assert report["ok"] is False


def test_a_file_function_in_a_comment_or_a_string_is_not_external_access(tmp_path):
    build = GOOD_SQL.replace("-- step s2\n", "-- step s2\n-- COPY INTO @s; read_csv('x')\n").replace(
        "WHERE NOTE IS NOT NULL", "WHERE NOTE <> 'read_csv(x)'")
    assert named(check(tmp_path, build), "external_access") == []


# --- parse ----------------------------------------------------------------------------------------

def test_parse_names_the_statement_that_is_not_snowflake_sql(tmp_path):
    error = only(check(tmp_path, GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELEC ID, NOTE FROM kept;")), "parse")
    assert error.startswith("parse: line 1") and "SELEC" in error and "\n" not in error
    assert "\x1b" not in error


def test_parse_names_an_unbalanced_parenthesis(tmp_path):
    error = only(check(tmp_path, GOOD_SQL.replace("WHERE NOTE IS NOT NULL;", "WHERE (NOTE IS NOT NULL;")), "parse")
    assert error.startswith("parse: line ") and "\n" not in error


# --- steps:missing / steps:unknown / steps:marker ---------------------------------------------------

def test_steps_missing_names_each_step_without_a_region(tmp_path):
    build = GOOD_SQL.replace("-- step s2\n", "")
    error = only(check(tmp_path, build), "steps:missing")
    assert "s2" in error and "-- step s2" in error


def test_a_marker_with_no_code_after_it_is_no_region(tmp_path):
    build = GOOD_SQL.replace("-- step s2\n", "-- step s2\n-- step s1\n")
    assert "s2" in only(check(tmp_path, build), "steps:missing")


def test_a_step_covered_by_a_shared_marker_has_a_region(tmp_path):
    build = GOOD_SQL.replace("-- step s1\n", "-- step s1, s2\n").replace("-- step s2\n", "")
    assert named(check(tmp_path, build), "steps:missing") == []


def test_steps_unknown_names_the_marker_and_its_line(tmp_path):
    error = only(check(tmp_path, GOOD_SQL.replace("-- step s2", "-- step s2, s9")), "steps:unknown")
    assert "s9" in error and "line 9" in error


def test_steps_unknown_also_fires_for_a_marker_with_no_code(tmp_path):
    error = only(check(tmp_path, GOOD_SQL + "\n-- step s7\n"), "steps:unknown")
    assert "s7" in error


@pytest.mark.parametrize("comment", ["-- Step s1: clean the items", "-- step s1: clean", "--step s1.", "-- STEP s1",
                                     "-- step  s1 s2", "-- step s01"])
def test_steps_marker_names_a_comment_that_looks_like_a_marker_but_is_not_one(tmp_path, comment):
    error = only(check(tmp_path, GOOD_SQL.replace("-- step s1", comment)), "steps:marker")
    assert error.startswith("steps:marker: line 5") and "-- step s1" in error


def test_a_prose_comment_that_starts_with_step_is_not_a_marker(tmp_path):
    assert named(check(tmp_path, GOOD_SQL.replace("-- step s1", "-- step by step, keep the items\n-- step s1")), "steps:marker") == []


def test_a_marker_after_code_on_the_same_line_is_not_a_marker(tmp_path):
    build = GOOD_SQL.replace("-- step s2\nCREATE OR REPLACE TABLE", "CREATE OR REPLACE TABLE").replace(
        "SELECT ID, NOTE FROM kept;", "SELECT ID, NOTE FROM kept; -- step s2")
    report = check(tmp_path, build)
    assert "s2" in only(report, "steps:missing") and named(report, "steps:marker")


def test_every_objective_step_needs_a_region(tmp_path):
    report = check(tmp_path, GOOD_SQL, objective(extra_steps=True))
    assert "s3" in only(report, "steps:missing")


# --- run / outputs:columns ------------------------------------------------------------------------

def test_run_names_the_statement_that_fails_on_the_empty_sandbox(tmp_path):
    error = only(check(tmp_path, GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT ID, MISSING FROM kept;")), "run")
    assert error.startswith("run: line 10") and "MISSING" in error and "\n" not in error


def test_run_sees_the_input_columns_of_the_objective(tmp_path):
    build = GOOD_SQL.replace("WHERE NOTE IS NOT NULL", "WHERE UPPER(NOTE) <> 'X' AND ID IS NOT NULL")
    assert check(tmp_path, build)["ok"] is True


@pytest.mark.parametrize("select, mention", [
    ("SELECT ID FROM kept", "[ID]"),
    ("SELECT NOTE, ID FROM kept", "[NOTE, ID]"),
    ("SELECT ID, NOTE, ID AS EXTRA FROM kept", "EXTRA"),
    ("SELECT ID, NOTE AS COMMENT FROM kept", "COMMENT"),
])
def test_outputs_columns_names_the_output_and_both_column_lists(tmp_path, select, mention):
    error = only(check(tmp_path, GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", f"{select};")), "outputs:columns")
    assert "ITEMS_OUT" in error and mention in error and "[ID, NOTE]" in error and "\n" not in error


def test_outputs_columns_is_compared_without_regard_to_case(tmp_path):
    assert check(tmp_path, GOOD_SQL.replace("SELECT ID, NOTE FROM kept;", "SELECT id, note FROM kept;"))["ok"]



def test_an_append_target_is_created_empty_so_an_insert_runs(tmp_path):
    build = GOOD_SQL.replace("CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS\nSELECT ID, NOTE FROM kept;",
                             "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)\nSELECT ID, NOTE FROM kept;")
    report = check(tmp_path, build, objective(write_mode="append"))
    assert report["ok"] is True, report["errors"]


def test_the_session_statement_is_recorded_not_run(tmp_path):
    build = GOOD_SQL.replace("TIMEZONE = 'America/New_York'", "TIMEZONE = 'Not/AZone'")
    assert check(tmp_path, build)["ok"] is True


def test_nothing_runs_when_a_static_check_says_it_cannot(tmp_path):
    report = check(tmp_path, GOOD_SQL + "\nCOPY INTO @stage/out FROM kept;\n")
    assert named(report, "run") == [] and named(report, "outputs:columns") == []


# --- the lint is reported and never changes ok ---------------------------------------------------------

def test_a_lint_flag_is_reported_and_does_not_make_the_build_fail(tmp_path):
    build = GOOD_SQL.replace("SELECT ID, NOTE FROM {{src}}.ITEMS WHERE NOTE IS NOT NULL;",
                             "SELECT ID, NOTE FROM {{src}}.ITEMS;").replace(
        "SELECT ID, NOTE FROM kept;", "SELECT ID, NOTE FROM kept WHERE NOTE IS NOT NULL;")
    report = check(tmp_path, build)
    assert report["ok"] is True
    assert [(flag["kind"], flag["name"], flag["line"]) for flag in report["lint"]["flags"]] == [("pass_through_cte", "kept", 6)]


# --- the objective decides what is checked --------------------------------------------------------------

def test_a_python_objective_with_a_build_sql_is_a_language_error(tmp_path):
    report = check(tmp_path, GOOD_SQL, objective(language="python"))
    assert "language" in only(report, "language") and report["ok"] is False


# --- the CLI ---------------------------------------------------------------------------------------

def run_cli(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(Path(cc.__file__)), WF, "--root", str(root), *extra],
                          capture_output=True, text=True)


def test_cli_exits_zero_when_the_build_compiles(tmp_path):
    make(tmp_path)
    done = run_cli(tmp_path)
    assert done.returncode == 0 and done.stdout.startswith("OK"), done.stdout + done.stderr


def test_cli_exits_one_on_a_compile_error_and_prints_each_error(tmp_path):
    make(tmp_path, GOOD_SQL.replace("{{src}}.ITEMS", "{{src}}.UNMAPPED"))
    done = run_cli(tmp_path)
    assert done.returncode == 1 and "tables:source" in done.stdout + done.stderr
    assert read_json(Repo(tmp_path).wf(WF, "compile_check.json"))["ok"] is False


def test_cli_exits_zero_with_lint_flags_and_says_so(tmp_path):
    make(tmp_path, GOOD_SQL.replace("WHERE NOTE IS NOT NULL;", ";").replace(
        "SELECT ID, NOTE FROM kept;", "SELECT ID, NOTE FROM kept WHERE NOTE IS NOT NULL;"))
    done = run_cli(tmp_path)
    assert done.returncode == 0 and "1 lint flag" in done.stdout


@pytest.mark.parametrize("setup, mention", [
    (lambda root: None, "objective.json"),                                           # nothing there
    (lambda root: make(root, name=None), "build"),                                   # no build file
    (lambda root: (make(root), (Repo(root).wf(WF) / "build.py").write_text("x = 1\n")), "both"),
    (lambda root: Repo(root).wf(WF).mkdir(parents=True) or (Repo(root).wf(WF) / "objective.json").write_text("{"), "JSON"),
])
def test_cli_exits_two_when_there_is_nothing_to_check(tmp_path, setup, mention):
    setup(tmp_path)
    done = run_cli(tmp_path)
    assert done.returncode == 2 and mention in done.stderr
    assert not Repo(tmp_path).wf(WF, "compile_check.json").exists()


def test_cli_exits_two_for_a_workflow_id_that_is_not_one(tmp_path):
    done = subprocess.run([sys.executable, str(Path(cc.__file__)), "../x", "--root", str(tmp_path)],
                          capture_output=True, text=True)
    assert done.returncode == 2


def test_cli_unexpected_exception_exits_2_with_a_traceback(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(cc, "compile_check", boom)
    assert cc.main([WF, "--root", str(tmp_path)]) == 2
    assert "RuntimeError" in capsys.readouterr().err


@pytest.mark.parametrize("edit, mention", [
    (lambda obj: obj["outputs"][0].update(logical=None), "logical"),
    (lambda obj: obj["inputs"][0].update(logical=None), "logical"),
    (lambda obj: obj["outputs"][0].update(write_mode=None), "write_mode"),
    (lambda obj: obj["outputs"][0].update(write_mode="update_only"), "update_only"),
    (lambda obj: obj["outputs"][0].update(write_mode="update_insert", keys=[]), "keys"),
    (lambda obj: obj.update(tier="T3"), "T3"),
])
def test_an_objective_that_cannot_be_checked_against_is_exit_2_not_a_compile_error(tmp_path, capsys, edit, mention):
    obj = objective()
    edit(obj)
    repo = make(tmp_path, obj=obj)
    assert cc.main([WF, "--root", str(tmp_path)]) == 2
    assert mention in capsys.readouterr().err
    assert not repo.wf(WF, "compile_check.json").exists()


def test_the_report_is_valid_json_with_lf_endings(tmp_path):
    make(tmp_path)
    run_cli(tmp_path)
    raw = Repo(tmp_path).wf(WF, "compile_check.json").read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw and json.loads(raw)["ok"] is True


def test_a_scripting_block_is_refused_by_name(tmp_path):
    build = GOOD_SQL + "\nBEGIN\n  SELECT 1;\nEND;\n"
    assert named(check(tmp_path, build), "statement:kind"), "a BEGIN ... END block is not a statement kind"


def test_a_usage_error_removes_the_previous_report(tmp_path, capsys):
    repo = make(tmp_path, name=None)
    stale = repo.wf(WF, "compile_check.json")
    stale.write_text('{"ok": true, "errors": []}\n', encoding="utf-8")
    assert cc.main([WF, "--root", str(tmp_path)]) == 2
    assert not stale.exists()


def test_an_objective_with_no_language_is_a_usage_error(tmp_path, capsys):
    obj = objective()
    obj["language"] = None
    make(tmp_path, obj=obj)
    assert cc.main([WF, "--root", str(tmp_path)]) == 2
    assert "language" in capsys.readouterr().err


def test_a_byte_order_mark_at_the_start_of_the_file_is_not_part_of_the_sql(tmp_path):
    repo = make(tmp_path)
    (repo.wf(WF) / "build.sql").write_bytes(b"\xef\xbb\xbf" + GOOD_SQL.encode("utf-8"))
    assert cc.compile_check(repo, WF)["ok"] is True


def test_a_pre_or_postsql_may_read_the_table_it_changes(tmp_path):
    pre = ("DELETE FROM {{tgt}}.ITEMS_OUT WHERE ID IN (SELECT ID FROM {{tgt}}.ITEMS_OUT WHERE NOTE IS NULL);\n")
    build = GOOD_SQL.replace("-- step s2\n", "-- step s2\n" + pre)
    report = check(tmp_path, build, objective(write_mode="overwrite", pre_sql="DELETE FROM dbo.ITEMS_OUT"))
    assert named(report, "tables:target") == [], report["errors"]


def test_an_overwrite_target_with_a_presql_exists_empty_before_the_presql_runs(tmp_path):
    pre = "DELETE FROM {{tgt}}.ITEMS_OUT WHERE ID < 0;\n"
    build = GOOD_SQL.replace("-- step s2\n", "-- step s2\n" + pre)
    report = check(tmp_path, build, objective(write_mode="overwrite", pre_sql="DELETE FROM dbo.ITEMS_OUT WHERE ID < 0"))
    assert report["ok"] is True, report["errors"]


# --- A3 review fix round 1 -------------------------------------------------------------------------------------

IDENTIFIER_HINT = "write {{src}}.<LOGICAL> / {{tgt}}.<LOGICAL>, not IDENTIFIER(...)"


@pytest.mark.parametrize("source", ["IDENTIFIER('MYDB.RAW.ITEMS')", "IDENTIFIER(:ITEMS_SRC)", "IDENTIFIER('{{src}}.ITEMS')"])
def test_a_source_read_through_identifier_is_refused_as_tables_source(tmp_path, source):
    report = check(tmp_path, GOOD_SQL.replace("{{src}}.ITEMS", source))
    error = only(report, "tables:source")
    assert error.startswith("tables:source: line 7:") and "IDENTIFIER(" in error and IDENTIFIER_HINT in error
    assert named(report, "run") == [] and named(report, "tables:target") == []


@pytest.mark.parametrize("write", [
    "CREATE OR REPLACE TABLE IDENTIFIER(:ITEMS_OUT_TGT) AS\nSELECT ID, NOTE FROM kept;",
    "CREATE OR REPLACE TABLE IDENTIFIER('MYDB.WORK.ITEMS_OUT') AS\nSELECT ID, NOTE FROM kept;",
])
def test_a_target_written_through_identifier_is_refused_as_tables_target(tmp_path, write):
    build = GOOD_SQL.replace("CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS\nSELECT ID, NOTE FROM kept;", write)
    report = check(tmp_path, build)
    error = only(report, "tables:target")
    assert error.startswith("tables:target: line 10:") and IDENTIFIER_HINT in error
    assert named(report, "run") == []


@pytest.mark.parametrize("statement", [
    "INSERT INTO IDENTIFIER(:OUT_TGT) (ID, NOTE) SELECT ID, NOTE FROM kept",
    "TRUNCATE TABLE IDENTIFIER(:OUT_TGT)",
    "DELETE FROM IDENTIFIER(:OUT_TGT) WHERE ID < 0",
    "UPDATE IDENTIFIER(:OUT_TGT) SET NOTE = 'x'",
    "MERGE INTO IDENTIFIER(:OUT_TGT) AS T USING kept AS S ON T.ID = S.ID WHEN MATCHED THEN UPDATE SET NOTE = S.NOTE",
])
def test_every_write_kind_through_identifier_is_refused_as_tables_target_not_statement_kind(tmp_path, statement):
    report = check(tmp_path, GOOD_SQL + f"\n{statement};\n")
    error = only(report, "tables:target")
    assert error.startswith("tables:target: line 13:") and IDENTIFIER_HINT in error
    assert named(report, "statement:kind") == []


def test_a_stage_or_path_named_through_identifier_is_external_access(tmp_path):
    build = GOOD_SQL.replace("{{src}}.ITEMS", "IDENTIFIER('@mystage/items.csv')")
    report = check(tmp_path, build)
    assert "stage or a file" in only(report, "external_access") and named(report, "run") == []


def test_the_statement_kind_message_shows_placeholders_with_their_braces(tmp_path):
    error = only(check(tmp_path, GOOD_SQL + "\nDELETE FROM {{src}}.ITEMS WHERE ID < 0;\n"), "statement:kind")
    assert "DELETE FROM {{src}}.ITEMS WHERE ID" in error and "SRC__PH" not in error and "src ITEMS" not in error
    error = only(check(tmp_path / "b", GOOD_SQL + "\nCALL {{tgt}}.ITEMS_OUT('x');\n"), "statement:kind")
    assert "CALL {{tgt}}.ITEMS_OUT('x')" in error or "CALL {{tgt}}.ITEMS_OUT(" in error


# --- dbt:form ----------------------------------------------------------------------------------------

#: The shapes of `tests/test_export_dbt.py`'s UNSUPPORTED table that no other check refuses: `compile_check.py`
#: accepted them, and `export_dbt.py` then parked the workflow with no fixer round. Only `dbt:form` sees them.
ONLY_DBT_FORM = ("a merge update with a condition", "a merge update that is not a copy of the source",
                 "an insert of values", "an insert into a temporary table")


def _unsupported(label: str):
    row = next(row for row in exd.UNSUPPORTED if row[0] == label)
    _, statement, changes, expect = row
    text = exd.build(exd.KEPT, statement) if "ITEMS_OUT" in statement else exd.build(exd.KEPT, statement, exd.OVERWRITE)
    return text, (exd.objective_with(**changes) if changes else None), statement, expect


@pytest.mark.parametrize("label", ONLY_DBT_FORM)
def test_a_shape_the_exporter_cannot_convert_fails_as_dbt_form_naming_its_line_while_dbt_is_on(tmp_path, label):
    text, obj, statement, expect = _unsupported(label)
    report = check(tmp_path, text, obj)
    error = only(report, "dbt:form")
    line = text.splitlines().index(statement.splitlines()[0]) + 1
    assert error.startswith(f"dbt:form: line {line}: no dbt form for ") and expect in error, error
    assert "export_dbt.py" in error and report["errors"] == [error] and report["ok"] is False


@pytest.mark.parametrize("label", ONLY_DBT_FORM)
def test_the_same_shape_compiles_when_program_dbt_is_false(tmp_path, label):
    text, obj, _, _ = _unsupported(label)
    dbt_off(tmp_path)
    report = check(tmp_path, text, obj)
    assert report["ok"] is True and report["errors"] == [], report["errors"]


@pytest.mark.parametrize("row", exd.UNSUPPORTED, ids=[row[0] for row in exd.UNSUPPORTED])
def test_every_shape_the_exporter_cannot_convert_fails_the_compile_check_while_dbt_is_on(tmp_path, row):
    text, obj, _, _ = _unsupported(row[0])
    assert check(tmp_path, text, obj)["ok"] is False


def test_a_with_before_an_insert_has_no_dbt_form_either(tmp_path):
    insert = ("WITH shaped AS (SELECT ID, NOTE FROM kept)\n"
              "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE) SELECT ID, NOTE FROM shaped;")
    text = exd.build(exd.KEPT, insert)
    error = only(check(tmp_path, text, objective(write_mode="append")), "dbt:form")
    assert "this INSERT; expected INSERT INTO {{tgt}}.<LOGICAL> (<columns>) <query>" in error
    dbt_off(tmp_path / "off")
    assert check(tmp_path / "off", text, objective(write_mode="append"))["ok"] is True


def test_dbt_form_is_checked_only_once_the_static_checks_pass_and_is_never_added_to_their_error(tmp_path):
    report = check(tmp_path, GOOD_SQL.replace("{{src}}.ITEMS", "{{src}}.NOT_AN_INPUT"))
    assert named(report, "tables:source") and named(report, "dbt:form") == []
    report = check(tmp_path / "w", GOOD_SQL.replace("CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS", "INSERT INTO {{tgt}}.ITEMS_OUT (ID, NOTE)"))
    assert named(report, "write_mode") and named(report, "dbt:form") == []


def test_dbt_form_writes_no_dbt_project_and_a_good_build_still_compiles(tmp_path):
    repo = make(tmp_path)
    assert cc.compile_check(repo, WF)["ok"] is True
    assert not repo.wf(WF, "dbt").exists(), "the conversion is run in memory"
    assert sorted(path.name for path in repo.wf(WF).iterdir()) == ["build.sql", "compile_check.json", "objective.json"]


def test_a_python_workflow_has_no_dbt_form_to_check(tmp_path):
    report = check(tmp_path, "def main(session, src, tgt):\n    return 'OK'\n", objective(language="python"), name="build.py")
    assert named(report, "dbt:form") == []


def test_a_program_dbt_that_is_not_true_or_false_is_a_usage_error_exit_2(tmp_path, capsys):
    repo = make(tmp_path)
    write_yaml(repo.global_mappings, {"program": {"dbt": "maybe"}})
    assert cc.main([WF, "--root", str(tmp_path)]) == 2
    assert "program.dbt" in capsys.readouterr().err
    assert not repo.wf(WF, "compile_check.json").exists()


def test_the_module_docstring_documents_the_python_null_row_retry_and_its_backstop():
    doc = cc.__doc__
    assert "ONCE" in doc and "one row of NULLs" in doc and "`validate_build.py`'s golden `empty` set" in doc
    assert "`language`" in doc and "IDENTIFIER" in doc and "IMMEDIATELY" in doc
    assert "`dbt:form`" in doc and "export_dbt.py" in doc


def test_compile_check_imports_nothing_it_does_not_use():
    import ast
    source = Path(cc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {alias.asname or alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names if alias.name != "annotations"}
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert sorted(imported - used) == []
