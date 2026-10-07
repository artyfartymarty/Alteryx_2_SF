"""`scripts/deploy.py`: print (default) or execute a VALIDATED workflow's build file on Snowflake (objective-first
design §4.4).

A dry run connects to nothing. `--execute` goes through `SnowflakeBackend(connection_name=…)` -- here a recording
stand-in patched in as `snowflake.connector` that keeps every statement it is given (the DuckDB-backed
`tests/fake_snowflake.py` cannot take a `PUT`, a stage or `EXECUTE IMMEDIATE FROM`, and would prove nothing
about them). The build files are small fixtures this module writes. Nothing here reaches a real account, and
nothing here shows that Snowflake accepts the statements: see `docs/reference/snowflake-backend.md` §7.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import deploy
from lib import snowflake_conn
from lib.io import read_json, write_json, write_text, write_yaml
from lib.paths import Repo

TARGET = ["--database", "ANALYTICS", "--schema", "CURATED"]
SRC_1 = ["--src-schema", "MIG_SRC_WF0001"]
SRC_6 = ["--src-schema", "MIG_SRC_WF0006"]

BUILD_SQL = """\
-- wf_0001 · Sales summary by region and size band
-- Written from objective.md. {{src}} = source schema, {{tgt}} = target schema.
ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;

-- step s1
CREATE OR REPLACE TEMPORARY TABLE orders AS
SELECT ORDER_ID, REGION FROM {{src}}.ORDERS;

-- step s2
CREATE OR REPLACE TABLE {{ tgt }}.SALES_SUMMARY AS
SELECT REGION, COUNT(*) AS ORDERS FROM orders GROUP BY REGION;
"""

BUILD_PY = """\
import pandas as pd


def main(session, src: str, tgt: str) -> str:
    # step s1
    ledger = session.table(f"{src}.LEDGER").to_pandas()
    session.create_dataframe(ledger).write.save_as_table(f"{tgt}.PERIOD_SUMMARY", mode="overwrite")
    return "OK"
"""

GLOBAL = {"program": {"target_database": "ANALYTICS", "warehouse": "MIG_WH", "snowpark_runtime": "3.11"},
          "session": {"TIMEZONE": "America/New_York", "WEEK_START": 1}}


class FakeError(Exception):
    """What a connector raises: a message, an `errno` and a `sqlstate`."""

    def __init__(self, message: str, errno: int | None = None, sqlstate: str | None = None):
        super().__init__(message)
        self.errno = errno
        self.sqlstate = sqlstate


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        module = self.connection.module
        module.statements.append(sql)
        for marker, message in module.fail.items():
            if marker in sql:
                raise FakeError(message, errno=100132, sqlstate="P0000")
        return self


class _Connection:
    def __init__(self, module):
        self.module = module
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


class RecordingConnector:
    """Patched in as `snowflake.connector`: `connect(**kwargs)` records its arguments and every statement a
    cursor is given is kept, in order, in `statements`; a statement holding a key of `fail` raises its message."""

    paramstyle = "pyformat"
    errors = SimpleNamespace(Error=FakeError, ProgrammingError=FakeError, DatabaseError=FakeError)

    def __init__(self):
        self.connect_calls: list[dict] = []
        self.connections: list[_Connection] = []
        self.statements: list[str] = []
        self.fail: dict[str, str] = {}

    def connect(self, **kwargs):
        self.connect_calls.append(dict(kwargs))
        connection = _Connection(self)
        self.connections.append(connection)
        return connection


@pytest.fixture
def fake(monkeypatch):
    module = RecordingConnector()
    monkeypatch.setitem(sys.modules, "snowflake.connector", module)
    monkeypatch.delenv(snowflake_conn.CONNECTION_ENV, raising=False)
    return module


def make_repo(tmp_path: Path, wf_id: str = "wf_0001", *, build: str | None = BUILD_SQL, name: str = "build.sql",
              translate: str | None = "VALIDATED", schedule: str | None = "0 6 * * 1-5",
              mappings: dict | None = GLOBAL) -> Repo:
    """A repo holding `wf_id` as the document stage leaves it: a manifest, one build file, the program answers."""
    repo = Repo(tmp_path)
    manifest = {"id": wf_id, "status": {"translate": translate} if translate else {},
                "source": {"file": "x.yxmd", "server_schedule": schedule}}
    write_json(repo.wf(wf_id, "manifest.json"), manifest)
    if build is not None:
        write_text(repo.wf(wf_id, name), build)
    if mappings is not None:
        write_yaml(repo.global_mappings, mappings)
    return repo


def _code(argv) -> int:
    """`deploy.main`'s exit code, whether it returned one or argparse raised SystemExit."""
    try:
        return deploy.main(argv)
    except SystemExit as exc:
        return exc.code


def argv(repo: Repo, wf_id: str, *rest: str) -> list[str]:
    return [wf_id, *TARGET, *rest, "--root", str(repo.root)]


def snapshot(repo: Repo) -> dict[str, bytes]:
    return {path.relative_to(repo.root).as_posix(): path.read_bytes()
            for path in sorted(repo.root.rglob("*")) if path.is_file()}


def put_uri(repo: Repo, wf_id: str, name: str = "build.sql") -> str:
    return "file://" + repo.wf(wf_id, name).resolve().as_posix()


TASK_1 = """\
-- Run it on the Alteryx schedule (manifest.json source.server_schedule '0 6 * * 1-5'), here read in the
-- session TIMEZONE of mappings/global.yaml. Commented out: no Task has ever been created from this.
-- CREATE OR REPLACE TASK ANALYTICS.CURATED.WF0001_BUILD_TASK
--   WAREHOUSE = MIG_WH
--   SCHEDULE = 'USING CRON 0 6 * * 1-5 America/New_York'
-- AS
--   EXECUTE IMMEDIATE FROM @ANALYTICS.CURATED.MIG_BUILD_WF0001/build.sql USING (src => 'ANALYTICS.MIG_SRC_WF0001', tgt => 'ANALYTICS.CURATED');
-- ALTER TASK ANALYTICS.CURATED.WF0001_BUILD_TASK RESUME;
"""


def sql_dry_run(repo: Repo) -> str:
    return f"""\
-- deploy.py wf_0001: dry run -- printed only, nothing was executed
-- Nothing in this repository has run on a real Snowflake account, and Snowflake's own templating of the
-- two names below (EXECUTE IMMEDIATE FROM ... USING) has never run on one: --render writes
-- build.rendered.sql with the names substituted, for an account where it does not.
-- workflows/wf_0001/build.sql reads {{{{src}}}}.<LOGICAL> and writes {{{{tgt}}}}.<LOGICAL>, with {{{{src}}}} = ANALYTICS.MIG_SRC_WF0001 and {{{{tgt}}}} = ANALYTICS.CURATED.
CREATE STAGE IF NOT EXISTS ANALYTICS.CURATED.MIG_BUILD_WF0001;
-- Upload the file as @ANALYTICS.CURATED.MIG_BUILD_WF0001/build.sql, uncompressed, replacing any earlier copy.
PUT '{put_uri(repo, "wf_0001")}' @ANALYTICS.CURATED.MIG_BUILD_WF0001 AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
-- Run it: Snowflake fills {{{{src}}}} and {{{{tgt}}}}, then runs the file's statements in order.
EXECUTE IMMEDIATE FROM @ANALYTICS.CURATED.MIG_BUILD_WF0001/build.sql USING (src => 'ANALYTICS.MIG_SRC_WF0001', tgt => 'ANALYTICS.CURATED');

{TASK_1}"""


PROCEDURE_6 = """\
CREATE OR REPLACE PROCEDURE CURATED.WF0006_BUILD(SRC STRING, TGT STRING)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'pandas')
HANDLER = 'main'
EXECUTE AS CALLER
AS
$$
""" + BUILD_PY + """$$"""

PYTHON_DRY_RUN = f"""\
-- deploy.py wf_0006: dry run -- printed only, nothing was executed
-- Nothing in this repository has run on a real Snowflake account; no procedure has been created from this.
-- workflows/wf_0006/build.py deploys as one Python stored procedure: main(session, src, tgt) reads
-- src.<LOGICAL> and writes tgt.<LOGICAL>, with src = ANALYTICS.MIG_SRC_WF0006 and tgt = ANALYTICS.CURATED.
-- Verify RUNTIME_VERSION and PACKAGES against the account (and its Anaconda terms) before running the DDL.
USE DATABASE ANALYTICS;
-- The procedure.
{PROCEDURE_6};
-- Run it.
CALL CURATED.WF0006_BUILD('ANALYTICS.MIG_SRC_WF0006', 'ANALYTICS.CURATED');

-- Run it on the Alteryx schedule (manifest.json source.server_schedule '0 6 * * 1-5'), here read in the
-- session TIMEZONE of mappings/global.yaml. Commented out: no Task has ever been created from this.
-- CREATE OR REPLACE TASK ANALYTICS.CURATED.WF0006_BUILD_TASK
--   WAREHOUSE = MIG_WH
--   SCHEDULE = 'USING CRON 0 6 * * 1-5 America/New_York'
-- AS
--   CALL ANALYTICS.CURATED.WF0006_BUILD('ANALYTICS.MIG_SRC_WF0006', 'ANALYTICS.CURATED');
-- ALTER TASK ANALYTICS.CURATED.WF0006_BUILD_TASK RESUME;
"""


# --- the dry run -----------------------------------------------------------------------------------------

def test_a_dry_run_prints_the_sql_deployment_and_connects_to_nothing(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)

    assert deploy.main(argv(repo, "wf_0001", *SRC_1)) == 0

    assert capsys.readouterr().out == sql_dry_run(repo)
    assert fake.connect_calls == [] and fake.statements == []


def test_a_dry_run_prints_the_python_procedure_and_its_call(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, "wf_0006", build=BUILD_PY, name="build.py")

    assert deploy.main(argv(repo, "wf_0006", *SRC_6)) == 0

    assert capsys.readouterr().out == PYTHON_DRY_RUN
    assert fake.connect_calls == [] and fake.statements == []


@pytest.mark.parametrize("wf_id, build, name, src", [("wf_0001", BUILD_SQL, "build.sql", SRC_1),
                                                     ("wf_0006", BUILD_PY, "build.py", SRC_6)])
def test_a_dry_run_writes_nothing(tmp_path, fake, wf_id, build, name, src):
    repo = make_repo(tmp_path, wf_id, build=build, name=name)
    before = snapshot(repo)

    assert deploy.main(argv(repo, wf_id, *src)) == 0

    assert snapshot(repo) == before


def test_without_a_src_schema_the_dry_run_prints_the_placeholder_and_says_where_it_comes_from(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)

    assert deploy.main(argv(repo, "wf_0001")) == 0

    out = capsys.readouterr().out
    assert "src => 'ANALYTICS.<SRC_SCHEMA>'" in out
    assert "-- <SRC_SCHEMA> is the schema of source views scripts/gen_source_views.py generates " \
           "(MIG_SRC_WF0001); pass --src-schema S to fill it in." in out
    assert fake.connect_calls == []


@pytest.mark.parametrize("flag", ["--execute", "--render"])
def test_execute_and_render_need_a_src_schema(tmp_path, fake, capsys, flag):
    repo = make_repo(tmp_path)

    assert _code(argv(repo, "wf_0001", "--connection", "prod", flag)) == 2

    err = capsys.readouterr().err
    assert "--src-schema is required" in err and flag in err and "MIG_SRC_WF0001" in err
    assert fake.connect_calls == [] and not repo.wf("wf_0001", "build.rendered.sql").exists()


@pytest.mark.parametrize("stage, qualified", [
    (None, "ANALYTICS.CURATED.MIG_BUILD_WF0001"), ("MY_STAGE", "ANALYTICS.CURATED.MY_STAGE"),
    ("OPS.MY_STAGE", "ANALYTICS.OPS.MY_STAGE"), ("OTHER.OPS.MY_STAGE", "OTHER.OPS.MY_STAGE")])
def test_stage_is_filled_in_to_three_parts(tmp_path, fake, capsys, stage, qualified):
    repo = make_repo(tmp_path)
    extra = ["--stage", stage] if stage else []

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, *extra)) == 0

    out = capsys.readouterr().out
    assert f"CREATE STAGE IF NOT EXISTS {qualified};\n" in out
    assert f"@{qualified} AUTO_COMPRESS = FALSE OVERWRITE = TRUE;\n" in out
    assert f"EXECUTE IMMEDIATE FROM @{qualified}/build.sql USING (" in out


@pytest.mark.parametrize("stage", ["my_stage", "A.B.C.D", "STAGE; DROP DATABASE X", "A..B", "@STAGE", ""])
def test_a_stage_that_is_not_plain_identifiers_is_a_usage_error(tmp_path, fake, capsys, stage):
    repo = make_repo(tmp_path)

    assert _code(argv(repo, "wf_0001", *SRC_1, "--stage", stage)) == 2

    assert "--stage" in capsys.readouterr().err
    assert fake.connect_calls == []


# --- the Task --------------------------------------------------------------------------------------------

def test_no_schedule_means_no_task_but_says_so(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, schedule=None)

    assert deploy.main(argv(repo, "wf_0001", *SRC_1)) == 0

    out = capsys.readouterr().out
    assert out.endswith("\n-- manifest.json records no schedule (source.server_schedule): no Task is printed.\n")
    assert "CREATE OR REPLACE TASK" not in out


def test_a_schedule_that_is_not_a_cron_expression_leaves_the_schedule_to_fill_in(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, schedule="every weekday at 6\nam'; DROP TASK X")

    assert deploy.main(argv(repo, "wf_0001", *SRC_1)) == 0

    out = capsys.readouterr().out
    assert "--   SCHEDULE = 'USING CRON <CRON> America/New_York'" in out
    assert "source.server_schedule \"every weekday at 6 am'; DROP TASK X\")" in out
    assert "\nDROP TASK" not in out


def test_the_task_takes_its_warehouse_and_time_zone_from_the_program_answers_or_leaves_them_to_fill_in(
        tmp_path, fake, capsys):
    repo = make_repo(tmp_path, mappings=None)

    assert deploy.main(argv(repo, "wf_0001", *SRC_1)) == 0

    out = capsys.readouterr().out
    assert "--   WAREHOUSE = <WAREHOUSE>\n--   SCHEDULE = 'USING CRON 0 6 * * 1-5 <TIMEZONE>'\n" in out


# --- --render -----------------------------------------------------------------------------------------------

def test_render_also_writes_the_build_with_the_names_substituted(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)
    original = repo.wf("wf_0001", "build.sql").read_bytes()

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--render")) == 0

    rendered = repo.wf("wf_0001", "build.rendered.sql").read_text(encoding="utf-8")
    assert rendered == (BUILD_SQL.replace("{{src}}", "ANALYTICS.MIG_SRC_WF0001").replace("{{tgt}}", "ANALYTICS.CURATED")
                        .replace("{{ tgt }}", "ANALYTICS.CURATED"))
    assert "{{" not in rendered and "ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;" in rendered
    assert b"\r" not in repo.wf("wf_0001", "build.rendered.sql").read_bytes()
    assert repo.wf("wf_0001", "build.sql").read_bytes() == original
    out = capsys.readouterr().out
    assert out.startswith(sql_dry_run(repo))                     # the dry run is printed in full, then the file
    assert out.endswith("\n-- wrote workflows/wf_0001/build.rendered.sql: build.sql with {{src}} = "
                        "ANALYTICS.MIG_SRC_WF0001 and {{tgt}} = ANALYTICS.CURATED substituted\n")
    assert fake.connect_calls == []


def test_render_on_a_python_build_is_a_usage_error(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, "wf_0006", build=BUILD_PY, name="build.py")

    assert _code(argv(repo, "wf_0006", *SRC_6, "--render")) == 2

    assert "--render applies to a build.sql" in capsys.readouterr().err
    assert not repo.wf("wf_0006", "build.rendered.sql").exists()


@pytest.mark.parametrize("placeholder", ["{{ schema }}", "{{src.x}}", "{{tgt"])
def test_a_placeholder_snowflake_could_not_fill_in_is_refused_before_anything_is_printed(tmp_path, fake, capsys,
                                                                                         placeholder):
    repo = make_repo(tmp_path, build=BUILD_SQL.replace("{{src}}.ORDERS", placeholder + ".ORDERS"))

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--render")) == 1

    captured = capsys.readouterr()
    assert captured.out == "" and "build.sql" in captured.err and "line 7" in captured.err
    assert not repo.wf("wf_0001", "build.rendered.sql").exists()


# --- the gate --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("translate", ["TRANSLATED", "PARKED", "MANUAL", None])
def test_a_workflow_that_is_not_validated_is_refused_and_nothing_connects(tmp_path, fake, capsys, translate):
    repo = make_repo(tmp_path, translate=translate)

    assert deploy.main(argv(repo, "wf_0001", *SRC_1)) == 1
    assert "VALIDATED" in capsys.readouterr().err
    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--connection", "prod", "--execute")) == 1
    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--render")) == 1
    assert fake.connect_calls == [] and not repo.wf("wf_0001", "build.rendered.sql").exists()


# --- --execute -----------------------------------------------------------------------------------------------

def test_execute_sends_exactly_the_printed_sql_statements_through_the_named_connection(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)
    run = ("EXECUTE IMMEDIATE FROM @ANALYTICS.CURATED.MIG_BUILD_WF0001/build.sql USING "
           "(src => 'ANALYTICS.MIG_SRC_WF0001', tgt => 'ANALYTICS.CURATED')")

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--connection", "prod", "--execute")) == 0

    assert fake.statements == [
        "CREATE STAGE IF NOT EXISTS ANALYTICS.CURATED.MIG_BUILD_WF0001",
        f"PUT '{put_uri(repo, 'wf_0001')}' @ANALYTICS.CURATED.MIG_BUILD_WF0001 AUTO_COMPRESS = FALSE OVERWRITE = TRUE",
        run]
    assert fake.connect_calls == [{"connection_name": "prod"}] and fake.connections[0].closed
    out = capsys.readouterr().out
    assert out.startswith("-- deploy.py wf_0001: executing through the named connection 'prod'\n")
    for statement in fake.statements:
        assert f"{statement};\n" in out                       # what ran is what was printed
    assert out.endswith("-- executed 3 statements through the named connection 'prod'\n")


def test_execute_sends_exactly_the_printed_python_statements(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, "wf_0006", build=BUILD_PY, name="build.py")

    assert deploy.main(argv(repo, "wf_0006", *SRC_6, "--connection", "prod", "--execute")) == 0

    assert fake.statements == ["USE DATABASE ANALYTICS", PROCEDURE_6,
                               "CALL CURATED.WF0006_BUILD('ANALYTICS.MIG_SRC_WF0006', 'ANALYTICS.CURATED')"]
    out = capsys.readouterr().out
    assert all(f"{statement};\n" in out for statement in fake.statements)
    assert out.endswith("-- executed 3 statements through the named connection 'prod'\n")


def test_execute_takes_the_connection_name_from_the_environment(tmp_path, fake, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setenv(snowflake_conn.CONNECTION_ENV, "from_env")

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--execute")) == 0

    assert fake.connect_calls == [{"connection_name": "from_env"}]


def test_execute_needs_a_connection(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)

    assert _code(argv(repo, "wf_0001", *SRC_1, "--execute")) == 2

    assert "MIG_SNOWFLAKE_CONNECTION" in capsys.readouterr().err
    assert fake.connect_calls == []


def test_execute_with_render_writes_the_file_and_still_runs_the_templated_form(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--connection", "prod", "--execute", "--render")) == 0

    assert repo.wf("wf_0001", "build.rendered.sql").is_file()
    assert fake.statements[-1].startswith("EXECUTE IMMEDIATE FROM @")


def test_a_statement_that_fails_is_a_redacted_domain_failure_and_nothing_after_it_runs(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)
    fake.fail["PUT"] = "SQL access control error: password=hunter2-secret"

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--connection", "prod", "--execute")) == 1

    err = capsys.readouterr().err
    assert "deploy stopped at PUT " in err and "hunter2-secret" not in err and "Traceback" not in err
    assert [s.split(" ")[0] for s in fake.statements] == ["CREATE", "PUT"]
    assert fake.connections[0].closed


def test_a_connection_that_cannot_be_opened_is_exit_2_with_the_error_redacted(tmp_path, fake, monkeypatch, capsys):
    repo = make_repo(tmp_path)

    def refuse(name):
        raise RuntimeError(f"250001: Could not connect: private_key_file_pwd=hunter2-secret for {name}")
    monkeypatch.setattr(snowflake_conn, "connect", refuse)

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--connection", "prod", "--execute")) == 2

    err = capsys.readouterr().err
    assert "Could not connect" in err and "'prod'" in err and "hunter2-secret" not in err
    assert "Traceback" not in err and fake.statements == []


# --- the Python procedure ------------------------------------------------------------------------------------

def test_the_procedure_takes_its_runtime_from_the_program_answers_and_always_lists_pandas(tmp_path, fake, capsys):
    """`to_pandas()` needs pandas at run time even in a module that never imports it."""
    plain = BUILD_PY.replace("import pandas as pd\n\n\n", "")
    repo = make_repo(tmp_path, "wf_0006", build=plain, name="build.py",
                     mappings={"program": {"snowpark_runtime": "3.12"}})

    assert deploy.main(argv(repo, "wf_0006", *SRC_6)) == 0

    out = capsys.readouterr().out
    assert "RUNTIME_VERSION = '3.12'\nPACKAGES = ('snowflake-snowpark-python', 'pandas')\nHANDLER = 'main'\n" in out


def test_the_procedure_adds_numpy_when_the_module_imports_it(tmp_path, fake, capsys):
    with_numpy = "import numpy as np\n" + BUILD_PY
    repo = make_repo(tmp_path, "wf_0006", build=with_numpy, name="build.py")

    assert deploy.main(argv(repo, "wf_0006", *SRC_6)) == 0

    assert "PACKAGES = ('snowflake-snowpark-python', 'pandas', 'numpy')\n" in capsys.readouterr().out


def test_a_runtime_that_is_not_a_version_is_a_usage_error(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, "wf_0006", build=BUILD_PY, name="build.py",
                     mappings={"program": {"snowpark_runtime": "3.11'; DROP"}})

    assert _code(argv(repo, "wf_0006", *SRC_6)) == 2
    assert "snowpark_runtime" in capsys.readouterr().err


def test_a_module_holding_dollar_dollar_is_refused_naming_its_line(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, "wf_0006", build=BUILD_PY.replace('return "OK"', 'return "$$"'), name="build.py")

    assert deploy.main(argv(repo, "wf_0006", *SRC_6)) == 1

    captured = capsys.readouterr()
    assert captured.out == "" and "build.py line 8 contains '$$'" in captured.err


# --- usage errors --------------------------------------------------------------------------------------------

def test_usage_errors_exit_2_and_connect_to_nothing(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)

    assert _code(["wf_0009", *TARGET, "--root", str(repo.root)]) == 2                       # never prepared
    assert _code(["../x", *TARGET, "--root", str(repo.root)]) == 2
    assert _code(["wf_0001", "--database", "ANALYTICS; DROP DATABASE X", "--schema", "CURATED",
                  "--root", str(repo.root)]) == 2
    assert _code(["wf_0001", "--database", "ANALYTICS", "--schema", "curated", "--root", str(repo.root)]) == 2
    assert _code(argv(repo, "wf_0001", "--src-schema", "A.B")) == 2
    assert _code(argv(repo, "wf_0001", "--password", "x")) == 2
    assert "unrecognized arguments: --password" in capsys.readouterr().err
    assert _code(["wf_0001", "--schema", "CURATED", "--root", str(repo.root)]) == 2          # no --database
    assert fake.connect_calls == []


def test_no_build_file_and_two_build_files_are_usage_errors(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, build=None)
    assert _code(argv(repo, "wf_0001", *SRC_1)) == 2
    assert "has no build.sql or build.py" in capsys.readouterr().err

    write_text(repo.wf("wf_0001", "build.sql"), BUILD_SQL)
    write_text(repo.wf("wf_0001", "build.py"), BUILD_PY)
    assert _code(argv(repo, "wf_0001", *SRC_1)) == 2
    assert "both build.sql and build.py" in capsys.readouterr().err


def test_a_workflow_with_no_manifest_names_the_file(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)
    repo.wf("wf_0001", "manifest.json").unlink()

    assert _code(argv(repo, "wf_0001", *SRC_1)) == 2
    assert "manifest.json" in capsys.readouterr().err


def test_the_manifest_is_not_changed_by_a_deployment(tmp_path, fake):
    repo = make_repo(tmp_path)
    before = read_json(repo.wf("wf_0001", "manifest.json"))

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--connection", "prod", "--execute")) == 0

    assert read_json(repo.wf("wf_0001", "manifest.json")) == before


def test_help_names_every_option_the_documents_use(capsys):
    with pytest.raises(SystemExit) as stop:
        deploy.main(["--help"])

    assert stop.value.code == 0
    out = capsys.readouterr().out
    for option in ("--database", "--schema", "--src-schema", "--stage", "--connection", "--execute", "--render",
                   "--dbt", "--root"):
        assert option in out, option


def test_the_script_runs_from_the_command_line_with_the_exit_codes_it_documents(tmp_path):
    repo = make_repo(tmp_path)
    script = str(Path(deploy.__file__))
    command = [sys.executable, script, "wf_0001", *TARGET, *SRC_1, "--root", str(repo.root)]

    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0 and done.stdout == sql_dry_run(repo), done.stderr

    write_json(repo.wf("wf_0001", "manifest.json"), {"id": "wf_0001", "status": {"translate": "TRANSLATED"}})
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 1 and "VALIDATED" in done.stderr and done.stdout == ""

    done = subprocess.run([sys.executable, script, "wf_0001", "--root", str(repo.root)],
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 2 and "--database" in done.stderr


# --- --dbt: the dbt command for the human's own profile -------------------------------------------------------------


def with_dbt_project(repo: Repo, wf_id: str = "wf_0001") -> None:
    """A workflow that has the dbt project `export_dbt.py` writes (only its `dbt_project.yml` matters here)."""
    write_text(repo.wf(wf_id, "dbt", "dbt_project.yml"), f"name: mig_{wf_id}\n")


DBT_DRY_RUN = """\
-- deploy.py wf_0001: dbt -- the command is printed only, nothing was run
-- Nothing in this repository has run on a real Snowflake account, and this dbt project never has: it ran on the
-- local DuckDB double only, against the same golden data as build.sql (workflows/wf_0001/dbt/validation.json).
-- Needs a profile named mig_wf_0001 in your own profiles directory (the commented snowflake target of
-- workflows/wf_0001/dbt/profiles.yml is a template for it) whose database is ANALYTICS: the project reads the source views in
-- ANALYTICS.MIG_SRC_WF0001 and writes its outputs to ANALYTICS.CURATED (the profile's schema is tgt_schema).
-- --target is the name of that profile's Snowflake target. Quote the JSON for your shell.
dbt build --project-dir workflows/wf_0001/dbt --profiles-dir <your profiles dir> --target <name> --vars '{"src_schema": "MIG_SRC_WF0001", "tgt_schema": "CURATED"}'
"""


def test_dbt_prints_the_dbt_build_command_with_its_vars_and_connects_to_nothing(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)
    with_dbt_project(repo)
    before = snapshot(repo)

    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--dbt")) == 0

    assert capsys.readouterr().out == DBT_DRY_RUN
    assert fake.connect_calls == [] and fake.statements == []
    assert snapshot(repo) == before, "a dry run writes nothing"


def test_dbt_without_a_src_schema_prints_the_placeholder_and_says_where_it_comes_from(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)
    with_dbt_project(repo)

    assert deploy.main(argv(repo, "wf_0001", "--dbt")) == 0

    out = capsys.readouterr().out
    assert """--vars '{"src_schema": "<SRC_SCHEMA>", "tgt_schema": "CURATED"}'""" in out
    assert out.splitlines()[-1] == ("-- <SRC_SCHEMA> is the schema of source views scripts/gen_source_views.py generates "
                                    "(MIG_SRC_WF0001); pass --src-schema S to fill it in.")


def test_dbt_names_the_profile_after_the_project(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, "wf_0012")
    with_dbt_project(repo, "wf_0012")

    assert deploy.main(argv(repo, "wf_0012", *SRC_1, "--dbt")) == 0

    out = capsys.readouterr().out
    assert "profile named mig_wf_0012 " in out and "--project-dir workflows/wf_0012/dbt " in out


def test_dbt_is_refused_when_there_is_no_dbt_project(tmp_path, fake, capsys):
    repo = make_repo(tmp_path)                                   # a SQL workflow whose project was never exported
    assert _code(argv(repo, "wf_0001", *SRC_1, "--dbt")) == 1
    err = capsys.readouterr().err
    assert err.startswith("wf_0001: workflows/wf_0001/dbt has no dbt_project.yml: there is no dbt project to deploy")
    assert "program.dbt: false" in err and "python scripts/export_dbt.py wf_0001" in err

    py = make_repo(tmp_path / "py", "wf_0006", build=BUILD_PY, name="build.py")      # a Python workflow has none
    assert _code(argv(py, "wf_0006", *SRC_6, "--dbt")) == 1
    assert "has no dbt_project.yml" in capsys.readouterr().err
    assert fake.connect_calls == []


def test_dbt_is_still_gated_on_a_validated_workflow(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, translate="NEEDS_HUMAN")
    with_dbt_project(repo)
    assert _code(argv(repo, "wf_0001", *SRC_1, "--dbt")) == 1
    assert "not 'VALIDATED'" in capsys.readouterr().err


@pytest.mark.parametrize("flag", [["--execute"], ["--render"], ["--stage", "S"], ["--connection", "prod"]])
def test_dbt_takes_none_of_the_build_file_options(tmp_path, fake, capsys, flag):
    repo = make_repo(tmp_path)
    with_dbt_project(repo)
    assert _code(argv(repo, "wf_0001", *SRC_1, "--dbt", *flag)) == 2
    assert f"--dbt only prints the dbt command and takes none of {flag[0]}" in capsys.readouterr().err
    assert fake.connect_calls == []
    assert not repo.wf("wf_0001", "build.rendered.sql").exists()


def test_dbt_does_not_read_the_build_file(tmp_path, fake, capsys):
    repo = make_repo(tmp_path, build=None)                       # no build.sql at all: the project is what is deployed
    with_dbt_project(repo)
    assert deploy.main(argv(repo, "wf_0001", *SRC_1, "--dbt")) == 0
    assert capsys.readouterr().out == DBT_DRY_RUN
