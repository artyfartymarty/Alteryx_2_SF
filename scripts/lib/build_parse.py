"""A build file's statements as sqlglot trees, with the `{{src}}`/`{{tgt}}` placeholders understood
(objective-first design §4.4).

`lib/build_sql.py` reads a build file as text; `compile_check.py` (the table rules) and
`lib/minimality.py` (the lint) both need the structure of each statement, so the shared part lives
here: one way to parse a statement of the file, one way to say which tables it reads and writes, and one
way to turn a position inside a parsed statement back into a line of the file.

* A placeholder is rendered to a **sentinel** two-part name (`SRC__PH.SRC__PH`, `TGT__PH.TGT__PH`) for
  parsing, never to a real schema, so a table written as `{{src}}.ORDERS` is told from one a person typed
  out as `MYDB.RAW.ORDERS` (`TableRef.kind` is `"src"`/`"tgt"` for the first, `"other"` for the second).
  The sentinel has the same shape as the real thing (`<db>.<schema>`), and no newline, so lines survive.
* A line is read from sqlglot's own position metadata on an identifier (relative to the text it parsed),
  shifted by where the statement's first line sits in the file. When an identifier carries none, the
  statement's first code line is the answer.

Nothing here has run against a real Snowflake account; sqlglot's Snowflake dialect is the only parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .build_sql import Statement, masked_code

#: What `{{src}}` and `{{tgt}}` become when a statement is parsed for analysis (see the module docstring).
SRC_MARK = "SRC__PH"
TGT_MARK = "TGT__PH"

_PLACEHOLDER = re.compile(r"\{\{\s*(src|tgt)\s*\}\}")


def sentinel(text: str) -> str:
    """`text` with each `{{ src }}` / `{{ tgt }}` replaced by its two-part sentinel name. Any other
    `{{ ... }}` is left as it is (it is a `template:placeholder` error, not this module's business)."""
    return _PLACEHOLDER.sub(lambda match: f"{SRC_MARK}.{SRC_MARK}" if match.group(1) == "src"
                            else f"{TGT_MARK}.{TGT_MARK}", text)


def first_line(statement: Statement) -> int:
    """The file line of the first line of `statement.text`, which keeps the comments in front of the
    statement's first code character (`Statement.start_line` is that character's line)."""
    masked = masked_code(statement.text)
    leading = len(masked) - len(masked.lstrip())
    return statement.start_line - statement.text[:leading].count("\n")


def parse(statement: Statement) -> exp.Expression | None:
    """The statement parsed with sqlglot's Snowflake dialect, placeholders as sentinels. `SqlglotError`
    when it does not parse; None when there is nothing in it."""
    return sqlglot.parse_one(sentinel(statement.text), read="snowflake")


def line_of(statement: Statement, node: exp.Expression | None) -> int:
    """The file line of `node` (a node of `parse(statement)`), judged from its own position (an identifier,
    or the argument of an `IDENTIFIER(...)`), else from the first identifier under it, else the statement's
    first code line."""
    probe = node
    if probe is not None and not isinstance(probe.meta.get("line"), int):
        probe = probe.find(exp.Identifier)
    line = (probe.meta.get("line") if probe is not None else None)
    if not isinstance(line, int):
        return statement.start_line
    return first_line(statement) + line - 1


def name_of(identifier: exp.Expression | None) -> str:
    """An identifier as its text, quotes removed ("Items" -> Items)."""
    return identifier.name if identifier is not None else ""


@dataclass(frozen=True)
class TableRef:
    """One table a statement names. `kind`: "src" (`{{src}}.X`), "tgt" (`{{tgt}}.X`), "cte" (a name the
    statement's own WITH defines), "plain" (an unqualified name that is not a CTE: a temporary table, if
    it is anything), "other" (a qualified name no placeholder produced) or "identifier" (a table named through
    `IDENTIFIER('...')` / `IDENTIFIER(:X)`, the stored-procedure habit: a build file names a table only through a
    placeholder). `write` is true where the statement writes the table (CREATE, INSERT, MERGE, UPDATE, DELETE,
    TRUNCATE) rather than reads it."""
    kind: str
    name: str
    write: bool
    line: int
    shown: str            # the name as the statement spells it, qualifiers included (for messages)


def write_tables(tree: exp.Expression) -> list[exp.Table]:
    """The `exp.Table` nodes `tree` writes."""
    if isinstance(tree, (exp.Create, exp.Insert, exp.Merge, exp.Update, exp.Delete)):
        target = tree.this
        if isinstance(target, exp.Schema):
            target = target.this
        return [target] if isinstance(target, exp.Table) else []
    if isinstance(tree, exp.TruncateTable):
        return [table for table in tree.expressions if isinstance(table, exp.Table)]
    return []


def table_refs(statement: Statement, tree: exp.Expression) -> list[TableRef]:
    """Every table `tree` (the parsed `statement`) names, in source order, classified. A table function
    (`TABLE(FLATTEN(...))`), a stage or a file is not a named table and is not returned: the external
    access check looks at those. A table named through `IDENTIFIER(...)` is returned with kind "identifier"."""
    ctes = {cte.alias.upper() for cte in tree.find_all(exp.CTE)}
    writes = {id(table) for table in write_tables(tree)}
    refs: list[TableRef] = []
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.DynamicIdentifier):
            argument = table.this.this
            shown = f"IDENTIFIER({argument.sql(dialect='snowflake') if argument is not None else ''})"
            refs.append(TableRef(kind="identifier", name="", write=id(table) in writes,
                                 line=line_of(statement, table.this), shown=shown))
            continue
        if not isinstance(table.this, exp.Identifier):
            continue
        name, schema, catalog = table.name, table.db, table.catalog
        if not schema and not catalog:
            kind = "cte" if name.upper() in ctes and id(table) not in writes else "plain"
        elif catalog == SRC_MARK and schema == SRC_MARK:
            kind = "src"
        elif catalog == TGT_MARK and schema == TGT_MARK:
            kind = "tgt"
        else:
            kind = "other"
        shown = ".".join(part for part in (catalog, schema, name) if part)
        shown = shown.replace(f"{SRC_MARK}.{SRC_MARK}", "{{src}}").replace(f"{TGT_MARK}.{TGT_MARK}", "{{tgt}}")
        refs.append(TableRef(kind=kind, name=name, write=id(table) in writes,
                             line=line_of(statement, table.this), shown=shown))
    refs.sort(key=lambda ref: ref.line)
    return refs


def is_temporary_create(tree: exp.Expression) -> bool:
    """`CREATE [OR REPLACE] TEMPORARY TABLE ...` (also spelled TEMP)."""
    if not isinstance(tree, exp.Create) or str(tree.args.get("kind") or "").upper() != "TABLE":
        return False
    properties = tree.args.get("properties")
    return properties is not None and any(isinstance(item, exp.TemporaryProperty) for item in properties.expressions)
