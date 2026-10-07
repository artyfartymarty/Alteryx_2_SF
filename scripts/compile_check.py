"""Does a workflow's build file compile and run on an empty sandbox, and is it minimal?

    python scripts/compile_check.py <wf_id> [--root .]

The cheap gate before parity (objective-first design §4.4-§4.6): it catches a build file that is not valid
Snowflake SQL, that reads a table nobody mapped, that writes an output in the wrong form, or that produces
different columns from the ones `objective.json` promises -- all without golden data. The run is on an empty
in-memory sandbox, so it says nothing about values; `validate_build.py` does that. The deliverable is ONE
file per workflow, `build.sql` or (when the objective says `language: python`) `build.py`.

Writes `workflows/<wf_id>/compile_check.json`: `{"ok", "errors", "lint"}`. Every error is one line that starts
with the name of the check, then `: `, then the line of the build file it is about and what was expected.
Exit 0 when `ok`, 1 when any check failed, 2 when there is nothing to check (no objective, no build file,
both build files, an objective that is T3 or does not name an output's logical name or write mode).
Nothing here has run on Snowflake: passing means sqlglot's Snowflake parser and DuckDB accepted a `build.sql`,
or the Snowpark rules and a Snowpark Local Testing run accepted a `build.py`.

`build.sql` -- the named checks, in the order they are reported:

* `template:placeholder` -- the only placeholders are `{{src}}` and `{{tgt}}` (spaces inside the braces
  are fine), and in code each is followed by `.<LOGICAL>`.
* `session:form` -- an optional FIRST statement `ALTER SESSION SET <parameter> = <literal>, ...`, the
  parameter TIMEZONE, WEEK_START or one `mappings/global.yaml`'s `session` block names. It is recorded, not
  applied, on the local double.
* `external_access` -- nothing reaches a file, a stage, the network, an extension or the engine's settings:
  `COPY`, `PUT`/`GET`, `ATTACH`, `INSTALL`/`LOAD`, `PRAGMA`, `SET`, `CREATE STAGE`/`SECRET`/`... INTEGRATION`,
  a function that reads files, settings or the network, a table reference that is a file or a stage.
* `statement:kind` -- `CREATE [OR REPLACE] [TEMPORARY] TABLE ... AS <query>`, `INSERT INTO`, `MERGE INTO`,
  and `TRUNCATE TABLE`/`DELETE FROM`/`UPDATE` on a target only. No procedure, `CALL`, `EXECUTE IMMEDIATE`,
  scripting block, `LET` or `DECLARE`.
* `parse` -- every statement parses with sqlglot's Snowflake dialect (and is not a raw `Command` fallback).
* `tables:source`, `tables:target`, `tables:temporary` -- sources are read only as `{{src}}.<input logical>`,
  targets written only as `{{tgt}}.<output logical>` (and read only after they are written, or by their own
  append / update_insert write), temporary tables use plain unqualified names that are not logical names and
  are created with `CREATE [OR REPLACE] TEMPORARY TABLE` before they are read. A table named through
  `IDENTIFIER('...')` / `IDENTIFIER(:X)` (the stored-procedure habit) is refused under `tables:source` when
  read and `tables:target` when written.
* `write_mode` -- each output is written exactly once in the form its write mode needs (`overwrite` one
  `CREATE OR REPLACE TABLE ... AS`; `append` one `INSERT INTO`; `truncate_append` a `TRUNCATE`/`DELETE FROM`
  (no WHERE) IMMEDIATELY followed by one `INSERT INTO`; `update_insert` one `MERGE INTO` on exactly the
  objective's keys with both `WHEN MATCHED THEN UPDATE` and `WHEN NOT MATCHED THEN INSERT`);
  `write_mode:pre_post` -- any other statement on the target is the Output tool's PreSQL (IMMEDIATELY before
  the write) or PostSQL (IMMEDIATELY after it), allowed only when the objective's output has one and required
  when it does; "immediately" means no other statement of the file sits between them.
* `steps:missing`, `steps:unknown`, `steps:marker` -- every objective step has at least one region (a comment
  line `-- step s1` or `-- step s1, s3` starts one, up to the next marker), a marker naming an unknown step is
  refused, and a comment that starts like a marker but is not one (`-- Step s1: clean`) is named.
* `run`, `outputs:columns` -- the file runs on empty tables created from the objective's input columns, and
  leaves every output table with exactly the objective's column names, in order, compared case-insensitively.
  The tables an `append` / `truncate_append` / `update_insert` write extends exist beforehand with the
  objective's columns, so the run cannot see which columns the write names: `outputs:columns` also holds the
  INSERT's column list (and a MERGE's `WHEN NOT MATCHED THEN INSERT` list) to the objective's column names, in
  its order, and refuses an INSERT with no column list, naming the missing / extra / out-of-order columns.
* `dbt:form` -- when `mappings/global.yaml` has `program.dbt: true` (the default) and the objective's language is
  `sql`, the build must be one `export_dbt.py` can convert to a dbt project: the check runs that conversion in
  memory (nothing is written) and reports a statement with no dbt form by its line and the reason. It catches, while
  the translator can still fix them, the shapes the other checks accept: a `MERGE` with a condition on a `WHEN`
  or an `UPDATE SET` that is not a copy of the source (`<column> = <source>.<column>`), an `INSERT ... VALUES`, a
  `WITH` before an `INSERT`, an `INSERT` into a temporary table, a temporary table created twice, a PreSQL/PostSQL
  statement that reads a temporary table. Only checked once the static checks, the write forms and the column
  lists are clean.
* `language` -- the build file is the one the objective's `language` names (`build.sql` for `sql`, `build.py`
  for `python`); a `build.py` next to a `sql` objective (or the reverse) is exit 1, not a usage error.

`build.py` -- the same step markers (`# step s1`), then `python:dollar_quote` (`deploy.py` wraps the module
in `$$ ... $$`, so it may not contain one), then the Snowpark static rules of `lib.snowpark_rules`
(`rule:*`: the `main(session, src, tgt)` signature, imports, session scope, table names read from the
objective, `rule:write_mode`, ...), then a run of `main` in the runtime sandbox (`lib.snowpark_sandbox`) on
empty input tables (`run`, `outputs:columns`). The Snowpark Local Testing Framework cannot `filter` an empty
table, so when `main` raises on empty inputs the run is repeated ONCE with one row of NULLs in each input table
and that second run's answer stands. A build that only copes with that row is therefore not caught here on a
genuinely empty table: `validate_build.py`'s golden `empty` set is the backstop.

The lint of `lib.minimality` (`pass_through_cte`, `tool_by_tool`, `single_use_chain`) is computed for a
`build.sql` only and reported under `lint`; a flag never changes `ok` or the exit code.
"""
from __future__ import annotations

import argparse
import re
import sys
import traceback
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.errors import SqlglotError

import export_dbt
from lib import build_parse, build_sql, minimality, objective as objective_lib, snowpark_rules
from lib.backend import SANDBOX_DB, BackendError, DuckDBBackend
from lib.io import read_yaml, write_json
from lib.paths import Repo, add_root_arg
from lib.write_modes import ContractError, TargetWrite, objective_targets

COMPILE_SCHEMA = "MIG_COMPILE"
WORK_SCHEMA = "MIG_WORK"
#: What `{{src}}` and `{{tgt}}` are rendered to for the empty-sandbox run.
RUN_VALUES = {"src": f"{SANDBOX_DB}.{COMPILE_SCHEMA}", "tgt": f"{SANDBOX_DB}.{WORK_SCHEMA}"}

#: The parameters an `ALTER SESSION SET` may name when `mappings/global.yaml` says nothing else: the two
#: the translator sets for Alteryx parity (its DateTimeNow() used the server's zone; week numbering).
SESSION_PARAMETERS = frozenset({"TIMEZONE", "WEEK_START"})

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*|[(),;]")
_NAME = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")'


class UsageError(Exception):
    """There is nothing to check, or nothing to check against (exit 2): a missing prerequisite file, an
    unreadable objective, both build files, an objective that cannot be checked against."""


def _clean(text: str) -> str:
    """sqlglot underlines the offending token with ANSI codes, a message may span lines, and a statement is
    parsed with sentinel names for the placeholders (`lib.build_parse`); a compile error is one plain line that
    names `{{src}}` and `{{tgt}}` as the file wrote them."""
    text = _ANSI_RE.sub("", str(text))
    text = text.replace(f"{build_parse.SRC_MARK}.{build_parse.SRC_MARK}", "{{src}}")
    text = text.replace(f"{build_parse.TGT_MARK}.{build_parse.TGT_MARK}", "{{tgt}}")
    return " ".join(text.split())


def _error(check: str, line: int | None, text: str) -> str:
    """`<check>: line N: <text>` (or `<check>: <text>` for a problem with no line to point at)."""
    return _clean(f"{check}: line {line}: {text}" if line else f"{check}: {text}")


# --- the entry point ----------------------------------------------------------------------------------


def compile_check(repo: Repo, wf_id: str) -> dict:
    """Checks `workflows/<wf_id>/build.sql` (or `build.py`) against `objective.json`, writes
    `compile_check.json` and returns the report `{"ok", "errors", "lint"}` (no `lint` for a `build.py`).
    `UsageError` when there is nothing to check (`ContractError` when the objective cannot say what a
    check needs): neither writes a report, and the previous report is removed first, so a report on disk always
    belongs to the latest invocation."""
    try:
        report_path = repo.wf(wf_id, "compile_check.json")
        report_path.unlink(missing_ok=True)
        objective = objective_lib.load(repo, wf_id)
        path = objective_lib.build_path(repo, wf_id)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    if objective.get("tier") == "T3":
        raise UsageError(f"{wf_id}: objective.json says tier T3, so the workflow is not translated and has no "
                         f"build file to check")
    if objective.get("language") not in objective_lib.LANGUAGES:
        raise UsageError(f"{wf_id}: objective.json has language {objective.get('language')!r}; expected one of "
                         f"{', '.join(objective_lib.LANGUAGES)} before a build file can be checked against it")
    if path is None:
        raise FileNotFoundError(f"{wf_id} has no build file to check: expected {repo.wf(wf_id, 'build.sql')} "
                                f"(or build.py when the objective's language is python)")
    targets = objective_targets(objective)
    sources = _sources(objective)
    language = "python" if path.suffix == ".py" else "sql"
    text = path.read_text(encoding="utf-8-sig")        # a BOM (PowerShell's Out-File) is not part of the SQL
    if objective.get("language") != language:
        report = {"ok": False, "errors": [_error(
            "language", None, f"{path.name} is a {language} build but objective.json says language "
                              f"{objective.get('language')!r}; write {'build.py' if objective.get('language') == 'python' else 'build.sql'} "
                              f"and remove {path.name}")]}
    elif language == "python":
        report = _check_python(text, objective, targets, sources)
    else:
        report = _check_sql(repo, wf_id, text, objective, targets, sources)
    write_json(report_path, report)
    return report


def _sources(objective: dict) -> list[dict]:
    """The objective's inputs, each with a `logical` name (a `ContractError` when intake has not named one)."""
    sources = []
    for position, entry in enumerate(objective.get("inputs") or []):
        if not entry.get("logical"):
            raise ContractError(f"objective.json inputs[{position}] (tool {entry.get('tool_id')}) has no logical "
                                f"name; expected intake to have named it (run objective_scaffold.py --apply once "
                                f"intake is READY)")
        sources.append(entry)
    return sources


# --- steps (build.sql and build.py share them) -------------------------------------------------------------


def step_errors(text: str, comment: str, objective: dict) -> list[str]:
    """`steps:marker` for a comment that starts like a marker but is not one, `steps:unknown` for a marker
    naming a step the objective does not have, `steps:missing` for an objective step with no region."""
    known = [str(step.get("id")) for step in objective.get("steps") or []]
    errors: list[str] = []
    example = f"{comment} step s1"
    for marker in build_sql.step_markers(text, comment):
        if not marker.steps:
            errors.append(_error("steps:marker", marker.line,
                                 f"`{marker.text}` starts like a step marker but is not one; write a comment line "
                                 f"of exactly `{example}` or `{example}, s3` (lower case, ids separated by "
                                 f"commas, nothing else on the line)"))
            continue
        for step in marker.steps:
            if step not in known:
                errors.append(_error("steps:unknown", marker.line,
                                     f"`{marker.text}` names {step}, which objective.json does not have; "
                                     f"its steps are {', '.join(known) or '(none)'}"))
    covered = {step for region in build_sql.step_regions(text, comment) for step in region.steps}
    for step in known:
        if step not in covered:
            errors.append(_error("steps:missing", None,
                                 f"step {step} has no region; put a `{comment} step {step}` comment line before "
                                 f"the code that does it, with at least one line of code after it"))
    return errors


# --- build.sql ----------------------------------------------------------------------------------------------


def _check_sql(repo: Repo, wf_id: str, text: str, objective: dict, targets: list[TargetWrite],
               sources: list[dict]) -> dict:
    statements = build_sql.split_statements(text)
    inputs = {str(entry["logical"]).upper() for entry in sources}
    outputs = {target.logical.upper(): target for target in targets}

    placeholder = _placeholder_errors(text)
    session = _session_errors(statements, _session_parameters(repo))
    steps = step_errors(text, "--", objective)
    per_statement: list[str] = []
    parsed: list[tuple[build_sql.Statement, exp.Expression]] = []
    # A placeholder that is not one of the two cannot be parsed as a table name: it is reported alone, with
    # the checks that do not read the statements' trees, rather than as a parse error on top of it.
    for statement in [] if placeholder else statements:
        problems, tree = _statement_errors(statement)
        per_statement += problems
        if tree is not None and not problems:
            parsed.append((statement, tree))
    tables = _table_errors(parsed, inputs, outputs)
    writes = [] if placeholder else _write_errors(statements, targets)
    columns = [] if placeholder else _column_list_errors(parsed, objective, targets)

    errors = placeholder + session + per_statement + tables + writes + columns + steps
    if not (placeholder or session or per_statement or tables):
        errors += _run_errors(statements, sources, targets, objective)
    if not (placeholder or session or per_statement or tables or writes or columns):
        errors += _dbt_form_errors(repo, wf_id)
    lint = minimality.lint(parsed, objective, lines=len(text.splitlines()), statements=len(statements))
    return {"ok": not errors, "errors": errors, "lint": lint}


# dbt:form ------------------------------------------------------------------------------------------------------


def _dbt_form_errors(repo: Repo, wf_id: str) -> list[str]:
    """`dbt:form` when the workflow is delivered as a dbt project (`mappings/global.yaml` `program.dbt` true, a SQL
    objective) and `export_dbt.py` cannot convert the build: the conversion is run in memory, nothing is written,
    and a statement with no dbt form is one error naming its line. A `program.dbt` that is neither true nor false
    is a usage error (the exporter would refuse it too)."""
    try:
        export_dbt.export(repo, wf_id, write=False)
    except export_dbt.NoDbtForm as exc:
        return [_error("dbt:form", exc.line, f"no dbt form for {exc.what}; the dbt project is converted from this file by "
                                             f"export_dbt.py, so write the statement in a shape it converts "
                                             f"(docs/reference/build-file.md, \"The conversion\")")]
    except (export_dbt.UsageError, ContractError, ValueError) as exc:
        raise UsageError(str(exc)) from exc
    return []


# template:placeholder ----------------------------------------------------------------------------------------

_BARE_PLACEHOLDER = re.compile(r"\{\{\s*(?:src|tgt)\s*\}\}(?!\s*\.\s*" + _NAME + ")")
_RULE = "the only placeholders are {{src}} (source schema) and {{tgt}} (target schema), each written {{src}}.<LOGICAL>"


def _placeholder_errors(text: str) -> list[str]:
    errors = []
    for name, line in build_sql.placeholders(text):
        if name not in build_sql.PLACEHOLDER_NAMES:
            errors.append(_error("template:placeholder", line, f"{{{{ {name} }}}} is not a placeholder; {_RULE}"))
    blanked = re.sub(r"\{\{([^{}\n]*)\}\}", lambda match: " " * len(match.group(0)), text)
    stray = blanked.find("{{")
    if stray >= 0:
        errors.append(_error("template:placeholder", text.count("\n", 0, stray) + 1,
                             f"'{{{{' is never closed by '}}}}'; {_RULE}"))
    code = build_sql.masked_code(text)
    for match in _BARE_PLACEHOLDER.finditer(code):
        shown = " ".join(match.group(0).split())
        errors.append(_error("template:placeholder", code.count("\n", 0, match.start()) + 1,
                             f"{shown} is not followed by .<LOGICAL>; {_RULE}"))
    return errors


# session:form --------------------------------------------------------------------------------------------------

_SETTING_LITERAL = re.compile(r"""'(?:[^'\\]|''|\\.)*'|-?\d+(?:\.\d+)?|TRUE|FALSE""", re.IGNORECASE)


def _session_parameters(repo: Repo) -> frozenset[str]:
    """TIMEZONE, WEEK_START and every key of `mappings/global.yaml`'s `session` block."""
    named: set[str] = set()
    if repo.global_mappings.is_file():
        block = (read_yaml(repo.global_mappings) or {}).get("session") or {}
        named = {str(key).upper() for key in block} if isinstance(block, dict) else set()
    return SESSION_PARAMETERS | named


def _is_session_statement(statement: build_sql.Statement) -> bool:
    words = [word.upper() for word in _WORD.findall(build_sql.masked_code(statement.text))[:2]]
    return words == ["ALTER", "SESSION"]


def _session_errors(statements: list[build_sql.Statement], allowed: frozenset[str]) -> list[str]:
    errors = []
    form = ("an optional first statement ALTER SESSION SET <parameter> = <literal>, ... with a parameter from "
            f"{', '.join(sorted(allowed))}")
    for index, statement in enumerate(statements):
        if not _is_session_statement(statement):
            continue
        line = statement.start_line
        if index != 0:
            errors.append(_error("session:form", line, f"ALTER SESSION is allowed only as the first statement; {form}"))
            continue
        settings = build_sql.session_settings(statement)
        if settings is None:
            errors.append(_error("session:form", line, f"this ALTER SESSION is not ALTER SESSION SET "
                                                       f"<parameter> = <literal>, ...; expected {form}"))
            continue
        raw = re.sub(r"(?is)^\s*ALTER\s+SESSION\s+SET\s+", "", build_sql.without_comments(statement.text).strip())
        for assignment in build_sql.split_code(raw, ","):
            name, _, value = assignment.partition("=")
            if name.strip().upper() not in allowed:
                errors.append(_error("session:form", line, f"parameter {name.strip().upper()} is not one a build "
                                                           f"file sets; expected {', '.join(sorted(allowed))}"))
            elif not _SETTING_LITERAL.fullmatch(value.strip()):
                errors.append(_error("session:form", line, f"{name.strip().upper()} is set to `{value.strip()}`, "
                                                           f"which is not a literal; expected a quoted string, a "
                                                           f"number or TRUE/FALSE"))
    return errors


# statement kind, external access, parse ---------------------------------------------------------------------------

#: Statement heads that move data to or from files, stages or the network, load extensions, or change the
#: engine's settings -- DuckDB's and Snowflake's.
_EXTERNAL_HEADS = frozenset({"COPY", "PUT", "GET", "LIST", "LS", "REMOVE", "RM", "ATTACH", "DETACH", "INSTALL",
                             "FORCE", "LOAD", "EXPORT", "IMPORT", "PRAGMA", "SET", "RESET", "UNSET"})
#: `CREATE` of an object that reaches outside the database: a stage, a secret, an integration, an external
#: table, a file format.
_EXTERNAL_CREATE_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:TEMP|TEMPORARY|PERSISTENT)\s+)?"
    r"(?P<what>STAGE|SECRET|(?:\w+\s+)?INTEGRATION|EXTERNAL\s+TABLE|FILE\s+FORMAT)\b", re.IGNORECASE)
#: Functions that read files, settings, the environment or the network, or run SQL from a string (DuckDB's
#: names, matched on the lower-cased name).
_DENIED_FUNCTION = re.compile(
    r"^(?:read_\w*|\w*_scan|glob|getenv|current_setting|sniff_csv|parquet_\w*|load\w*|install\w*|http\w*"
    r"|query|query_table|duckdb_\w*|pragma_\w*|which_secret|json_execute_serialized_sql|iceberg_\w*|delta_\w*"
    r"|sqlite_\w*|postgres_\w*|mysql_\w*|st_read\w*|\w*_metadata|checkpoint|force_checkpoint|enable_\w*"
    r"|disable_\w*)$")
#: Snowflake's own stage and file functions.
_SNOWFLAKE_FILE_FUNCTION = re.compile(
    r"^(?:get_presigned_url|build_scoped_file_url|build_stage_file_url|get_stage_location|get_relative_path"
    r"|get_absolute_path|infer_schema|to_file|fl_\w+|system\$\w+)$")
_EXTERNAL_TAIL = "a build file reads its sources through {{src}}.<LOGICAL> and writes tables, nothing else"
_PATH_CHARACTERS = re.compile(r"[./\\:]")
_PATH_SEPARATORS = re.compile(r"[/\\]")
#: Scripting and procedure heads: a build file is plain statements, not a program.
_SCRIPTING_HEADS = frozenset({"CALL", "EXECUTE", "LET", "DECLARE", "BEGIN", "END", "RETURN", "IF", "FOR", "WHILE",
                              "REPEAT", "LOOP", "BREAK", "CONTINUE", "RAISE", "OPEN", "FETCH", "CLOSE",
                              "COMMIT", "ROLLBACK", "START"})
_KINDS = ("a build file holds only an optional first ALTER SESSION SET, CREATE [OR REPLACE] [TEMPORARY] TABLE ... AS "
          "<query>, INSERT INTO, MERGE INTO and, on a {{tgt}}.<LOGICAL> target, TRUNCATE TABLE, DELETE FROM and UPDATE")


def _function_name(node: exp.Func) -> str:
    return str(node.name if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)) else node.sql_name()).lower()


def _head_text(code: str) -> str:
    """The first few words of a statement as the file wrote them -- placeholders with their braces
    (`DELETE FROM {{src}}.ITEMS`), not the words a tokenizer would split them into."""
    words = _clean(code).split()
    shown = " ".join(words[:5])
    return shown + (" ..." if len(words) > 5 else "")


def _statement_errors(statement: build_sql.Statement) -> tuple[list[str], exp.Expression | None]:
    """The `external_access`, `statement:kind` and `parse` errors of one statement and, when it is one of the
    allowed kinds and parses, its tree. An `ALTER SESSION` is `session:form`'s and returns neither."""
    if _is_session_statement(statement):
        return [], None
    code = build_sql.masked_code(statement.text)
    words = _WORD.findall(code)
    head = words[0].upper() if words else ""
    line = statement.start_line
    if head in _EXTERNAL_HEADS:
        return [_error("external_access", line, f"{head} moves data to or from files, stages or the network, or "
                                                f"changes the engine's settings; {_EXTERNAL_TAIL}")], None
    created = _EXTERNAL_CREATE_RE.match(code)
    if created:
        return [_error("external_access", line, f"CREATE {' '.join(created.group('what').upper().split())} reaches "
                                                f"outside the database; {_EXTERNAL_TAIL}")], None
    if head in _SCRIPTING_HEADS:
        return [_error("statement:kind", line, f"`{_head_text(code)}` is not allowed; {_KINDS}")], None
    try:
        tree = build_parse.parse(statement)
    except SqlglotError as exc:
        return [_error("parse", _parse_error_line(statement, exc), f"not valid Snowflake SQL: {exc}")], None
    if tree is None:
        return [_error("parse", line, "no statement to parse")], None
    command = next(tree.find_all(exp.Command), None)
    if command is not None:
        shown = " ".join(build_sql.without_comments(statement.text).split())
        return [_error("parse", line, "sqlglot's Snowflake parser does not understand this statement (it fell back "
                                      f"to a raw command): {shown if len(shown) <= 120 else shown[:117] + '...'}")], None
    external = _tree_external_errors(statement, tree)
    if external:
        return external, None
    kind = _kind_error(tree, code)
    if kind:
        return [_error("statement:kind", line, f"{kind}; {_KINDS}")], None
    return [], tree


def _parse_error_line(statement: build_sql.Statement, exc: SqlglotError) -> int:
    """The file line sqlglot reports for the first problem of a parse error, else the statement's own."""
    for problem in getattr(exc, "errors", None) or []:
        if isinstance(problem, dict) and isinstance(problem.get("line"), int):
            return build_parse.first_line(statement) + problem["line"] - 1
    return statement.start_line


def _tree_external_errors(statement: build_sql.Statement, tree: exp.Expression) -> list[str]:
    """A function that reads files, settings or the network; a table that is a file or a stage."""
    errors: list[str] = []
    for node in tree.find_all(exp.Func):
        name = _function_name(node)
        if _DENIED_FUNCTION.match(name) or _SNOWFLAKE_FILE_FUNCTION.match(name):
            errors.append(_error("external_access", build_parse.line_of(statement, node),
                                 f"{name}() reads files, settings or the network; {_EXTERNAL_TAIL}"))
    for table in tree.find_all(exp.Table):
        this = table.this
        line = build_parse.line_of(statement, table)
        if isinstance(this, exp.DynamicIdentifier):
            # `IDENTIFIER('<name>')`: refused as a table by `tables:*`; one whose literal is a stage or a path also
            # reaches outside the database.
            named = this.this
            if isinstance(named, exp.Literal) and named.is_string and (named.name.startswith("@")
                                                                       or _PATH_SEPARATORS.search(named.name)):
                errors.append(_error("external_access", build_parse.line_of(statement, this),
                                     f"IDENTIFIER('{named.name}') names a stage or a file; {_EXTERNAL_TAIL}"))
        elif isinstance(this, exp.Literal) and this.is_string:
            errors.append(_error("external_access", line, f"'{this.name}' as a table reads a file; {_EXTERNAL_TAIL}"))
        elif isinstance(this, exp.Var) and str(this.name).startswith("@"):
            errors.append(_error("external_access", line, f"{this.name} is a stage; {_EXTERNAL_TAIL}"))
        elif isinstance(this, exp.Identifier) and this.quoted and _PATH_CHARACTERS.search(this.name):
            errors.append(_error("external_access", line, f'"{this.name}" as a table names a file; {_EXTERNAL_TAIL}'))
    for var in tree.find_all(exp.Var):
        if str(var.name).startswith("@") and not isinstance(var.parent, exp.Table):
            errors.append(_error("external_access", build_parse.line_of(statement, var), f"{var.name} is a stage; "
                                                                                          f"{_EXTERNAL_TAIL}"))
    return list(dict.fromkeys(errors))


def _kind_error(tree: exp.Expression, code: str) -> str | None:
    """Why `tree` is not one of the allowed statement kinds, or None when it is."""
    if isinstance(tree, exp.Create):
        what = str(tree.args.get("kind") or "").upper()
        properties = tree.args.get("properties")
        names = {type(item).__name__ for item in (properties.expressions if properties is not None else [])}
        if what != "TABLE":
            return f"CREATE {what or '...'} is not a CREATE TABLE"
        if "TransientProperty" in names:
            return "CREATE TRANSIENT TABLE is not CREATE [OR REPLACE] [TEMPORARY] TABLE"
        if not isinstance(tree.expression, exp.Query):
            return "this CREATE TABLE has no AS <query>"
        if names - {"TemporaryProperty"}:
            return "this CREATE TABLE has options beyond TEMPORARY"
        return None
    if isinstance(tree, (exp.Insert, exp.Merge)):
        return None
    if isinstance(tree, (exp.TruncateTable, exp.Delete, exp.Update)):
        targets = build_parse.write_tables(tree)
        # A table named through IDENTIFIER(...) passes here so that `tables:target` can refuse it by name.
        if targets and all(isinstance(table.this, exp.DynamicIdentifier)
                           or (table.catalog == build_parse.TGT_MARK and table.db == build_parse.TGT_MARK)
                           for table in targets):
            return None
        return f"{_head_text(code)} is allowed only on a {{{{tgt}}}}.<LOGICAL> target"
    return f"`{_head_text(code)}` is not a statement a build file holds"


# tables:source / tables:target / tables:temporary ----------------------------------------------------------------


def _table_errors(parsed: list[tuple[build_sql.Statement, exp.Expression]], inputs: set[str],
                  outputs: dict[str, TargetWrite]) -> list[str]:
    errors: list[str] = []
    written: set[str] = set()            # upper-cased target logicals some earlier statement wrote
    temporaries: set[str] = set()        # upper-cased temporary table names created so far
    logicals = inputs | set(outputs)
    for statement, tree in parsed:
        temporary_create = build_parse.is_temporary_create(tree)
        refs = build_parse.table_refs(statement, tree)
        own_writes = {ref.name.upper() for ref in refs if ref.kind == "tgt" and ref.write}
        for ref in refs:
            upper = ref.name.upper()
            if ref.kind == "cte":
                continue
            if ref.kind == "src":
                if ref.write:
                    errors.append(_error("tables:source", ref.line, f"{ref.shown} is written; a source is only read "
                                                                    f"(a write goes to {{{{tgt}}}}.<output logical>)"))
                elif upper not in inputs:
                    errors.append(_error("tables:source", ref.line, f"{ref.shown} is not an input of the objective; "
                                                                    f"expected {{{{src}}}}.<one of "
                                                                    f"{', '.join(sorted(inputs)) or 'no inputs'}>"))
            elif ref.kind == "tgt":
                if temporary_create and ref.write:
                    errors.append(_error("tables:temporary", ref.line, f"{ref.shown} is a TEMPORARY table with a "
                                                                       f"qualified name; expected a plain unqualified "
                                                                       f"name that is not a logical name"))
                elif upper not in outputs:
                    errors.append(_error("tables:target", ref.line, f"{ref.shown} is not an output of the objective; "
                                                                    f"expected {{{{tgt}}}}.<one of "
                                                                    f"{', '.join(sorted(outputs)) or 'no outputs'}>"))
                elif not ref.write and upper not in written and not _reads_its_own_write(tree, outputs[upper], upper, own_writes):
                    errors.append(_error("tables:target", ref.line, f"{ref.shown} is read before it is written; a "
                                                                    f"target may be read after the statement that "
                                                                    f"writes it (an append or update_insert target "
                                                                    f"also by its own write)"))
            elif ref.kind == "plain":
                if ref.write and not temporary_create and not isinstance(tree, (exp.Insert, exp.Merge)):
                    errors.append(_error("tables:temporary", ref.line, f"{ref.shown} is created without TEMPORARY; "
                                                                       f"a plain unqualified name is a temporary "
                                                                       f"table, made with CREATE [OR REPLACE] "
                                                                       f"TEMPORARY TABLE ... AS"))
                elif ref.write and temporary_create and upper in logicals:
                    errors.append(_error("tables:temporary", ref.line, f"{ref.shown} is a logical name of the "
                                                                       f"objective; a temporary table needs a "
                                                                       f"name that is not one"))
                elif not ref.write and upper not in temporaries:
                    errors.append(_error("tables:temporary", ref.line, f"{ref.shown} is read, but no earlier CREATE "
                                                                       f"[OR REPLACE] TEMPORARY TABLE {ref.shown} "
                                                                       f"makes it"))
                elif ref.write and not temporary_create and upper not in temporaries:
                    errors.append(_error("tables:temporary", ref.line, f"{ref.shown} is written, but no earlier "
                                                                       f"CREATE [OR REPLACE] TEMPORARY TABLE "
                                                                       f"{ref.shown} makes it"))
            elif ref.kind == "identifier":                              # the stored-procedure habit
                check = "tables:target" if ref.write else "tables:source"
                errors.append(_error(check, ref.line, f"{ref.shown} names a table without a placeholder; write "
                                                      f"{{{{src}}}}.<LOGICAL> / {{{{tgt}}}}.<LOGICAL>, not "
                                                      f"IDENTIFIER(...)"))
            else:                                                       # "other": a qualified name no placeholder gave
                check = "tables:target" if ref.write else "tables:source"
                role = "written" if ref.write else "read"
                errors.append(_error(check, ref.line, f"{ref.shown} is {role} without a placeholder; expected "
                                                      f"{{{{{'tgt' if ref.write else 'src'}}}}}.<LOGICAL>"))
        written |= own_writes
        if temporary_create:
            temporaries |= {ref.name.upper() for ref in refs if ref.kind == "plain" and ref.write}
    return errors


def _reads_its_own_write(tree: exp.Expression, target: TargetWrite, upper: str, own_writes: set[str]) -> bool:
    """An `append` or `update_insert` target may be read by the INSERT / MERGE that writes it, and any target by
    a DELETE / UPDATE of it (an Output tool's PreSQL / PostSQL, which act on a table that is already there)."""
    if upper not in own_writes:
        return False
    if isinstance(tree, (exp.Delete, exp.Update)):
        return True
    return target.mode in ("append", "update_insert") and isinstance(tree, (exp.Insert, exp.Merge))


# write_mode / write_mode:pre_post ------------------------------------------------------------------------------------
#
# Moved from the procedure form (`IDENTIFIER(:<LOGICAL>_TGT)`) to the build file's `{{tgt}}.<LOGICAL>`; the
# judgments are the same: a form is read from the text, nothing here has run on Snowflake.

_ROLE_WORD_RE = _WORD
_TABLE_MODIFIERS = frozenset({"OR", "REPLACE", "TRANSIENT", "TEMPORARY", "TEMP", "VOLATILE", "LOCAL", "GLOBAL",
                              "TABLE", "IF", "NOT", "EXISTS"})
#: `{{tgt}}.<NAME>` -- the one table reference a build file writes through.
_TGT_RE = re.compile(r"\{\{\s*tgt\s*\}\}\s*\.\s*(?P<name>" + _NAME + ")")
_AS_RE = re.compile(r"\s*AS\b", re.IGNORECASE)
_ALIAS_ONLY_RE = re.compile(r"\s*(?:AS\s+)?[A-Za-z_][A-Za-z0-9_$]*\s*", re.IGNORECASE)
_CREATE_OTHER_RE = re.compile(r"\s*(LIKE|CLONE)\b", re.IGNORECASE)
#: The token stream of a MERGE's tail: a quoted identifier, a word, or a parenthesis.
_MERGE_TOKEN_RE = re.compile(r'"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*|[()]')
#: One conjunct of a MERGE's ON clause as `write_mode` accepts it: `<alias>.<col> = <alias>.<col>`.
_KEY_EQUALITY_RE = re.compile(rf"(?P<lq>{_NAME})\s*\.\s*(?P<lc>{_NAME})\s*=\s*(?P<rq>{_NAME})\s*\.\s*(?P<rc>{_NAME})")
#: Each `WHEN [NOT] MATCHED ... THEN <action>` clause of a MERGE, in order.
_MERGE_CLAUSE_RE = re.compile(r"\bWHEN\s+(?P<matched>NOT\s+MATCHED|MATCHED)\b.*?\bTHEN\s+(?P<action>UPDATE|DELETE|INSERT)\b",
                              re.IGNORECASE | re.DOTALL)
_PLAIN_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


@dataclass(frozen=True)
class _Write:
    """One statement's write to a target: its `form` (what `write_mode` compares), `label` (how a message
    shows it), `line`, `index` (the statement's position in the file, which says whether two writes are next to
    each other), and what follows the `{{tgt}}.<NAME>` in the statement (a MERGE's ON clause): `code` with
    comments and string literals blanked, `text` with only the comments blanked -- the same offsets, so a
    clause found in the one is shown from the other."""
    form: str
    label: str
    line: int
    index: int
    code: str
    text: str


def _write_form(before: str, after: str, statement_head: str) -> tuple[str, str] | None:
    """(form, label) of the write a `{{tgt}}.<NAME>` makes after the code `before` it and before the code
    `after` it; None when it is not a write (a read is `tables:target`'s)."""
    words = [word.upper() for word in _ROLE_WORD_RE.findall(before)]
    if not words:
        return None
    last, previous = words[-1], (words[-2] if len(words) > 1 else "")
    if last == "INTO":
        known = {"INSERT": ("insert", "INSERT"), "OVERWRITE": ("insert_overwrite", "INSERT OVERWRITE"),
                 "MERGE": ("merge", "MERGE"), "COPY": ("copy", "COPY INTO")}
        return known.get(previous, ("other", f"{statement_head} ... INTO"))
    if last == "FROM":
        if previous != "DELETE":
            return None
        # Only an alias after the name still empties the whole table.
        whole = not after.strip() or _ALIAS_ONLY_RE.fullmatch(after) is not None
        return ("delete", "DELETE") if whole else ("delete_where", "DELETE ... WHERE")
    if last == "UPDATE":
        return "update", "UPDATE"
    position = len(words) - 1
    while position >= 0 and words[position] in _TABLE_MODIFIERS:
        position -= 1
    if position < 0:
        return None
    head = words[position]
    if head == "CREATE":
        as_follows = _AS_RE.match(after) is not None
        if words[position + 1:] == ["OR", "REPLACE", "TABLE"] and as_follows:
            return "ctas", "CREATE OR REPLACE TABLE ... AS"
        other = _CREATE_OTHER_RE.match(after)                  # CREATE TABLE ... LIKE / CLONE
        if other is not None:
            return "create", f"{' '.join(words[position:])} ... {other.group(1).upper()}"
        return "create", " ".join(words[position:]) + ("" if as_follows else " with no AS right after the name")
    if head in ("TRUNCATE", "DROP", "ALTER"):
        return head.lower(), head
    return None


def _target_writes_in(statements: list[build_sql.Statement], logical: str) -> list[_Write]:
    """Every write to `{{tgt}}.<logical>` in the file, in statement order."""
    writes = []
    for index, statement in enumerate(statements):
        if _is_session_statement(statement):
            continue
        code = build_sql.masked_code(statement.text)
        text = build_sql.masked_code(statement.text, strings=False)   # the same offsets, string literals kept
        head_words = _ROLE_WORD_RE.findall(code)
        head = head_words[0].upper() if head_words else ""
        for match in _TGT_RE.finditer(code):
            if _unquote_name(match.group("name")) != logical.upper():
                continue
            form = _write_form(code[:match.start()], code[match.end():], head)
            if form is not None:
                line = build_parse.first_line(statement) + code.count("\n", 0, match.start())
                writes.append(_Write(form=form[0], label=form[1], line=line, index=index, code=code[match.end():],
                                     text=text[match.end():]))
    return writes


def _required_form(target: TargetWrite) -> str:
    name = f"{{{{tgt}}}}.{target.logical}"
    return {
        "overwrite": f"CREATE OR REPLACE TABLE {name} AS ...",
        "append": f"INSERT INTO {name} (<columns>) SELECT ...",
        "truncate_append": (f"TRUNCATE TABLE {name} (or DELETE FROM it, with no WHERE) followed by "
                            f"INSERT INTO {name} (<columns>) SELECT ..."),
        "update_insert": (f"MERGE INTO {name} ... ON the objective's keys {_sql_keys(target.keys)} ... "
                          f"WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ..."),
    }[target.mode]


_WRITE_FORM = {"overwrite": "ctas", "append": "insert", "truncate_append": "insert", "update_insert": "merge"}
_CLEARS = ("truncate", "delete")


def _write_errors(statements: list[build_sql.Statement], targets: list[TargetWrite]) -> list[str]:
    """`write_mode` and `write_mode:pre_post`: each output is written through `{{tgt}}.<LOGICAL>` exactly as its
    write mode requires (`cookbook/output.md`, "Config fields that change the pattern"): overwrite is one
    `CREATE OR REPLACE TABLE ... AS`; append one `INSERT INTO`; truncate_append a `TRUNCATE`/`DELETE FROM` (no
    WHERE) of it, then one `INSERT INTO`; update_insert one `MERGE INTO` on exactly the objective's keys with
    both `WHEN MATCHED THEN UPDATE` and `WHEN NOT MATCHED THEN INSERT`. Any other statement on the target is
    the Output tool's PreSQL (before the write) or PostSQL (after it) -- allowed only when the objective's
    output has one, and required when it does. A form is judged from the text."""
    errors = []
    for target in targets:
        writes = _target_writes_in(statements, target.logical)
        head = f"{target.logical} is {target.spelling}: write it with {_required_form(target)}"
        if not writes:
            errors.append(_error("write_mode", None, f"{head}; the file never writes {{{{tgt}}}}.{target.logical}"))
            continue
        found = " + ".join(write.label if len(writes) == 1 else f"{write.label} (line {write.line})" for write in writes)
        core = [i for i, write in enumerate(writes) if write.form == _WRITE_FORM[target.mode]]
        if not core:
            errors.append(_error("write_mode", writes[0].line, f"{head}; found {found}"))
            continue
        if len(core) > 1:
            errors.append(_error("write_mode", writes[core[1]].line, f"{head} once; found {found}"))
            continue
        start = end = core[0]
        if target.mode == "truncate_append":
            clears = [i for i in range(end) if writes[i].form in _CLEARS]
            if not clears:
                errors.append(_error("write_mode", writes[end].line, f"{head}; found {found}"))
                continue
            start = clears[-1]
            if writes[start + 1:end]:
                errors.append(_error("write_mode", writes[start + 1].line, f"{head}; nothing may come between the "
                                                                          f"TRUNCATE/DELETE and the INSERT; found {found}"))
                continue
            if writes[end].index != writes[start].index + 1:
                other = statements[writes[start].index + 1]
                errors.append(_error("write_mode", other.start_line,
                                     f"{head}; the {writes[start].label} must come immediately before the INSERT, but "
                                     f"another statement (line {other.start_line}) sits between them"))
                continue
        errors += _pre_post_errors(target, writes, start, end)
        errors += _adjacency_errors(target, writes, start, end, statements)
        if target.mode == "update_insert":
            errors += _merge_errors(target, writes[end])
    return errors


def _pre_post_errors(target: TargetWrite, writes: list[_Write], start: int, end: int) -> list[str]:
    """`write_mode:pre_post`: statements on the target before/after its write are the Output tool's
    PreSQL/PostSQL -- only if the objective's output has one, and always if it does."""
    name = f"{{{{tgt}}}}.{target.logical}"
    errors = []
    if start > 0 and not target.pre_sql:
        errors.append(_error("write_mode:pre_post", writes[0].line,
                             f"{writes[0].label} on {name} comes before its write, but tool {target.tool_id} has no "
                             f"PreSQL; a statement before the write is allowed only as an Output tool's PreSQL"))
    if writes[end + 1:] and not target.post_sql:
        errors.append(_error("write_mode:pre_post", writes[end + 1].line,
                             f"{writes[end + 1].label} on {name} comes after its write, but tool {target.tool_id} "
                             f"has no PostSQL; a statement after the write is allowed only as an Output tool's PostSQL"))
    if target.pre_sql and start == 0:
        errors.append(_error("write_mode:pre_post", writes[start].line,
                             f"tool {target.tool_id} has a PreSQL: expected a statement on {name} before its write, "
                             f"and there is none"))
    if target.post_sql and not writes[end + 1:]:
        errors.append(_error("write_mode:pre_post", writes[end].line,
                             f"tool {target.tool_id} has a PostSQL: expected a statement on {name} after its write, "
                             f"and there is none"))
    return errors


def _adjacency_errors(target: TargetWrite, writes: list[_Write], start: int, end: int,
                      statements: list[build_sql.Statement]) -> list[str]:
    """`write_mode:pre_post`: a PreSQL statement comes IMMEDIATELY before the write and a PostSQL statement
    IMMEDIATELY after it -- no other statement of the file between a statement on the target and the next one
    (`start` is the first statement of the write proper: the TRUNCATE/DELETE of a truncate_append, else the
    write itself; the clear-to-INSERT gap is `write_mode`'s own check)."""
    name = f"{{{{tgt}}}}.{target.logical}"
    errors = []
    for position, (first, second) in enumerate(zip(writes, writes[1:])):
        if second.index == first.index + 1:
            continue
        sits = statements[first.index + 1]
        if position < start:                                    # `first` is a PreSQL statement, `second` follows it
            errors.append(_error("write_mode:pre_post", first.line,
                                 f"{first.label} on {name} is tool {target.tool_id}'s PreSQL and must come immediately "
                                 f"before its write, but another statement (line {sits.start_line}) sits between them"))
        else:
            errors.append(_error("write_mode:pre_post", second.line,
                                 f"{second.label} on {name} is tool {target.tool_id}'s PostSQL and must come immediately "
                                 f"after its write, but another statement (line {sits.start_line}) sits between them"))
    return errors


def _merge_errors(target: TargetWrite, merge: _Write) -> list[str]:
    """The MERGE matches on exactly the objective's keys -- `<a>.<K> = <b>.<K>` per key, two different
    aliases, joined by AND and nothing else (a NULL key then never matches, which is Alteryx's rule too) --
    and has both an update of matched rows and an insert of the rest."""
    name = f"{{{{tgt}}}}.{target.logical}"
    errors = []
    span = _merge_on_clause(merge.code)
    keys = _merge_keys(merge.code[span[0]:span[1]]) if span is not None else None
    if keys is None or set(keys) != {key.upper() for key in target.keys}:
        shown = " ".join(merge.text[span[0]:span[1]].split()) if span is not None else "(no ON clause found)"
        errors.append(_error("write_mode", merge.line,
                             f"{target.logical} is {target.spelling}: the MERGE INTO {name} must match on exactly the "
                             f"objective's keys {_sql_keys(target.keys)} -- ON <target>.<KEY> = <source>.<KEY> for "
                             f"each key, joined by AND, nothing else -- not ON {shown}"))
    clauses = {(" ".join(m.group("matched").upper().split()), m.group("action").upper())
               for m in _MERGE_CLAUSE_RE.finditer(merge.code)}
    if not {("MATCHED", "UPDATE"), ("NOT MATCHED", "INSERT")} <= clauses:
        errors.append(_error("write_mode", merge.line,
                             f"{target.logical} is {target.spelling}: the MERGE INTO {name} needs both WHEN MATCHED "
                             f"THEN UPDATE and WHEN NOT MATCHED THEN INSERT (Update; Insert if new)"))
    return errors


def _merge_on_clause(after: str) -> tuple[int, int] | None:
    """The (start, end) offsets of a MERGE's ON clause in `after`: from the first top-level ON after its
    USING to the first top-level WHEN. None when there is no such ON."""
    depth, using, start = 0, False, None
    for match in _MERGE_TOKEN_RE.finditer(after):
        token = match.group(0)
        if token in "()":
            depth += 1 if token == "(" else -1
            continue
        if depth:
            continue
        word = token.upper()
        if start is None:
            if word == "USING":
                using = True
            elif word == "ON" and using:
                start = match.end()
        elif word == "WHEN":
            return start, match.start()
    return (start, len(after)) if start is not None else None


def _merge_keys(on_clause: str) -> list[str] | None:
    """The key columns an ON clause equates, one `<a>.<K> = <b>.<K>` per top-level AND; None as soon as a
    conjunct is anything else (an OR, a function, a literal, one alias on both sides)."""
    conjuncts, depth, start = [], 0, 0
    for match in _MERGE_TOKEN_RE.finditer(on_clause):
        token = match.group(0)
        if token in "()":
            depth += 1 if token == "(" else -1
        elif depth == 0 and token.upper() == "AND":
            conjuncts.append(on_clause[start:match.start()])
            start = match.end()
    conjuncts.append(on_clause[start:])
    keys = []
    for conjunct in conjuncts:
        text = conjunct.strip()
        while text.startswith("(") and text.endswith(")") and _balanced(text[1:-1]):
            text = text[1:-1].strip()
        match = _KEY_EQUALITY_RE.fullmatch(text)
        if match is None:
            return None
        left, right = _unquote_name(match.group("lc")), _unquote_name(match.group("rc"))
        if _unquote_name(match.group("lq")) == _unquote_name(match.group("rq")) or left != right:
            return None
        keys.append(left)
    return keys


def _balanced(text: str) -> bool:
    depth = 0
    for char in text:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            return False
    return depth == 0


def _unquote_name(name: str) -> str:
    """A name as the key check compares it: unquoted, upper-cased. Quoted or not, and in any case, because
    objective keys are Alteryx field names verbatim and every other stage compares keys case-insensitively
    (`compare.py`)."""
    return (name[1:-1].replace('""', '"') if name.startswith('"') else name).upper()


def _sql_keys(keys) -> str:
    """The keys as a MERGE has to spell them: a plain identifier as is, anything else (a space, a symbol)
    double-quoted, so the message names a spelling that compiles."""
    return ", ".join(key if _PLAIN_NAME_RE.fullmatch(key) else '"' + key.replace('"', '""') + '"' for key in keys)


# outputs:columns, static half: the columns an INSERT / MERGE names -------------------------------------------------------
#
# The run cannot see which columns an `append` / `truncate_append` / `update_insert` write names: the target exists
# beforehand with the objective's columns, so a write of the wrong ones still leaves "the right columns". The
# statement's own column list is what is held to the objective instead.


def _column_list_errors(parsed: list[tuple[build_sql.Statement, exp.Expression]], objective: dict,
                        targets: list[TargetWrite]) -> list[str]:
    """`outputs:columns`: an INSERT into an append / truncate_append target, and the `WHEN NOT MATCHED THEN
    INSERT` of a MERGE into an update_insert target, name exactly the objective output's columns in the
    objective's order (compared case-insensitively); with no column list at all the statement is refused and the
    columns named. The message names the missing, extra and out-of-order columns."""
    modes = {target.logical.upper(): target.mode for target in targets}
    expected = {str(output.get("logical")).upper(): [str(column["name"]) for column in output.get("columns") or []]
                for output in objective.get("outputs") or []}
    errors: list[str] = []
    for statement, tree in parsed:
        for table in build_parse.write_tables(tree):
            if table.catalog != build_parse.TGT_MARK or table.db != build_parse.TGT_MARK:
                continue
            logical = table.name.upper()
            name = f"{{{{tgt}}}}.{table.name}"
            if isinstance(tree, exp.Insert) and modes.get(logical) in ("append", "truncate_append"):
                listed = [column.name for column in tree.this.expressions] if isinstance(tree.this, exp.Schema) else None
                errors += _compare_columns(f"INSERT INTO {name}", listed, expected.get(logical, []),
                                           build_parse.line_of(statement, listed and tree.this or table.this))
            elif isinstance(tree, exp.Merge) and modes.get(logical) == "update_insert":
                for when in (tree.args.get("whens") or exp.Whens()).expressions:
                    then = when.args.get("then")
                    if when.args.get("matched") or not isinstance(then, exp.Insert):
                        continue
                    listed = [column.name for column in then.this.expressions] if isinstance(then.this, exp.Tuple) else None
                    errors += _compare_columns(f"the WHEN NOT MATCHED THEN INSERT of MERGE INTO {name}", listed,
                                               expected.get(logical, []),
                                               build_parse.line_of(statement, then.this or table.this))
    return errors


def _compare_columns(what: str, listed: list[str] | None, expected: list[str], line: int) -> list[str]:
    """One `outputs:columns` error (or none) for the column list of `what` against the objective's."""
    shown = _listed([name.upper() for name in expected])
    if listed is None:
        return [_error("outputs:columns", line, f"{what} has no column list; name the objective's columns {shown}, in "
                                                f"that order")]
    have, want = [name.upper() for name in listed], [name.upper() for name in expected]
    if have == want:
        return []
    missing = [name for name in want if name not in have]
    extra = [name for name in dict.fromkeys(have) if name not in want]
    problems = []
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    if extra:
        problems.append(f"extra {', '.join(extra)}")
    if len(set(have)) != len(have):
        problems.append(f"repeated {', '.join(name for name in dict.fromkeys(have) if have.count(name) > 1)}")
    if not problems:
        problems.append("out of order")
    return [_error("outputs:columns", line, f"{what} lists {_listed(have)} but the objective lists {shown}; "
                                            f"{'; '.join(problems)} (expected the same names in the same order)")]


# run / outputs:columns ---------------------------------------------------------------------------------------------


def _create_empty(backend: DuckDBBackend, fqn: str, columns: list[dict]) -> None:
    if not columns:
        raise ValueError("the objective lists no columns for it")
    declarations = ", ".join(f'"{column["name"]}" {column["type"]}' for column in columns)
    backend.execute(f"CREATE OR REPLACE TABLE {fqn} ({declarations})")


def _fixtures(sources: list[dict], objective: dict, targets: list[TargetWrite]):
    """(fqn, columns) for every table the build expects to find already there: each input, and each output
    an append / truncate_append / update_insert write extends, or whose Output tool has a PreSQL (which acts on
    the table before an overwrite replaces it). A plain overwrite creates its own."""
    for source in sources:
        yield f"{RUN_VALUES['src']}.{source['logical']}", source.get("columns") or []
    by_logical = {str(output.get("logical")): output for output in objective.get("outputs") or []}
    for target in targets:
        if target.mode != "overwrite" or target.pre_sql:
            yield f"{RUN_VALUES['tgt']}.{target.logical}", by_logical[target.logical].get("columns") or []


def _run_errors(statements: list[build_sql.Statement], sources: list[dict], targets: list[TargetWrite],
                objective: dict) -> list[str]:
    """Runs every statement on an empty DuckDB sandbox, then compares each output's columns with the
    objective's. An `ALTER SESSION` is recorded, not applied; the double has no external access."""
    errors: list[str] = []
    backend = DuckDBBackend()
    try:
        for fqn, columns in _fixtures(sources, objective, targets):
            try:
                _create_empty(backend, fqn, columns)
            except (BackendError, ValueError) as exc:
                errors.append(_error("run", None, f"cannot build an empty table {fqn} from the objective: {exc}"))
        if errors:
            return errors
        backend.lock_external_access()
        for statement in statements:
            if _is_session_statement(statement):
                continue
            try:
                backend.execute(build_sql.render(statement.text, RUN_VALUES))
            except (BackendError, ValueError) as exc:
                return [_error("run", statement.start_line, f"the statement fails on an empty sandbox: "
                                                           f"{str(exc).splitlines()[0] if str(exc) else exc}")]
        for output in objective.get("outputs") or []:
            errors += _output_columns_errors(backend, f"{RUN_VALUES['tgt']}.{output['logical']}", output)
    finally:
        backend.close()
    return errors


def _output_columns_errors(backend: DuckDBBackend, fqn: str, output: dict) -> list[str]:
    expected = [str(column["name"]).upper() for column in output.get("columns") or []]
    if not backend.table_exists(fqn):
        return [_error("outputs:columns", None, f"{output['logical']}: the build did not create {{{{tgt}}}}.{output['logical']}; "
                                                f"expected a table with columns {_listed(expected)}")]
    actual = [column["name"].upper() for column in backend.table_columns(fqn)]
    if actual != expected:
        return [_error("outputs:columns", None, f"{output['logical']} has columns {_listed(actual)} but the objective "
                                                f"lists {_listed(expected)}; expected the same names in the same order")]
    return []


def _listed(names: list[str]) -> str:
    return "[" + ", ".join(names) + "]"


# --- build.py -----------------------------------------------------------------------------------------------------


def _check_python(text: str, objective: dict, targets: list[TargetWrite], sources: list[dict]) -> dict:
    """The Snowpark static rules, the step markers, no `$$`, then a run of `main` on empty inputs."""
    errors = [_error("python:dollar_quote", text.count("\n", 0, match.start()) + 1,
                     "build.py contains $$; deploy.py wraps the module in $$ ... $$, so it may not appear in it")
              for match in re.finditer(r"\$\$", text)][:1]
    rules = snowpark_rules.check_build_py(text, objective)
    errors += rules
    errors += step_errors(text, "#", objective)
    if not errors:
        errors += _python_run_errors(text, objective, targets, sources)
    return {"ok": not errors, "errors": errors}


def _python_run_errors(text: str, objective: dict, targets: list[TargetWrite], sources: list[dict]) -> list[str]:
    """Runs `main(session, src, tgt)` in the runtime sandbox on empty input tables and compares each output's
    columns with the objective's. A refusal, a crash or a timeout is `run`; there is no local line to name."""
    from lib import snowpark_sandbox  # noqa: PLC0415  (lazy: the sandbox child is a separate process)
    outputs = objective.get("outputs") or []
    tables = [{"fqn": fqn, "columns": columns} for fqn, columns in _fixtures(sources, objective, targets)]

    def run(null_row: bool):
        return snowpark_sandbox.run_build(source=text, display_path="build.py", src=RUN_VALUES["src"],
                                          tgt=RUN_VALUES["tgt"], tables=tables, null_row=null_row,
                                          outputs=[f"{RUN_VALUES['tgt']}.{output['logical']}" for output in outputs])

    result = run(null_row=False)
    if result.kind == "run":
        # The Local Testing Framework cannot `filter` an empty table, so a build that raises on empty inputs is
        # run once more on one row of NULLs; whatever that run says is the answer. Whether the build copes with
        # genuinely empty data is `validate_build.py`'s question, on the golden `empty` set.
        result = run(null_row=True)
    if result.kind == "usage":
        raise UsageError(result.error or "the sandbox could not prepare its empty tables")
    if result.kind != "ok":
        return [_error("run", None, f"build.py did not run on empty inputs ({result.kind}): {result.error}")]
    errors = []
    for output in outputs:
        table = result.outputs.get(f"{RUN_VALUES['tgt']}.{output['logical']}")
        expected = [str(column["name"]).upper() for column in output.get("columns") or []]
        if table is None:
            errors.append(_error("outputs:columns", None, f"{output['logical']}: main did not create "
                                                          f"{output['logical']}; expected a table with columns "
                                                          f"{_listed(expected)}"))
            continue
        actual = [str(field["name"]).upper() for field in table["fields"]]
        if actual != expected:
            errors.append(_error("outputs:columns", None, f"{output['logical']} has columns {_listed(actual)} but "
                                                          f"the objective lists {_listed(expected)}; expected the "
                                                          f"same names in the same order"))
    return errors


# --- the command line ----------------------------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id", help="workflow id, e.g. wf_0003")
    add_root_arg(parser)
    args = parser.parse_args(argv)

    # Not parser.error(): main() is called in-process by callers that expect an int back.
    try:
        report = compile_check(Repo(args.root), args.wf_id)
    except (FileNotFoundError, UsageError, ContractError) as exc:
        print(f"cannot compile-check {args.wf_id}: {exc}", file=sys.stderr)
        return 2
    except Exception:  # exit 2: a crash outside the checks above, which leaves no report either
        traceback.print_exc()
        return 2
    flags = len((report.get("lint") or {}).get("flags") or [])
    print(f"{'OK' if report['ok'] else 'ERROR'}: {len(report['errors'])} errors, {flags} lint flag{'' if flags == 1 else 's'}")
    for error in report["errors"]:
        print(f"  {error}", file=sys.stderr)
    for flag in (report.get("lint") or {}).get("flags") or []:
        print(f"  lint {flag['kind']} {flag['name']} (line {flag['line']}): {flag['note']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
