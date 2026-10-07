"""Export a workflow's validated `build.sql` as a small dbt project (objective-first design D9, §4.10).

    python scripts/export_dbt.py <wf_id> [--root .]

The translation is written once, as minimal SQL (`build.sql`); the dbt project is its conversion, done by
this script and never by an agent, and `validate_dbt.py` runs the result against the same golden data. The
project lands in `workflows/<wf_id>/dbt/`:

    dbt_project.yml        name mig_<wf>, model-paths [models]; the build's ALTER SESSION, if any, as a
                           project-level +pre-hook of the models, Snowflake only
    profiles.yml           a local DuckDB target (what validate_dbt.py runs on); a COMMENTED Snowflake target
    README.md              what each model is, how to run it, that nothing has run on Snowflake
    models/sources.yml     source "<wf>": one table per objective input, schema from var('src_schema')
    models/schema.yml      one entry per output model: its columns (the objective's meaning), not_null for
                           every NOT NULL column, unique for a one-column key -- both `severity: warn`, as
                           compare.py's `nullability_contract` / `keys_not_unique` are advisories, never failures
    models/<logical>.sql   one per output
    models/<temp>.sql      one per temporary table, materialized ephemeral

**Conversion, statement by statement** (the forms `compile_check.py` already enforces; each model body keeps the
build's own SQL text, with the substitutions below, so it reads like the build file):

| `build.sql`                                                   | model                                              |
|---------------------------------------------------------------|----------------------------------------------------|
| `CREATE OR REPLACE TEMPORARY TABLE t AS <query>`              | ephemeral, body `<query>`                          |
| `CREATE OR REPLACE TABLE {{tgt}}.X AS <query>` (overwrite)    | `table`, `alias='X'`                               |
| `INSERT INTO {{tgt}}.X (cols) <query>` (append)               | `incremental`, strategy `append`, `full_refresh=false` |
| `TRUNCATE TABLE {{tgt}}.X` + `INSERT INTO` (truncate_append)  | `table` (it is replaced on every run)              |
| `MERGE INTO {{tgt}}.X USING (<query>) s ON <keys> ...`        | `incremental`, strategy `merge`, `unique_key` the  |
|   (update_insert)                                             | objective's keys, `merge_update_columns` the SET,  |
|                                                               | `full_refresh=false`                               |
| PreSQL / PostSQL statements on an output                      | that model's `pre_hook` / `post_hook`              |

In every body `{{src}}.X` becomes `{{ source('<wf>', 'X') }}`, a temporary table `t` becomes `{{ ref('t') }}`,
a target `{{tgt}}.Y` read after the statement that writes it becomes `{{ ref('y') }}` (its own model's table is
`{{ this }}`). The step markers a statement sits under are kept as one `-- step s1, s3` comment at the top of its
model, followed by the build's own comments on that statement. Two places need more than a prefix cut, both
because dbt, not the build, writes the rows:

* an `INSERT INTO t (a, b) <query>` is positional, but dbt inserts by NAME from the model's result, so when the
  query's own output names are not exactly the INSERT's column list the body is `WITH q (a, b) AS (<query>)
  SELECT * FROM q`;
* dbt's `merge` strategy inserts EVERY column of the table and updates only `merge_update_columns`. A column the
  MERGE's `WHEN NOT MATCHED THEN INSERT` fills with something other than `S.<column>` (a constant such as the
  `NULL` of a column a PostSQL fills later) is therefore projected by the model: `SELECT S.*, NULL AS X FROM
  (<query>) AS S`; and `UPDATE SET` may only copy columns of the source, the one thing `merge_update_columns`
  says. When the USING source is a table, the body is `SELECT * FROM {{ ref('t') }}`.

An incremental model (append and merge) carries `full_refresh=false`: the table it writes holds history the build
relies on, so `dbt build --full-refresh` must never rebuild it from this run's rows alone.

The `ALTER SESSION` of the build becomes a project-level `+pre-hook` on the models in `dbt_project.yml`, guarded
`{% if target.type == 'snowflake' %}...{% endif %}` so that it renders to nothing on the local DuckDB double (which
has no sessions: `validate_build.py` records the statement, it does not apply it). It is NOT an `on-run-start`: dbt
runs the start hooks on its own "master" connection and every model on a worker-thread connection that is opened
and released separately, `threads: 1` or not, so a session parameter set there never reaches a model. A pre-hook
runs on the model's own connection, before its SQL. A model's own PreSQL `pre_hook` runs AFTER it: dbt appends the
list of a model's `config()` to the list of the project (`dbt/context/context_config.py`,
`calculate_node_config`: the project's configs are applied first, the model's `config()` call after;
`dbt_common/contracts/config/base.py`, `_merge_field_value`, `MergeBehavior.Append`: `self + other`).

**Exit codes.** 0: the project was written, or the export was skipped (a line says why, nothing is written and any
old `dbt/` is removed) -- `program.dbt: false` in `mappings/global.yaml`, the objective's language is `python` (a
Python workflow stays a Snowpark procedure), or its tier is `T3` (not translated: there is no build file). 1: a
statement with no dbt form, named by its line (`build.sql line 14: no dbt form for ...`); any old `dbt/` is removed,
so a project that does not match the build is never left behind. 2: a usage error (no objective, no build file, both
build files, an objective that cannot say what the export needs, bad arguments); an old `dbt/` is left where it is,
because nothing was learned about the build.

The output is deterministic: a re-run on the same inputs is byte-identical. Nothing here has run against a real
Snowflake account; passing `validate_dbt.py` means dbt-duckdb agreed with the golden data.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import textwrap
import traceback
from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import SqlglotError

from lib import build_parse, build_sql, dbt_project, objective as objective_lib
from lib.io import read_yaml, write_text
from lib.paths import Repo, add_root_arg
from lib.write_modes import ContractError, TargetWrite, objective_targets

#: What `{{src}}` and `{{tgt}}` look like in the text sqlglot parsed (`lib.build_parse`).
_SRC_SENTINEL = f"{build_parse.SRC_MARK}.{build_parse.SRC_MARK}"
_TGT_SENTINEL = f"{build_parse.TGT_MARK}.{build_parse.TGT_MARK}"

_NAME = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")'
_PATH = rf"{_NAME}(?:\s*\.\s*{_NAME}){{0,2}}"
_CREATE_PREFIX = re.compile(
    rf"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:LOCAL\s+|GLOBAL\s+)?(?:TEMP|TEMPORARY|VOLATILE)\s+)?TABLE\s+{_PATH}\s+AS\s+",
    re.IGNORECASE)
_INSERT_PREFIX = re.compile(rf"\s*INSERT\s+INTO\s+{_PATH}\s*", re.IGNORECASE)
_INSERT_COLUMNS = re.compile(r"\([^()]*\)\s*")
_MERGE_TOKEN = re.compile(r'"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*|[()]')
_MARKER_LINE = re.compile(r"--\s*step\s+s[1-9][0-9]*(?:\s*,\s*s[1-9][0-9]*)*\s*", re.IGNORECASE)
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_MODEL_NAME = re.compile(r"[a-z_][a-z0-9_]*")


class NoDbtForm(Exception):
    """A statement of the build the conversion table has no row for (exit 1). `line` is the build file line."""

    def __init__(self, line: int | None, what: str):
        self.line = line
        self.what = what
        super().__init__(f"build.sql line {line}: no dbt form for {what}" if line else f"build.sql: no dbt form for {what}")


class UsageError(Exception):
    """There is nothing to export from, or the inputs cannot say what the export needs (exit 2)."""


# --- the model, as the conversion builds it --------------------------------------------------------------------


@dataclass
class Model:
    """One dbt model: a temporary table (`ephemeral`) or an output (`table`, or `incremental` with a strategy)."""
    name: str                                   # lower-case, the file stem and the `ref()` name
    materialized: str                           # ephemeral | table | incremental
    strategy: str | None = None                 # append | merge (incremental only)
    alias: str | None = None                    # an output's exact logical name
    unique_key: list[str] = field(default_factory=list)
    update_columns: list[str] = field(default_factory=list)
    pre_hooks: list[str] = field(default_factory=list)
    post_hooks: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    body: str = ""
    first_line: int = 0
    last_line: int = 0
    logical: str | None = None                  # an output's logical name (None for a temporary table)
    mode: str | None = None                     # an output's write mode


@dataclass
class Piece:
    """One statement of the build, parsed and classified."""
    index: int
    statement: build_sql.Statement
    tree: exp.Expression
    kind: str                                   # temp | ctas | insert | merge | clear | dml
    logical: str | None = None                  # upper-cased logical name of the target it writes
    temp: str | None = None                     # the temporary table's name


@dataclass
class Export:
    """What an export produced. `skipped` is the reason when nothing was written."""
    wf_id: str
    skipped: str | None = None
    files: dict[str, str] = field(default_factory=dict)       # path under dbt/ -> text
    models: list[Model] = field(default_factory=list)


# --- small text helpers ------------------------------------------------------------------------------------------


def _jinja_string(text: str) -> str:
    """`text` as a Jinja string literal: single-quoted when it can be, else double-quoted with the escapes
    a Jinja (and Python) string takes. `{{ this }}` inside it is left for dbt to render when the hook runs."""
    if "'" not in text and "\\" not in text and "\n" not in text and "\r" not in text:
        return f"'{text}'"
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def _jinja_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_jinja_value(item) for item in value) + "]"
    return _jinja_string(str(value))


def _yaml(text: str) -> str:
    """A YAML double-quoted scalar (a JSON string is one)."""
    return json.dumps(text, ensure_ascii=False)


def _ident(name: str) -> str:
    """A column name as SQL spells it: bare when it is a plain identifier, else double-quoted."""
    return name if _PLAIN.fullmatch(name) else '"' + name.replace('"', '""') + '"'


def _neutral_comment(text: str) -> str:
    """A comment as a model carries it: the build's `{{src}}`/`{{tgt}}` spelled `<src>`/`<tgt>`, and nothing a
    Jinja lexer would take for a tag (dbt renders the whole model file, comments included)."""
    text = text.replace(_SRC_SENTINEL, "<src>").replace(_TGT_SENTINEL, "<tgt>")
    text = re.sub(r"\{\{\s*src\s*\}\}", "<src>", text)
    text = re.sub(r"\{\{\s*tgt\s*\}\}", "<tgt>", text)
    return text.replace("{{", "{ {").replace("{%", "{ %").replace("{#", "{ #")


def _neutral_code(text: str, line: int) -> str:
    """Code (or a string literal) as a model carries it: a placeholder that is not the start of a table name,
    or a stray `{{`, has no dbt form; `{%` and `{#` are spelled so Jinja leaves them alone."""
    if _SRC_SENTINEL in text or _TGT_SENTINEL in text:
        raise NoDbtForm(line, "a {{src}} or {{tgt}} placeholder that is not the start of a table name")
    if "{{" in text:
        raise NoDbtForm(line, "a '{{' that is not {{src}} or {{tgt}}")
    return text.replace("{%", "{{ '{' }}%").replace("{#", "{{ '{' }}#")


def _neutral(text: str, line: int) -> str:
    """`text` (a piece of the sentinel text between two table names) made safe for a model file."""
    out = []
    for kind, start, end in build_sql.spans(text):
        chunk = text[start:end]
        out.append(_neutral_comment(chunk) if kind == "comment" else _neutral_code(chunk, line))
    return "".join(out)


def _one_line(text: str) -> str:
    """A statement as one line: comments dropped, whitespace runs outside string literals collapsed."""
    out = []
    for kind, start, end in build_sql.spans(text):
        chunk = text[start:end]
        if kind == "comment":
            out.append(" ")
        elif kind == "code":
            out.append(re.sub(r"\s+", " ", chunk))
        else:
            out.append(chunk)
    return "".join(out).strip()


# --- skipping ----------------------------------------------------------------------------------------------------


def _dbt_enabled(repo: Repo) -> bool:
    """`mappings/global.yaml`'s `program.dbt`: true when the file or the key is absent (the export is on by
    default), else the value, which must be true or false."""
    path = repo.global_mappings
    if not path.is_file():
        return True
    program = (read_yaml(path) or {}).get("program") or {}
    value = program.get("dbt", True)
    if not isinstance(value, bool):
        raise ValueError(f"{path}: program.dbt is {value!r}; expected true or false")
    return value


def remove_export(repo: Repo, wf_id: str) -> None:
    """Deletes `workflows/<wf_id>/dbt/` and the validation sandboxes beside it: a workflow whose project is not
    (or no longer) exported has none, so a stale one is never mistaken for the real thing."""
    shutil.rmtree(repo.wf(wf_id, "dbt"), ignore_errors=True)
    for stale in repo.wf(wf_id).glob("dbt_sandbox_*.duckdb*"):
        stale.unlink(missing_ok=True)


# --- reading the statements -------------------------------------------------------------------------------------


def _is_session(statement: build_sql.Statement) -> bool:
    words = [word.upper() for word in re.findall(r"[A-Za-z_]+", build_sql.masked_code(statement.text))[:2]]
    return words == ["ALTER", "SESSION"]


def _is_tgt(table: exp.Table) -> bool:
    return table.catalog == build_parse.TGT_MARK and table.db == build_parse.TGT_MARK


def _shown(statement: build_sql.Statement) -> str:
    """The statement as one line of code, comments dropped, cut to 80 characters (for a message)."""
    code = " ".join(build_sql.without_comments(statement.text).split())
    return code if len(code) <= 80 else code[:77] + "..."


def _classify(index: int, statement: build_sql.Statement, tree: exp.Expression) -> Piece:
    """Which row of the conversion table a statement is, or `NoDbtForm` naming its line."""
    line = statement.start_line
    shown = _shown(statement)
    tables = build_parse.write_tables(tree)
    if isinstance(tree, exp.Create):
        if str(tree.args.get("kind") or "").upper() != "TABLE" or not isinstance(tree.expression, exp.Query):
            raise NoDbtForm(line, f"`{shown}`; expected CREATE [OR REPLACE] [TEMPORARY] TABLE <name> AS <query>")
        if build_parse.is_temporary_create(tree):
            table = tables[0] if tables else None
            if table is None or table.catalog or table.db or not isinstance(table.this, exp.Identifier):
                raise NoDbtForm(line, f"`{shown}`; a temporary table has a plain unqualified name")
            return Piece(index, statement, tree, "temp", temp=table.name)
        if tables and _is_tgt(tables[0]):
            return Piece(index, statement, tree, "ctas", logical=tables[0].name.upper())
        raise NoDbtForm(line, f"`{shown}`; a table is created as {{{{tgt}}}}.<LOGICAL> or as a TEMPORARY table")
    if isinstance(tree, (exp.Insert, exp.Merge)):
        kind = "insert" if isinstance(tree, exp.Insert) else "merge"
        if not tables or not _is_tgt(tables[0]):
            raise NoDbtForm(line, f"`{shown}`; {kind.upper()} writes {{{{tgt}}}}.<LOGICAL>, not a temporary table")
        if isinstance(tree, exp.Insert) and not isinstance(tree.expression, exp.Query):
            raise NoDbtForm(line, f"`{shown}`; INSERT takes a query (an INSERT ... VALUES has no model body)")
        return Piece(index, statement, tree, kind, logical=tables[0].name.upper())
    if isinstance(tree, (exp.TruncateTable, exp.Delete, exp.Update)):
        if len(tables) != 1 or not _is_tgt(tables[0]):
            raise NoDbtForm(line, f"`{shown}`; allowed only on one {{{{tgt}}}}.<LOGICAL> target")
        clears = isinstance(tree, exp.TruncateTable) or (isinstance(tree, exp.Delete) and tree.args.get("where") is None)
        return Piece(index, statement, tree, "clear" if clears else "dml", logical=tables[0].name.upper())
    raise NoDbtForm(line, f"`{shown}`")


def _leading_comments(statement: build_sql.Statement) -> list[str]:
    """The comment lines directly above a statement (after the last blank line), step markers left out -- the
    model carries its own, computed from the regions."""
    masked = build_sql.masked_code(statement.text)
    head = statement.text[: len(masked) - len(masked.lstrip())]
    lines = [line.rstrip() for line in head.splitlines()]
    block: list[str] = []
    for line in reversed(lines):
        if not line.strip():
            break
        block.append(line.strip())
    return [_neutral_comment(line) for line in reversed(block) if not _MARKER_LINE.fullmatch(line)]


def _steps_of(regions: list[build_sql.Region], statement: build_sql.Statement) -> list[str]:
    for region in regions:
        if region.start_line <= statement.start_line <= region.end_line:
            return list(region.steps)
    return []


# --- the table references of one statement ------------------------------------------------------------------------


class _Names:
    """What a table name in a statement can mean, as the walk has seen it so far."""

    def __init__(self, wf_id: str, inputs: dict[str, str], outputs: dict[str, TargetWrite]):
        self.wf_id = wf_id
        self.inputs = inputs                    # UPPER -> the logical name as the objective spells it
        self.outputs = outputs                  # UPPER -> TargetWrite
        self.temps: dict[str, str] = {}         # UPPER -> model name, for the temporary tables created so far
        self.written: set[str] = set()          # UPPER logical names whose model the walk has built

    def replacement(self, statement: build_sql.Statement, table: exp.Table, ctes: set[str], own: str | None,
                    hook: bool = False) -> str | None:
        """The text that stands for `table` in a model, None to leave it (a CTE of the statement itself or a
        table function). `own` is the output this statement writes (its `{{ this }}`). `hook` is true for a PreSQL /
        PostSQL statement, which becomes a `pre_hook` / `post_hook`: dbt injects an ephemeral model's CTE into a
        model's SQL, never into a hook, so a hook that reads a temporary table has no dbt form."""
        line = build_parse.line_of(statement, table.this)
        if isinstance(table.this, exp.DynamicIdentifier):
            raise NoDbtForm(line, "a table named through IDENTIFIER(...); a build file names a table only through a placeholder")
        if not isinstance(table.this, exp.Identifier):
            return None
        name, upper = table.name, table.name.upper()
        if not table.db and not table.catalog:
            if upper in ctes:
                return None
            if upper in self.temps:
                if hook:
                    raise NoDbtForm(line, f"{name}, a temporary table read by a PreSQL/PostSQL statement (it becomes a "
                                          f"hook, and dbt gives a hook no CTE for an ephemeral model)")
                return f"{{{{ ref({_jinja_string(self.temps[upper])}) }}}}"
            raise NoDbtForm(line, f"{name}, read before any CREATE TEMPORARY TABLE {name} makes it")
        if table.catalog == build_parse.SRC_MARK and table.db == build_parse.SRC_MARK:
            if upper not in self.inputs:
                raise NoDbtForm(line, f"{{{{src}}}}.{name}, which is not an input of objective.json")
            return f"{{{{ source({_jinja_string(self.wf_id)}, {_jinja_string(self.inputs[upper])}) }}}}"
        if _is_tgt(table):
            if upper not in self.outputs:
                raise NoDbtForm(line, f"{{{{tgt}}}}.{name}, which is not an output of objective.json")
            if upper == own:
                return "{{ this }}"
            if upper in self.written:
                return f"{{{{ ref({_jinja_string(self.outputs[upper].logical.lower())}) }}}}"
            raise NoDbtForm(line, f"{{{{tgt}}}}.{name}, read before the statement that writes it")
        shown = ".".join(part for part in (table.catalog, table.db, name) if part)
        raise NoDbtForm(line, f"{shown}, a table named without {{{{src}}}} or {{{{tgt}}}}")


def _whole(piece: Piece) -> int:
    """The length of the statement's sentinel text: the end of a region that runs to the end of the statement."""
    return len(build_parse.sentinel(piece.statement.text))


def _body(names: _Names, piece: Piece, node: exp.Expression, lo: int, own: str | None, hi: int | None = None) -> str:
    """A model body: the statement's text from `lo` (to `hi`, default its end) with the substitutions, taken
    out of the indentation of the statement it sat in."""
    text = _rewrite(names, piece, node, lo, _whole(piece) if hi is None else hi, own)
    return textwrap.dedent(text).strip()


def _rewrite(names: _Names, piece: Piece, node: exp.Expression, lo: int, hi: int, own: str | None,
             hook: bool = False) -> str:
    """The text `[lo, hi)` of the statement's sentinel text, with every table reference under `node` replaced
    by its dbt form and everything else made safe for a model file. Offsets are those of the text sqlglot
    parsed (`build_parse.sentinel`). `hook`: the statement is a PreSQL / PostSQL (`_Names.replacement`)."""
    statement = piece.statement
    text = build_parse.sentinel(statement.text)
    ctes = {cte.alias.upper() for cte in piece.tree.find_all(exp.CTE)}
    edits: list[tuple[int, int, str]] = []
    for table in node.find_all(exp.Table):
        replacement = names.replacement(statement, table, ctes, own, hook)
        if replacement is None:
            continue
        parts = [part for part in (table.args.get("catalog"), table.args.get("db"), table.this) if part is not None]
        start, end = parts[0].meta.get("start"), parts[-1].meta.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise NoDbtForm(build_parse.line_of(statement, table.this), f"the table reference {table.sql(dialect='snowflake')} (sqlglot gave no position for it)")
        edits.append((start, end + 1, replacement))
    out, cursor, line = [], lo, statement.start_line
    for start, end, replacement in sorted(edits):
        if start < lo or end > hi:
            continue
        out.append(_neutral(text[cursor:start], line))
        out.append(replacement)
        cursor = end
    out.append(_neutral(text[cursor:hi], line))
    return "".join(out)


# --- the query of a statement ------------------------------------------------------------------------------------


def _masked(statement: build_sql.Statement) -> str:
    return build_sql.masked_code(build_parse.sentinel(statement.text))


def _query_start(piece: Piece) -> int:
    """The offset, in the sentinel text, where a CREATE's or INSERT's query begins."""
    masked = _masked(piece.statement)
    if piece.kind in ("temp", "ctas"):
        match = _CREATE_PREFIX.match(masked)
        if match is None:
            raise NoDbtForm(piece.statement.start_line, "this CREATE; expected CREATE [OR REPLACE] [TEMPORARY] TABLE <name> AS <query>")
        return match.end()
    match = _INSERT_PREFIX.match(masked)
    if match is None:
        raise NoDbtForm(piece.statement.start_line, "this INSERT; expected INSERT INTO {{tgt}}.<LOGICAL> (<columns>) <query>")
    end = match.end()
    if isinstance(piece.tree.this, exp.Schema):
        columns = _INSERT_COLUMNS.match(masked, end)
        if columns is None:
            raise NoDbtForm(piece.statement.start_line, "this INSERT's column list")
        end = columns.end()
    return end


def _output_names(query: exp.Expression) -> list[str | None] | None:
    """The names the query's own result carries (None where the SQL engine would invent one: a constant, an
    expression without an alias, `*`), from its first SELECT; None when that cannot be told."""
    node = query
    while isinstance(node, exp.Subquery):
        node = node.this
    while isinstance(node, exp.SetOperation):
        node = node.left
        while isinstance(node, exp.Subquery):
            node = node.this
    if not isinstance(node, exp.Select):
        return None
    names: list[str | None] = []
    for item in node.expressions:
        if isinstance(item, exp.Alias):
            names.append(item.alias)
        elif isinstance(item, exp.Column) and not isinstance(item.this, exp.Star):
            names.append(item.name)
        else:
            names.append(None)
    return names


def _same_names(have: list[str | None] | None, want: list[str]) -> bool:
    return have is not None and len(have) == len(want) and all(
        name is not None and name.upper() == column.upper() for name, column in zip(have, want))


def _wrap_named(query: str, columns: list[str]) -> str:
    return f"WITH q ({', '.join(_ident(column) for column in columns)}) AS (\n{query}\n)\nSELECT * FROM q"


# --- the MERGE ----------------------------------------------------------------------------------------------------


def _using_region(piece: Piece) -> tuple[int, int]:
    """The offsets, in the sentinel text, of the query inside `USING ( ... )` of a MERGE."""
    masked = _masked(piece.statement)
    depth, using = 0, None
    for match in _MERGE_TOKEN.finditer(masked):
        token = match.group(0)
        if token in "()":
            depth += 1 if token == "(" else -1
            continue
        if depth == 0 and token.upper() == "USING":
            using = match.end()
            break
    if using is None:
        raise NoDbtForm(piece.statement.start_line, "this MERGE (no USING)")
    cursor = using
    while cursor < len(masked) and masked[cursor].isspace():
        cursor += 1
    if cursor >= len(masked) or masked[cursor] != "(":
        raise NoDbtForm(piece.statement.start_line, "this MERGE's USING")
    depth = 0
    for position in range(cursor, len(masked)):
        depth += {"(": 1, ")": -1}.get(masked[position], 0)
        if depth == 0:
            return cursor + 1, position
    raise NoDbtForm(piece.statement.start_line, "this MERGE's USING (unbalanced parentheses)")


def _merge_keys(on: exp.Expression | None) -> list[str] | None:
    """The key columns an ON clause equates, `<a>.<K> = <b>.<K>` per AND; None as soon as it is anything else."""
    if on is None:
        return None
    conjuncts = list(on.flatten()) if isinstance(on, exp.And) else [on]
    keys = []
    for item in conjuncts:
        while isinstance(item, exp.Paren):
            item = item.this
        if not isinstance(item, exp.EQ) or not isinstance(item.this, exp.Column) or not isinstance(item.expression, exp.Column):
            return None
        left, right = item.this, item.expression
        if left.name.upper() != right.name.upper() or (left.table or "").upper() == (right.table or "").upper():
            return None
        keys.append(left.name)
    return keys


@dataclass
class _Merge:
    keys: list[str]
    update_columns: list[str]
    extras: list[tuple[str, str]]               # (column, SQL) the INSERT fills with something other than S.<column>
    alias: str                                  # the USING source's alias


def _merge_parts(piece: Piece, target: TargetWrite, columns: list[str]) -> _Merge:
    """What a MERGE says that dbt's `merge` strategy has to be told (or has to be able to do)."""
    tree, line = piece.tree, piece.statement.start_line
    using = tree.args.get("using")
    alias = using.alias if using is not None else ""
    if not alias:
        raise NoDbtForm(line, "this MERGE's USING (it needs an alias, the S of S.<column>)")
    keys = _merge_keys(tree.args.get("on"))
    if keys is None or {key.upper() for key in keys} != {key.upper() for key in target.keys}:
        raise NoDbtForm(line, f"this MERGE's ON clause (expected exactly the objective's keys {', '.join(target.keys)}, "
                              f"`<target>.<KEY> = <source>.<KEY>` joined by AND)")
    whens = (tree.args.get("whens") or exp.Whens()).expressions
    matched = [when for when in whens if when.args.get("matched")]
    unmatched = [when for when in whens if not when.args.get("matched")]
    if len(matched) != 1 or len(unmatched) != 1 or len(whens) != 2 or any(when.args.get("condition") for when in whens):
        raise NoDbtForm(line, "this MERGE's WHEN clauses (expected one WHEN MATCHED THEN UPDATE and one WHEN NOT MATCHED THEN INSERT, no conditions)")
    update, insert = matched[0].args.get("then"), unmatched[0].args.get("then")
    if not isinstance(update, exp.Update) or not isinstance(insert, exp.Insert):
        raise NoDbtForm(line, "this MERGE's WHEN clauses (expected UPDATE SET ... and INSERT ... VALUES ...)")
    update_columns = []
    for assignment in update.expressions:
        left, right = (assignment.this, assignment.expression) if isinstance(assignment, exp.EQ) else (None, None)
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column) or right.name.upper() != left.name.upper() \
                or (right.table or "").upper() != alias.upper():
            raise NoDbtForm(line, f"`{assignment.sql(dialect='snowflake')}` in this MERGE's UPDATE SET; dbt's merge copies "
                                  f"columns of the source, so each must be `<column> = {alias}.<column>`")
        update_columns.append(left.name)
    insert_columns = [column.name for column in insert.this.expressions] if isinstance(insert.this, exp.Tuple) else list(columns)
    values = list(insert.expression.expressions) if isinstance(insert.expression, exp.Tuple) else []
    if len(values) != len(insert_columns):
        raise NoDbtForm(line, "this MERGE's INSERT (the column list and the VALUES differ in length)")
    extras = []
    for column, value in zip(insert_columns, values):
        copies = isinstance(value, exp.Column) and value.name.upper() == column.upper() and (value.table or "").upper() == alias.upper()
        if not copies:
            extras.append((column, value.sql(dialect="snowflake")))
    return _Merge(keys=list(target.keys), update_columns=update_columns, extras=extras, alias=alias)


# --- the walk -----------------------------------------------------------------------------------------------------


def _convert(wf_id: str, objective: dict, text: str) -> list[Model]:
    """Every model of the build, in the order its statement appears. `NoDbtForm` for a statement that has no
    row in the conversion table."""
    targets = objective_targets(objective)
    outputs = {target.logical.upper(): target for target in targets}
    by_logical = {str(output["logical"]).upper(): output for output in objective.get("outputs") or []}
    names = _Names(wf_id, {str(entry["logical"]).upper(): str(entry["logical"]) for entry in objective.get("inputs") or []},
                   outputs)
    regions = build_sql.step_regions(text)
    pieces: list[Piece] = []
    for index, statement in enumerate(build_sql.split_statements(text)):
        if _is_session(statement):
            if index != 0:
                raise NoDbtForm(statement.start_line, "an ALTER SESSION after the first statement")
            continue
        try:
            tree = build_parse.parse(statement)
        except SqlglotError as exc:
            raise NoDbtForm(statement.start_line, f"`{_shown(statement)}`, which does not parse as Snowflake SQL "
                                                  f"({str(exc).splitlines()[0] if str(exc) else exc})") from exc
        if tree is None or next(tree.find_all(exp.Command), None) is not None:
            raise NoDbtForm(statement.start_line, f"`{_shown(statement)}`, which sqlglot's Snowflake parser does not understand")
        pieces.append(_classify(index, statement, tree))

    for piece in pieces:
        if piece.logical is not None and piece.logical not in outputs:
            raise NoDbtForm(piece.statement.start_line, f"{{{{tgt}}}}.{piece.logical}, which is not an output of objective.json")

    # Per output: its core statement (by write mode), the TRUNCATE a truncate_append consumes, and the rest.
    core_of: dict[str, Piece] = {}
    consumed: set[int] = set()
    for logical, target in outputs.items():
        mine = [piece for piece in pieces if piece.logical == logical]
        want = {"overwrite": "ctas", "append": "insert", "truncate_append": "insert", "update_insert": "merge"}[target.mode]
        cores = [piece for piece in mine if piece.kind == want]
        if not mine:
            raise NoDbtForm(None, f"{{{{tgt}}}}.{target.logical}, which the build never writes (objective.json says {target.spelling})")
        if len(cores) != 1:
            at = mine[0].statement.start_line if not cores else cores[1].statement.start_line
            raise NoDbtForm(at, f"{{{{tgt}}}}.{target.logical}: objective.json says {target.spelling}, which is "
                                f"{'one' if cores else 'a'} {want.upper()} statement; found {len(cores)}")
        core = cores[0]
        core_of[logical] = core
        if target.mode == "truncate_append":
            before = next((piece for piece in pieces if piece.index == core.index - 1), None)
            if before is None or before.logical != logical or before.kind != "clear":
                raise NoDbtForm(core.statement.start_line, f"{{{{tgt}}}}.{target.logical}: objective.json says truncate_append, "
                                                            f"which is a TRUNCATE TABLE (or DELETE FROM with no WHERE) immediately before the INSERT")
            consumed.add(before.index)

    order: list[Model] = []
    pending: dict[str, Model] = {}

    def model_for(logical: str) -> Model:
        """The output's model, started when its first statement (a PreSQL) or its core is reached."""
        if logical not in pending:
            target = outputs[logical]
            pending[logical] = Model(name=target.logical.lower(), materialized="table", alias=target.logical,
                                     logical=target.logical, mode=target.mode)
        return pending[logical]

    def touch(model: Model, piece: Piece) -> None:
        for step in _steps_of(regions, piece.statement):
            if step not in model.steps:
                model.steps.append(step)
        model.steps.sort(key=lambda step: int(step[1:]))
        model.comments += _leading_comments(piece.statement)
        model.first_line = piece.statement.start_line if not model.first_line else min(model.first_line, piece.statement.start_line)
        model.last_line = max(model.last_line, piece.statement.end_line)

    for piece in pieces:
        logical = piece.logical
        if piece.kind == "temp":
            name = piece.temp or ""
            if name.upper() in names.temps:
                raise NoDbtForm(piece.statement.start_line, f"the temporary table {name} created a second time (a dbt model has one definition)")
            if name.upper() in outputs or not _MODEL_NAME.fullmatch(name.lower()):
                raise NoDbtForm(piece.statement.start_line, f"the temporary table name {name}; expected a plain identifier that is not an output's name")
            model = Model(name=name.lower(), materialized="ephemeral")
            touch(model, piece)
            model.body = _body(names, piece, piece.tree.expression, _query_start(piece), None)
            names.temps[name.upper()] = model.name
            order.append(model)
            continue
        target = outputs[logical]
        if piece.index in consumed:                           # the TRUNCATE of a truncate_append: the table is replaced
            touch(model_for(logical), piece)
            continue
        if piece is not core_of[logical]:
            hook = _one_line(_rewrite(names, piece, piece.tree, 0, _whole(piece), logical, hook=True))
            model = model_for(logical)
            touch(model, piece)
            (model.pre_hooks if core_of[logical].index > piece.index else model.post_hooks).append(hook)
            continue
        model = model_for(logical)
        touch(model, piece)
        if not _MODEL_NAME.fullmatch(model.name):
            raise NoDbtForm(piece.statement.start_line, f"the output name {target.logical}; expected a plain identifier")
        columns = [str(column["name"]) for column in by_logical[logical].get("columns") or []]
        _write_body(names, piece, target, model, columns)
        names.written.add(logical)
        order.append(model)
    return order


def _write_body(names: _Names, piece: Piece, target: TargetWrite, model: Model, columns: list[str]) -> None:
    """Fills in an output model's materialization and body from its core statement."""
    own = target.logical.upper()
    if piece.kind == "ctas":
        model.materialized = "table"
        model.body = _body(names, piece, piece.tree.expression, _query_start(piece), own)
        return
    if piece.kind == "insert":
        listed = [column.name for column in piece.tree.this.expressions] if isinstance(piece.tree.this, exp.Schema) else columns
        query = _body(names, piece, piece.tree.expression, _query_start(piece), own)
        model.body = query if _same_names(_output_names(piece.tree.expression), listed) else _wrap_named(query, listed)
        if target.mode == "truncate_append":
            model.materialized = "table"
        else:
            model.materialized, model.strategy = "incremental", "append"
        return
    merge = _merge_parts(piece, target, columns)
    model.materialized, model.strategy = "incremental", "merge"
    model.unique_key, model.update_columns = merge.keys, merge.update_columns
    using = piece.tree.args["using"]
    if isinstance(using, exp.Subquery):
        if not isinstance(using.this, exp.Query):
            raise NoDbtForm(piece.statement.start_line, "this MERGE's USING (expected a query or a table)")
        lo, hi = _using_region(piece)
        query = _body(names, piece, using.this, lo, own, hi)
        source = f"(\n{query}\n) AS {merge.alias}"
        known = _output_names(using.this)
        model.body = query
    else:
        if not isinstance(using, exp.Table):
            raise NoDbtForm(piece.statement.start_line, "this MERGE's USING (expected a query or a table)")
        replacement = names.replacement(piece.statement, using, set(), own)
        if replacement is None:
            raise NoDbtForm(piece.statement.start_line, "this MERGE's USING table")
        source, known = f"{replacement} AS {merge.alias}", None
        model.body = f"SELECT * FROM {replacement}"
    if merge.extras:
        taken = {name.upper() for name in known or [] if name}
        exclude = [column for column, _ in merge.extras if column.upper() in taken]
        star = f"{merge.alias}.*" + (f" EXCLUDE ({', '.join(_ident(column) for column in exclude)})" if exclude else "")
        projected = ", ".join([star] + [f"{value} AS {_ident(column)}" for column, value in merge.extras])
        model.body = f"SELECT {projected}\nFROM {source}"


# --- the project's files -----------------------------------------------------------------------------------------


def _config(model: Model) -> str:
    items = [("materialized", model.materialized)]
    if model.strategy:
        items.append(("incremental_strategy", model.strategy))
    if model.materialized == "incremental":
        items.append(("full_refresh", False))       # the table holds history this run cannot rebuild
    if model.unique_key:
        items.append(("unique_key", model.unique_key))
    if model.update_columns:
        items.append(("merge_update_columns", model.update_columns))
    if model.alias:
        items.append(("alias", model.alias))
    for key, hooks in (("pre_hook", model.pre_hooks), ("post_hook", model.post_hooks)):
        if hooks:
            items.append((key, hooks if len(hooks) > 1 else hooks[0]))
    arguments = [f"{key}={_jinja_value(value)}" for key, value in items]
    if len(arguments) <= 2:
        return "{{ config(" + ", ".join(arguments) + ") }}"
    return "{{ config(\n    " + ",\n    ".join(arguments) + "\n) }}"


def model_sql(model: Model) -> str:
    """The model file: its config, the step markers it sits under and the build's own comments, its body."""
    lines = [_config(model)]
    if model.steps:
        lines.append(f"-- step {', '.join(model.steps)}")
    lines += model.comments
    lines.append(model.body)
    return "\n".join(lines) + "\n"


def _dbt_project_yml(wf_id: str, session: str | None) -> str:
    name = dbt_project.project_name(wf_id)
    lines = [f"name: {name}", 'version: "1.0.0"', "config-version: 2", f"profile: {name}", "model-paths: [models]",
             "vars:", "  src_schema: null", "  tgt_schema: null",
             "models:", "  +transient: false           # dbt-snowflake makes tables transient unless told otherwise; "
                        "build.sql's CREATE TABLE is permanent"]
    if session:
        # Not `on-run-start`: dbt runs that on its master connection, and each model on a worker-thread connection of
        # its own, so a session parameter set there never reaches a model. A pre-hook runs on the model's connection.
        hook = "{% if target.type == 'snowflake' %}" + session + "{% endif %}"
        lines += ["  # the build's ALTER SESSION, on each model's own connection before its SQL (and before a PreSQL pre_hook)",
                  "  +pre-hook:", f"    - {_yaml(hook)}"]
    return "\n".join(lines) + "\n"


def _profiles_yml(wf_id: str) -> str:
    name = dbt_project.project_name(wf_id)
    return f"""\
# The dbt profile of the project exported from workflows/{wf_id}/build.sql. No credential belongs in this file.
# `local` is the DuckDB double scripts/validate_dbt.py runs the project on. The Snowflake target below is a
# template: copy it into your own profiles.yml (outside the repository), fill in the placeholders, and keep any
# secret in your environment or your password manager. Nothing in it has ever run: dbt-snowflake is not installed
# in this repository's environment.
{name}:
  target: local
  outputs:
    local:
      type: duckdb
      path: "{{{{ env_var('MIG_DBT_DUCKDB_PATH', 'dbt_sandbox.duckdb') }}}}"
      schema: "{{{{ var('tgt_schema') }}}}"
      threads: 1
    # snowflake:
    #   type: snowflake
    #   account: <account identifier>
    #   user: <user name>
    #   authenticator: externalbrowser        # or another method of your account; never a password in this file
    #   role: <role>
    #   warehouse: <warehouse>
    #   database: <database holding the source views and the target schema>
    #   schema: "{{{{ var('tgt_schema') }}}}"
    #   threads: 1                            # one thread keeps the run order close to build.sql's; raise it if you like
"""


def _sources_yml(wf_id: str, objective: dict) -> str:
    lines = ["version: 2", "sources:", f"  - name: {wf_id}", '    schema: "{{ var(\'src_schema\') }}"', "    tables:"]
    for entry in objective.get("inputs") or []:
        lines.append(f"      - name: {_yaml(str(entry['logical']))}" if not _PLAIN.fullmatch(str(entry["logical"])) else f"      - name: {entry['logical']}")
        columns = [str(column["name"]) for column in entry.get("columns") or []]
        if columns:
            lines.append("        columns:")
            lines += [_column_line(column, 10) for column in columns]
    return "\n".join(lines) + "\n"


def _column_line(column: str, indent: int) -> str:
    pad = " " * indent
    if _PLAIN.fullmatch(column):
        return f"{pad}- name: {column}"
    return f"{pad}- name: {_yaml(column)}\n{pad}  quote: true"


def _schema_yml(objective: dict, models: list[Model]) -> str:
    by_logical = {str(output["logical"]).upper(): output for output in objective.get("outputs") or []}
    lines = ["# not_null and unique state the objective's contract. They are advisories (severity: warn), exactly as",
             "# scripts/compare.py treats a golden NULL in a NOT NULL column and a repeating key: a test that warns says",
             "# the contract is worth revisiting; it never fails the build.",
             "version: 2", "models:"]
    for model in models:
        if model.logical is None:
            continue
        output = by_logical[model.logical.upper()]
        lines.append(f"  - name: {model.name}")
        if output.get("grain"):
            lines.append(f"    description: {_yaml(str(output['grain']))}")
        lines.append("    columns:")
        keys = [str(key) for key in output.get("keys") or []]
        for column in output.get("columns") or []:
            name = str(column["name"])
            lines.append(_column_line(name, 6))
            if column.get("meaning"):
                lines.append(f"        description: {_yaml(str(column['meaning']))}")
            tests = (["not_null"] if column.get("nullable") is False else []) + (["unique"] if keys == [name] else [])
            if tests:
                lines.append("        data_tests:")
                for test in tests:
                    lines += [f"          - {test}:", "              config:", "                severity: warn"]
    return "\n".join(lines) + "\n"


def _lines(model: Model) -> str:
    return f"lines {model.first_line}-{model.last_line}" if model.last_line != model.first_line else f"line {model.first_line}"


def _readme(wf_id: str, objective: dict, models: list[Model], session: str | None) -> str:
    title = str(objective.get("title") or wf_id)
    name = dbt_project.project_name(wf_id)
    rows = []
    for model in models:
        if model.logical is None:
            what, kind = f"temporary table `{model.name}`", "ephemeral (a CTE inside the models that read it)"
        elif model.strategy == "merge":
            what, kind = f"output `{model.logical}`", f"incremental, merge on {', '.join(model.unique_key)}"
        elif model.strategy == "append":
            what, kind = f"output `{model.logical}`", "incremental, append"
        else:
            what = f"output `{model.logical}`"
            kind = "table, replaced on every run" if model.mode == "truncate_append" else "table"
        extras = []
        if model.pre_hooks:
            extras.append(f"{len(model.pre_hooks)} pre_hook")
        if model.post_hooks:
            extras.append(f"{len(model.post_hooks)} post_hook")
        rows.append(f"| `models/{model.name}.sql` | {what} | {kind}{'; ' + ', '.join(extras) if extras else ''} | {_lines(model)} |")
    table = "\n".join(["| Model | What | Materialization | `build.sql` |", "|---|---|---|---|", *rows])
    session_note = (f"\n- The build's `{session}` is a project-level `+pre-hook` of every model in `dbt_project.yml`, "
                    f"guarded for Snowflake: it renders to nothing on the local DuckDB double, which has no sessions. "
                    f"It runs on each model's own connection, before the model's SQL and before the model's own PreSQL "
                    f"`pre_hook` (dbt's `on-run-start` would run on a different connection and set nothing for the "
                    f"models)." if session else "")
    return f"""\
# {wf_id} — {title}, as a dbt project

Exported by `scripts/export_dbt.py` from the validated `workflows/{wf_id}/build.sql`; it is written by code, never by hand
or by an agent, so change `build.sql` (or the objective) and export again rather than editing these files.
**Nothing in this project has run on a real Snowflake account or a real Alteryx engine.** It has been run only on the
local DuckDB double (`dbt-duckdb`), by `scripts/validate_dbt.py`, against the same golden data `build.sql` was checked
against (`dbt/validation.json`).

{table}

Each model starts with the step markers (`-- step s1, s3`) of the objective's steps it implements, so
`docs/mapping.md` and these files point at the same steps.

## Running it

On Snowflake, with a profile named `{name}` in your own profiles directory (the commented `snowflake` target of
`profiles.yml` is a template for it):

```
dbt build --project-dir workflows/{wf_id}/dbt --profiles-dir <your profiles dir> --target <name> \\
  --vars '{{"src_schema": "<SRC_SCHEMA>", "tgt_schema": "<TGT_SCHEMA>"}}'
```

`scripts/deploy.py {wf_id} --dbt --database <DB> --schema <TGT_SCHEMA>` prints that command with the schema names filled
in. Run it from the repository root, or give absolute paths. `dbt build` runs the models and the tests of
`models/schema.yml` (`not_null` for every NOT NULL column of the objective, `unique` for a one-column key). Those tests
are `severity: warn`: a NULL in a column the objective calls NOT NULL, or a repeated key, is reported as a warning
(the objective's contract is worth revisiting) and never fails the build.

- `src_schema` is the schema of the source views `scripts/gen_source_views.py` generates (`MIG_SRC_{wf_id.upper().replace('_', '')}`, one view per
  logical source) and `tgt_schema` the schema the outputs are written to; both live in the profile's database.
  `dbt_project.yml` declares them as `null`: pass both on every run.{session_note}
- An output written as `append` or `update_insert` is an incremental model, and its table must already exist in
  `tgt_schema` with its history; so must the table of any output with a PreSQL (its `pre_hook` runs before the model).
  The first run of an incremental model on a missing table creates it from the model's query. An incremental model
  carries `full_refresh=false`, so `dbt build --full-refresh` leaves it alone: the table holds history that this
  project cannot rebuild from one run's rows.
- It needs dbt-core 1.8 or later (`data_tests:` in `models/schema.yml`).
- Locally, `python scripts/validate_dbt.py {wf_id}` builds a fresh DuckDB sandbox per golden set and runs this project on it.
"""


def _build_files(wf_id: str, objective: dict, models: list[Model], session: str | None) -> dict[str, str]:
    files = {"dbt_project.yml": _dbt_project_yml(wf_id, session), "profiles.yml": _profiles_yml(wf_id),
             "README.md": _readme(wf_id, objective, models, session), "models/sources.yml": _sources_yml(wf_id, objective),
             "models/schema.yml": _schema_yml(objective, models)}
    for model in models:
        files[f"models/{model.name}.sql"] = model_sql(model)
    return files


# --- the entry point ---------------------------------------------------------------------------------------------


def _session_of(text: str) -> str | None:
    """The build's first `ALTER SESSION SET ...` as one line, comments dropped; None when it has none."""
    statements = build_sql.split_statements(text)
    if statements and _is_session(statements[0]):
        return " ".join(build_sql.without_comments(statements[0].text).split())
    return None


def skip_reason(repo: Repo, wf_id: str) -> str | None:
    """Why this workflow has no dbt project -- `program.dbt` is false in `mappings/global.yaml`, or the objective's
    language is python -- or None when it should have one. `validate_dbt.py` asks the same question, so the two
    scripts always agree on what is skipped. `FileNotFoundError` / `ValueError` when the objective cannot be read."""
    if not _dbt_enabled(repo):
        return "program.dbt is false in mappings/global.yaml"
    objective = objective_lib.load(repo, wf_id)
    if objective.get("tier") == "T3":
        return "the objective says tier T3: the workflow is not translated, so it has no build file and no dbt project"
    if objective.get("language") == "python":
        return "the objective's language is python: a Python workflow deploys as a Snowpark procedure, not a dbt project"
    return None


def export(repo: Repo, wf_id: str, *, write: bool = True) -> Export:
    """Converts `workflows/<wf_id>/build.sql` and (with `write`) replaces `workflows/<wf_id>/dbt/` with the
    result. `NoDbtForm` (exit 1) when a statement has no dbt form, `UsageError` / `FileNotFoundError` /
    `ValueError` / `ContractError` (exit 2) when there is nothing to export from. A skip and a `NoDbtForm` remove
    an old `dbt/` (it no longer matches the build); the exit-2 paths leave it where it is, because nothing was
    learned about the build."""
    repo.wf(wf_id)                                              # the id is checked before anything is touched
    reason = skip_reason(repo, wf_id)
    if reason is not None:
        if write:
            remove_export(repo, wf_id)
        return Export(wf_id, skipped=reason)
    objective = objective_lib.load(repo, wf_id)
    if objective.get("language") != "sql":
        raise UsageError(f"{wf_id}: objective.json has language {objective.get('language')!r}; expected sql or python")
    path = objective_lib.build_path(repo, wf_id)
    if path is None:
        raise FileNotFoundError(f"{wf_id} has no build file to export: expected {repo.wf(wf_id, 'build.sql')}")
    if path.suffix != ".sql":
        raise UsageError(f"{path.name} is a python build but objective.json says language 'sql'; expected build.sql")
    for position, entry in enumerate(objective.get("inputs") or []):
        if not entry.get("logical"):
            raise ContractError(f"objective.json inputs[{position}] (tool {entry.get('tool_id')}) has no logical name; "
                                f"expected intake to have named it")
    text = path.read_text(encoding="utf-8-sig")
    try:
        models = _convert(wf_id, objective, text)
    except NoDbtForm:
        if write:
            remove_export(repo, wf_id)
        raise
    result = Export(wf_id, files=_build_files(wf_id, objective, models, _session_of(text)), models=models)
    if write:
        remove_export(repo, wf_id)
        for relative, content in result.files.items():
            write_text(repo.wf(wf_id, "dbt", *relative.split("/")), content)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id", help="workflow id, e.g. wf_0001")
    add_root_arg(parser)
    args = parser.parse_args(argv)

    # Not parser.error(): main() is called in-process by callers that expect an int back.
    try:
        result = export(Repo(args.root), args.wf_id)
    except NoDbtForm as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (FileNotFoundError, UsageError, ContractError, ValueError) as exc:
        print(f"cannot export {args.wf_id}: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 -- exit 2 is "anything unexpected"; never a bare traceback exit 1
        traceback.print_exc()
        return 2
    if result.skipped:
        print(f"{args.wf_id}: dbt export skipped: {result.skipped}")
    else:
        print(f"{args.wf_id}: wrote workflows/{args.wf_id}/dbt ({len(result.models)} models: "
              f"{', '.join(model.name for model in result.models)})")
    return 0


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
