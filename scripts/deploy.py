"""Print -- or, with --execute, run -- a VALIDATED workflow's build file on Snowflake (objective-first design §4.4).

    python scripts/deploy.py <wf_id> --database DB --schema SCHEMA [--src-schema S] [--stage STAGE]
        [--connection NAME] [--execute] [--render] [--root .]
    python scripts/deploy.py <wf_id> --database DB --schema SCHEMA [--src-schema S] --dbt [--root .]

**Dry run by default.** Without `--execute` this prints the deployment and connects to nothing.

* **`build.sql`**: `CREATE STAGE IF NOT EXISTS <stage>`, the `PUT` of the file to it, then the one statement
  that runs it, `EXECUTE IMMEDIATE FROM @<stage>/build.sql USING (src => '<DB>.<SRC_SCHEMA>', tgt =>
  '<DB>.<SCHEMA>')`, which has Snowflake's own templating fill the file's `{{src}}` and `{{tgt}}`; then a
  COMMENTED `CREATE TASK … AS EXECUTE IMMEDIATE FROM …` carrying the workflow's schedule
  (`manifest.source.server_schedule`, a five-field cron, read in the session `TIMEZONE` of
  `mappings/global.yaml`) when the manifest has one. `--render` also writes
  `workflows/<id>/build.rendered.sql`: the same file with the two names substituted, for an account where
  the templated form does not run (it needs `--src-schema`).
* **`build.py`**: `USE DATABASE DB`, one `CREATE OR REPLACE PROCEDURE <SCHEMA>.<WF>_BUILD(SRC STRING, TGT
  STRING)` (`LANGUAGE PYTHON`, `RUNTIME_VERSION` from `mappings/global.yaml`'s `program.snowpark_runtime`,
  `PACKAGES` the Snowpark package and pandas (the rules bless `to_pandas()`, which needs it whether or not the module imports pandas) -- and numpy when the module imports it --, `HANDLER = 'main'`, the module
  as its `$$` body) and the `CALL` that runs it with the same two schema names. `--render` does not apply.

* **`--dbt`** (a SQL workflow with a dbt project, `workflows/<id>/dbt/`, which `scripts/export_dbt.py` writes from the
  validated `build.sql`): prints, instead of the build-file deployment, the one command that runs the project,
  `dbt build --project-dir workflows/<id>/dbt --profiles-dir <your profiles dir> --target <name> --vars '{...}'`,
  with the two `--vars` the project needs, `src_schema` (the source views, `--src-schema`) and `tgt_schema`
  (`--schema`: where the outputs are written), both in the database of YOUR dbt profile -- `--database` is
  only named in a comment, the project never sets it. It never runs dbt and never connects: there is no `--execute`
  for it (`--execute`, `--render`, `--stage` and `--connection` with `--dbt` are usage errors). A workflow with no
  dbt project (a Python workflow, `program.dbt: false`, a project not exported yet) is refused.

`<SRC_SCHEMA>` is the view schema `scripts/gen_source_views.py` generates (`MIG_SRC_<WF>`, one view per
logical source), so the build never names a production table: `--src-schema S` fills it in (in `DB`); the dry
run prints the placeholder when it is not given, and `--execute` and `--render` need it. `<stage>` is
`--stage` (`NAME`, `SCHEMA.NAME` or `DB.SCHEMA.NAME`; the missing leading parts are `DB` and `SCHEMA`) and
defaults to `DB.SCHEMA.MIG_BUILD_<WF>` -- the file is uploaded as `build.sql`, so give each workflow a stage
of its own.

**`--execute`** sends exactly the statements the dry run printed, in order, through
`SnowflakeBackend(connection_name=…)` -- a NAMED connection (`--connection` or `MIG_SNOWFLAKE_CONNECTION`),
never a password -- stopping at the first that fails. It runs the build: the `EXECUTE IMMEDIATE FROM` (or the
`CALL`) writes the workflow's outputs in their write modes, an `overwrite` output replacing what the table
held. It replaces no schema and creates nothing in `MIG_GOLDEN`/`MIG_COMPARE`; the target schema must exist.

**Only a VALIDATED workflow deploys.** `manifest.status.translate` must be `VALIDATED` -- the golden-data
validation (`validate_build.py`) is the gate -- otherwise the workflow is refused (exit 1) before anything
connects.

Exit codes: 0 printed / executed; 1 refused (not VALIDATED, no dbt project for `--dbt`), a build file that cannot be
deployed (an unknown placeholder, a `$$` in `build.py`) or a statement that failed on the account; 2 a usage error (workflow not
prepared, no or two build files, a name that is not a plain identifier, no `--src-schema` for `--execute` or
`--render`, no connection name for `--execute`, a connection that cannot be opened) or any other crash. Every
error text from the account is redacted.

Nothing here has run against a real Snowflake account: the tests drive `--execute` through a recording
stand-in for the connector. `EXECUTE IMMEDIATE FROM … USING` -- whether it fills `{{src}}` and `{{tgt}}` and
accepts the file's first `ALTER SESSION` --, the `PUT` through the connector, the stage and the Task have
never run; `--render` is the fallback. See `docs/reference/snowflake-backend.md`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Sequence

import yaml

from gen_source_views import source_view_schema
from lib import build_sql, dbt_project, snowflake_conn
from lib.backend import BackendError, SnowflakeBackend, ident
from lib.io import read_json, read_yaml, write_text
from lib.objective import build_path
from lib.paths import Repo, add_root_arg, wf_token

STAGE_PREFIX = "MIG_BUILD"
DEFAULT_RUNTIME = "3.11"
#: The only shapes a runtime version, a time zone, a warehouse and a cron expression may take where they are
#: spliced into the text this script prints.
_RUNTIME = re.compile(r"^\d+(?:\.\d+)*$")
_TIMEZONE = re.compile(r"^[A-Za-z0-9_+/-]+$")
_WAREHOUSE = re.compile(r"^[A-Z_][A-Z0-9_$]*$")
_CRON = re.compile(r"^[\w*/,?#-]+(?: [\w*/,?#-]+){4}$")
_NUMPY = re.compile(r"^\s*(?:import|from)\s+numpy\b", re.MULTILINE)

#: One thing a deployment prints: the comment lines that introduce a statement, then the statement.
Block = tuple[list[str], str]


class Refused(Exception):
    """The workflow may not be deployed, or the deployment failed on the account: exit 1."""


# --- reading what gets deployed -----------------------------------------------------------------------


def _gate(repo: Repo, wf_id: str) -> dict:
    """The manifest of a workflow that may deploy; `FileNotFoundError` for one that was never prepared and
    `Refused` for one that may not deploy."""
    manifest_path = repo.wf(wf_id, "manifest.json")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"{manifest_path}: {wf_id} has no manifest.json to deploy from")
    manifest = read_json(manifest_path)
    translate = (manifest.get("status") or {}).get("translate")
    if translate != "VALIDATED":
        raise Refused(f"manifest.json status.translate is {translate!r}, not 'VALIDATED': only a validated "
                      f"workflow deploys")
    return manifest


def _mappings(repo: Repo) -> dict:
    """`mappings/global.yaml`, `{}` when the run root has none."""
    path = repo.global_mappings
    if not path.is_file():
        return {}
    try:
        return read_yaml(path) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"{path}: not valid YAML ({error}); expected the program answers") from error


def stage_name(stage: str | None, wf_id: str, database: str, schema: str) -> str:
    """The stage's three-part name: `--stage` with its missing leading parts taken from `database` and
    `schema`, or `<DB>.<SCHEMA>.MIG_BUILD_<WF>` when none was given. `ValueError` for a name that is not
    plain identifiers."""
    if stage is None:
        return f"{database}.{schema}.{STAGE_PREFIX}_{wf_token(wf_id)}"
    parts = stage.split(".")
    if len(parts) > 3:
        raise ValueError(f"--stage {stage!r} has {len(parts)} parts; expected NAME, SCHEMA.NAME or DB.SCHEMA.NAME")
    for part in parts:
        ident(part, "--stage part")
    return ".".join([database, schema][:3 - len(parts)] + parts)


def _runtime(mappings: dict) -> str:
    runtime = str((mappings.get("program") or {}).get("snowpark_runtime") or DEFAULT_RUNTIME)
    if not _RUNTIME.match(runtime):
        raise ValueError(f"mappings/global.yaml program.snowpark_runtime is {runtime!r}; expected a version "
                         f"such as {DEFAULT_RUNTIME}")
    return runtime


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# --- the Task -----------------------------------------------------------------------------------------


def _task_lines(wf_id: str, manifest: dict, mappings: dict, database: str, schema: str, body: str) -> list[str]:
    """The commented `CREATE TASK` that runs `body` on the workflow's Alteryx schedule, or one comment line
    saying there is none. Nothing here has been created on an account."""
    schedule = (manifest.get("source") or {}).get("server_schedule")
    if not schedule:
        return ["-- manifest.json records no schedule (source.server_schedule): no Task is printed."]
    recorded = " ".join(str(schedule).split())
    cron = f"USING CRON {recorded}" if _CRON.match(recorded) else "USING CRON <CRON>"
    timezone = str((mappings.get("session") or {}).get("TIMEZONE") or "")
    warehouse = str((mappings.get("program") or {}).get("warehouse") or "")
    task = f"{database}.{schema}.{wf_token(wf_id)}_BUILD_TASK"
    return [
        f"-- Run it on the Alteryx schedule (manifest.json source.server_schedule {recorded!r}), here read in the",
        "-- session TIMEZONE of mappings/global.yaml. Commented out: no Task has ever been created from this.",
        f"-- CREATE OR REPLACE TASK {task}",
        f"--   WAREHOUSE = {warehouse if _WAREHOUSE.match(warehouse) else '<WAREHOUSE>'}",
        f"--   SCHEDULE = '{cron} {timezone if _TIMEZONE.match(timezone) else '<TIMEZONE>'}'",
        "-- AS",
        f"--   {body};",
        f"-- ALTER TASK {task} RESUME;"]


# --- the two deployments ------------------------------------------------------------------------------


def check_placeholders(text: str, src: str, tgt: str) -> None:
    """`Refused` when `build.sql` holds a placeholder Snowflake's templating could not fill in. The file is
    rendered once with the two names -- the job `EXECUTE IMMEDIATE FROM … USING` does -- so a deployment
    never sends a file that cannot be filled in."""
    try:
        build_sql.render(text, {"src": src, "tgt": tgt})
    except ValueError as exc:
        raise Refused(f"build.sql: {exc}") from None


def sql_blocks(wf_id: str, build: Path, stage: str, src: str, tgt: str) -> tuple[list[Block], str]:
    """The blocks of a `build.sql` deployment and the statement that runs it (the Task's body)."""
    run = f"EXECUTE IMMEDIATE FROM @{stage}/build.sql USING (src => {_quote(src)}, tgt => {_quote(tgt)})"
    notes = [
        "-- Nothing in this repository has run on a real Snowflake account, and Snowflake's own templating of the",
        "-- two names below (EXECUTE IMMEDIATE FROM ... USING) has never run on one: --render writes",
        "-- build.rendered.sql with the names substituted, for an account where it does not.",
        f"-- workflows/{wf_id}/build.sql reads {{{{src}}}}.<LOGICAL> and writes {{{{tgt}}}}.<LOGICAL>, with "
        f"{{{{src}}}} = {src} and {{{{tgt}}}} = {tgt}."]
    return [
        (notes, f"CREATE STAGE IF NOT EXISTS {stage}"),
        ([f"-- Upload the file as @{stage}/build.sql, uncompressed, replacing any earlier copy."],
         f"PUT {_quote('file://' + build.resolve().as_posix())} @{stage} AUTO_COMPRESS = FALSE OVERWRITE = TRUE"),
        (["-- Run it: Snowflake fills {{src}} and {{tgt}}, then runs the file's statements in order."], run),
    ], run


def procedure_statement(wf_id: str, schema: str, runtime: str, body: str) -> str:
    """The one `CREATE OR REPLACE PROCEDURE` that wraps a `build.py`'s module. `Refused` for a module holding
    `$$`, which would end the procedure body early."""
    for number, line in enumerate(body.splitlines(), 1):
        if "$$" in line:
            raise Refused(f"build.py line {number} contains '$$', which would end the procedure body; "
                          f"expected the module without it")
    # pandas always: `to_pandas()` is the rules' blessed row-sequential route and needs it at run time even
    # when the module never imports pandas itself; numpy only when the module imports it.
    packages = "'snowflake-snowpark-python', 'pandas'" + (", 'numpy'" if _NUMPY.search(body) else "")
    return (f"CREATE OR REPLACE PROCEDURE {schema}.{wf_token(wf_id)}_BUILD(SRC STRING, TGT STRING)\n"
            f"RETURNS STRING\nLANGUAGE PYTHON\nRUNTIME_VERSION = '{runtime}'\nPACKAGES = ({packages})\n"
            f"HANDLER = 'main'\nEXECUTE AS CALLER\nAS\n$$\n{body.rstrip()}\n$$")


def python_blocks(wf_id: str, schema: str, database: str, runtime: str, body: str, src: str,
                  tgt: str) -> tuple[list[Block], str]:
    """The blocks of a `build.py` deployment and the `CALL` a Task would run (named in full: a Task is not
    in the session that `USE DATABASE` set)."""
    procedure = f"{wf_token(wf_id)}_BUILD"
    call = f"CALL {schema}.{procedure}({_quote(src)}, {_quote(tgt)})"
    notes = [
        "-- Nothing in this repository has run on a real Snowflake account; no procedure has been created from this.",
        f"-- workflows/{wf_id}/build.py deploys as one Python stored procedure: main(session, src, tgt) reads",
        f"-- src.<LOGICAL> and writes tgt.<LOGICAL>, with src = {src} and tgt = {tgt}.",
        "-- Verify RUNTIME_VERSION and PACKAGES against the account (and its Anaconda terms) before running the DDL."]
    return [
        (notes, f"USE DATABASE {database}"),
        (["-- The procedure."], procedure_statement(wf_id, schema, runtime, body)),
        (["-- Run it."], call),
    ], f"CALL {database}.{schema}.{procedure}({_quote(src)}, {_quote(tgt)})"


def dbt_script(repo: Repo, wf_id: str, database: str, schema: str, src_schema: str | None) -> str:
    """What `--dbt` prints: the comment lines that say what the command needs, then the one `dbt build` command.
    `Refused` when the workflow has no dbt project. Nothing is run."""
    project = repo.wf(wf_id, "dbt")
    if not (project / "dbt_project.yml").is_file():
        raise Refused(f"{project.relative_to(repo.root).as_posix()} has no dbt_project.yml: there is no dbt project to "
                      f"deploy (a Python workflow, or `program.dbt: false` in mappings/global.yaml, has none; "
                      f"run python scripts/export_dbt.py {wf_id} for a SQL workflow)")
    shown = f"workflows/{wf_id}/dbt"
    src = src_schema or "<SRC_SCHEMA>"
    profile = dbt_project.project_name(wf_id)
    variables = json.dumps({"src_schema": src, "tgt_schema": schema})
    lines = [
        f"-- deploy.py {wf_id}: dbt -- the command is printed only, nothing was run",
        "-- Nothing in this repository has run on a real Snowflake account, and this dbt project never has: it ran on the",
        f"-- local DuckDB double only, against the same golden data as build.sql ({shown}/validation.json).",
        f"-- Needs a profile named {profile} in your own profiles directory (the commented snowflake target of",
        f"-- {shown}/profiles.yml is a template for it) whose database is {database}: the project reads the source views in",
        f"-- {database}.{src} and writes its outputs to {database}.{schema} (the profile's schema is tgt_schema).",
        "-- --target is the name of that profile's Snowflake target. Quote the JSON for your shell.",
        f"dbt build --project-dir {shown} --profiles-dir <your profiles dir> --target <name> --vars '{variables}'"]
    if src_schema is None:
        lines.append(f"-- <SRC_SCHEMA> is the schema of source views scripts/gen_source_views.py generates "
                     f"({source_view_schema(wf_id)}); pass --src-schema S to fill it in.")
    return "\n".join(lines) + "\n"


def script(wf_id: str, heading: str, blocks: list[Block], task: list[str]) -> str:
    """What a deployment prints: the heading, each block (its comments, then its statement ended by `;`), a
    blank line and the Task."""
    lines = [f"-- deploy.py {wf_id}: {heading}"]
    for comments, statement in blocks:
        lines += [*comments, f"{statement};"]
    return "\n".join([*lines, "", *task]) + "\n"


# --- running it ---------------------------------------------------------------------------------------


def _execute(statements: list[str], connection: str) -> int:
    backend = SnowflakeBackend(connection_name=connection)      # BackendError: cannot open -> exit 2
    try:
        for statement in statements:
            try:
                backend.execute(statement)
            except BackendError as exc:
                raise Refused(f"deploy stopped at {statement.splitlines()[0]}: {exc}") from None
    finally:
        backend.close()
    return len(statements)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id", help="workflow id, e.g. wf_0001")
    parser.add_argument("--database", required=True, metavar="DB", help="the database to deploy into")
    parser.add_argument("--schema", required=True, metavar="SCHEMA",
                        help="the schema the workflow's outputs are written to ({{tgt}}); it must exist")
    parser.add_argument("--src-schema", default=None, metavar="S",
                        help="the schema in DB holding the source views ({{src}}), e.g. MIG_SRC_<WF> from "
                             "gen_source_views.py; needed for --execute and --render")
    parser.add_argument("--stage", default=None, metavar="STAGE",
                        help="build.sql only: the stage to PUT it to -- NAME, SCHEMA.NAME or DB.SCHEMA.NAME "
                             "(default: DB.SCHEMA.MIG_BUILD_<WF>)")
    parser.add_argument("--connection", default=None, metavar="NAME",
                        help="--execute: the connections.toml entry to use (default: $MIG_SNOWFLAKE_CONNECTION)")
    parser.add_argument("--execute", action="store_true",
                        help="run the deployment on the account (default: print it and connect to nothing)")
    parser.add_argument("--render", action="store_true",
                        help="build.sql only: also write build.rendered.sql, the file with {{src}} and {{tgt}} "
                             "substituted, for an account where EXECUTE IMMEDIATE FROM ... USING does not "
                             "fill them in")
    parser.add_argument("--dbt", action="store_true",
                        help="print the `dbt build` command (and its --vars) that runs the workflow's dbt project "
                             "with your own Snowflake profile, instead of the build-file deployment; it only "
                             "prints, and refuses a workflow with no dbt project")
    add_root_arg(parser)
    return parser


def deploy(repo: Repo, args: argparse.Namespace) -> None:
    """Prints (and, with `--execute`, runs) the deployment of `args.wf_id`. `Refused` and `BackendError`
    and the usage errors (`FileNotFoundError`, `ValueError`) are `main`'s to turn into exit codes."""
    wf_id = args.wf_id
    database, schema = ident(args.database, "--database"), ident(args.schema, "--schema")
    src_schema = ident(args.src_schema, "--src-schema") if args.src_schema is not None else None
    manifest = _gate(repo, wf_id)
    if args.dbt:
        taken = [flag for flag, given in (("--execute", args.execute), ("--render", args.render),
                                          ("--stage", args.stage is not None), ("--connection", args.connection is not None))
                 if given]
        if taken:
            raise ValueError(f"--dbt only prints the dbt command and takes none of {', '.join(taken)}; run the printed "
                             f"command yourself with your own profile")
        sys.stdout.write(dbt_script(repo, wf_id, database, schema, src_schema))
        return
    build = build_path(repo, wf_id)
    if build is None:
        raise FileNotFoundError(f"{repo.wf(wf_id)}: {wf_id} has no build.sql or build.py to deploy; expected the "
                                f"translate stage to write one")
    is_sql = build.suffix == ".sql"
    if args.render and not is_sql:
        raise ValueError("--render applies to a build.sql; this workflow has a build.py, which deploys as a "
                         "stored procedure")
    if (args.execute or args.render) and src_schema is None:
        raise ValueError(f"--src-schema is required to {'--execute' if args.execute else '--render'}: the schema in "
                         f"{database} whose views the build's {{{{src}}}} reads (scripts/gen_source_views.py "
                         f"generates {source_view_schema(wf_id)})")
    connection = snowflake_conn.connection_name(args.connection) if args.execute else None
    mappings = _mappings(repo)
    src, tgt = f"{database}.{src_schema or '<SRC_SCHEMA>'}", f"{database}.{schema}"
    text = build.read_text(encoding="utf-8")
    if is_sql:
        stage = stage_name(args.stage, wf_id, database, schema)
        check_placeholders(text, src, tgt)
        blocks, body = sql_blocks(wf_id, build, stage, src, tgt)
    else:
        blocks, body = python_blocks(wf_id, schema, database, _runtime(mappings), text, src, tgt)
    if src_schema is None:
        blocks[0][0].append(f"-- <SRC_SCHEMA> is the schema of source views scripts/gen_source_views.py generates "
                            f"({source_view_schema(wf_id)}); pass --src-schema S to fill it in.")
    heading = (f"executing through the named connection {connection!r}" if args.execute
               else "dry run -- printed only, nothing was executed")
    sys.stdout.write(script(wf_id, heading, blocks, _task_lines(wf_id, manifest, mappings, database, schema, body)))
    if args.render:
        target = repo.wf(wf_id, "build.rendered.sql")
        write_text(target, build_sql.render(text, {"src": src, "tgt": tgt}))
        print(f"\n-- wrote {target.relative_to(repo.root).as_posix()}: build.sql with {{{{src}}}} = {src} and "
              f"{{{{tgt}}}} = {tgt} substituted")
    if args.execute:
        count = _execute([statement for _, statement in blocks], connection)
        print(f"-- executed {count} statements through the named connection {connection!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = Repo(args.root)
    try:
        deploy(repo, args)
        return 0
    except Refused as exc:
        print(f"{args.wf_id}: {snowflake_conn.redact(str(exc))}", file=sys.stderr)
        return 1
    except BackendError as exc:  # only opening the connection can get here: statements become Refused
        print(f"{args.wf_id}: {snowflake_conn.redact(str(exc))}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        parser.error(snowflake_conn.redact(str(exc)))       # exit 2: a usage error; nothing was deployed
    except Exception:            # noqa: BLE001 -- exit 2 is "anything unexpected"; never a bare traceback exit 1
        sys.stderr.write(snowflake_conn.redact(traceback.format_exc()))
        return 2


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
