"""Live hardening L4, fix round 1 (P, pre-existing, ruled in): agent SQL never touches host files -- for a
`build.sql`.

A build file is agent-written SQL that `compile_check.py` (and `validate_build.py`) run on the DuckDB double.
DuckDB can read and write the host's files (`COPY ... TO '<path>'`, `read_text('<path>')`, `FROM '<file>.csv'`,
`ATTACH`, ...), so two guards stand between that SQL and the host:

- `compile_check.py` refuses file-, network- and settings-touching constructs by name (`external_access`), before
  anything runs;
- the run turns DuckDB's external access off, and locks the configuration, before the file's first statement
  runs (the empty input tables are created before that, and every later load goes through Python, never a file
  function) -- so even SQL that got past the first guard cannot reach a file.

The probes are the ones the retired stored-procedure form was tested with, kept because each is a real way
DuckDB reaches a file. Nothing here has run on Snowflake; the file functions named are DuckDB's (the double's),
plus Snowflake's stage and file functions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import compile_check as cc
from lib import build_sql
from lib.backend import BackendError, DuckDBBackend
from tests.build_fixtures import GOOD_SQL, check, make, named, objective


def _secret(tmp_path: Path) -> Path:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VALUE", encoding="utf-8")
    return secret


def _probes(tmp_path: Path) -> dict[str, str]:
    """The reviewer's two probes, and the other ways DuckDB reaches a file."""
    marker = (tmp_path / "marker.csv").as_posix()
    secret = _secret(tmp_path).as_posix()
    return {
        "copy_to": f"COPY (SELECT 42 AS X) TO '{marker}'",
        "read_text": f"CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT content AS NOTE FROM read_text('{secret}')",
        "read_csv": f"CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT * FROM read_csv_auto('{secret}')",
        "quoted_function": f"CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT \"read_text\"('{secret}') AS NOTE",
        "file_in_from": f"CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT * FROM '{secret}'",
        "file_in_join": (f"CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT t.ID FROM kept t JOIN '{secret}' s ON TRUE"),
        "quoted_file": 'CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT * FROM "secret.txt"',
        "install": "INSTALL httpfs",
        "stage": "CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT $1 AS NOTE FROM @MIG_WORK.S",
        "getenv": "CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT getenv('PATH') AS NOTE",
        "set": "SET enable_external_access = true",
        "presigned": "CREATE OR REPLACE TEMPORARY TABLE leak AS SELECT GET_PRESIGNED_URL(@S, 'f') AS NOTE",
    }


#: `_probes`' names, spelled out: collecting the tests must not call `_probes`, which writes a file.
PROBES = ("copy_to", "read_text", "read_csv", "quoted_function", "file_in_from", "file_in_join", "quoted_file",
          "install", "stage", "getenv", "set", "presigned")


def _with(statement: str) -> str:
    """The fixture's correct build with `statement` as its last statement."""
    return GOOD_SQL + f"\n{statement};\n"


def test_the_probe_list_names_every_probe(tmp_path):
    assert sorted(PROBES) == sorted(_probes(tmp_path))


@pytest.mark.parametrize("probe", PROBES)
def test_compile_check_refuses_every_file_touching_construct_before_it_runs(tmp_path, probe):
    report = check(tmp_path / "root", _with(_probes(tmp_path)[probe]))
    assert report["ok"] is False
    assert named(report, "external_access"), report["errors"]
    assert named(report, "run") == [] and not (tmp_path / "marker.csv").exists()


def test_the_refusal_names_the_construct_and_the_rule(tmp_path):
    report = check(tmp_path / "root", _with(_probes(tmp_path)["read_text"]))
    assert named(report, "external_access") == [
        "external_access: line 13: read_text() reads files, settings or the network; a build file reads its sources "
        "through {{src}}.<LOGICAL> and writes tables, nothing else"]


def test_ordinary_sql_that_merely_looks_similar_is_not_refused(tmp_path):
    """`EXTRACT(... FROM '<literal>')`, a column named like a function, a string that mentions one."""
    statement = ("CREATE OR REPLACE TEMPORARY TABLE note AS SELECT "
                 "EXTRACT(YEAR FROM '2020-01-01'::DATE) AS Y, 'read_text(x) COPY TO' AS load_date")
    assert named(check(tmp_path / "root", _with(statement)), "external_access") == []


# --- with the static check bypassed, the run itself cannot reach a file -----------------------------------------

@pytest.mark.parametrize("probe", ["copy_to", "read_text", "file_in_from", "read_csv"])
def test_with_the_check_bypassed_the_run_fails_because_external_access_is_off(tmp_path, probe):
    obj = objective()
    text = _with(_probes(tmp_path)[probe])
    sources = cc._sources(obj)
    errors = cc._run_errors(build_sql.split_statements(text), sources, [], obj)
    assert errors, "the run must fail"
    # DuckDB refuses the file ("Permission Error ... disabled by configuration"), or -- for `FROM '<path>'`,
    # whose replacement scan is off with external access -- no longer reads it as a file at all.
    assert any(any(sign in error for sign in ("disabled by configuration", "Permission Error", "does not exist"))
               for error in errors), errors
    assert not (tmp_path / "marker.csv").exists()
    assert "TOP-SECRET-VALUE" not in " ".join(errors)


def test_the_run_locks_external_access_before_the_first_statement(tmp_path):
    backend = DuckDBBackend()
    try:
        backend.lock_external_access()
        with pytest.raises(BackendError, match="disabled by configuration|Permission Error"):
            backend.execute(_probes(tmp_path)["copy_to"])
        assert not (tmp_path / "marker.csv").exists()
        # nothing on the connection can turn it back on (the backend's own translation of a Snowflake `SET` is a
        # DuckDB user variable, so the raw setting is tried as well) ...
        backend.execute("SET enable_external_access = true")
        with pytest.raises(Exception, match="Cannot change configuration option"):
            backend._con.execute("SET enable_external_access = true")
        assert backend._con.execute("SELECT current_setting('enable_external_access')").fetchall() == [(False,)]
    finally:
        backend.close()


def test_the_fixture_build_needs_no_external_access(tmp_path):
    assert check(tmp_path)["ok"] is True
