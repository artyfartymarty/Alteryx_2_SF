"""Is a `build.sql` minimal? Three deterministic flags (objective-first design §4.6, decision D7).

The pipeline's old output was one CTE per Alteryx tool, each re-listing every column: correct, and far
too large. A build file is meant to be the least SQL that produces the objective's outputs, so
`compile_check.py` lints it and reports the result under `lint` in `compile_check.json`. A flag is never
an error: a person may have a good reason for a CTE the lint dislikes, so the translator either removes
the flag or says why it stays (`lint <kind> <name>: kept because ...` in `translation_notes.md`), and the
reviewer blocks one that is neither.

* `pass_through_cte` -- a CTE or a temporary table whose query only projects, renames or `SELECT *`s
  the columns of ONE relation: no `WHERE`, `GROUP BY`, `HAVING`, `QUALIFY`, `JOIN`, `DISTINCT`, window,
  `ORDER BY` or `LIMIT`, no set operation, no expression other than a column. Inline it.
* `tool_by_tool` -- at least four CTEs (temporary tables count) and as many CTEs as the objective has
  tools that are neither an input nor an output: the shape of a per-tool translation.
* `single_use_chain` -- three or more CTEs (or temporary tables) in a row, each read only by the next, none
  filtering, grouping or joining: one SELECT could say the same.

Everything is read from sqlglot's tree of each statement (`lib.build_parse`); a statement that does not
parse is skipped here (`compile_check.py` reports it as `parse`). Python builds are not linted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import SqlglotError

from . import build_parse
from .build_sql import Statement, split_statements

PASS_THROUGH = "pass_through_cte"
TOOL_BY_TOOL = "tool_by_tool"
SINGLE_USE_CHAIN = "single_use_chain"

#: A `tool_by_tool` file has at least this many CTEs, whatever the objective's size.
MIN_TOOL_BY_TOOL_CTES = 4
#: A chain is at least this many nodes long.
MIN_CHAIN = 3

#: Parts of a SELECT that make it do more than project columns (sqlglot's argument names).
_REAL_WORK = ("where", "group", "having", "qualify", "distinct", "order", "limit", "offset", "sample",
              "joins", "pivots", "laterals", "match")
#: Parts that filter, group or join -- what a chain link may not do.
_FILTER_GROUP_JOIN = ("where", "group", "having", "qualify", "distinct", "limit", "offset", "sample", "joins")


@dataclass
class _Node:
    """A named intermediate: a CTE of one statement or a temporary table, and the relations it reads."""
    key: tuple                                   # ("cte", statement index, NAME) or ("temp", NAME, statement index)
    name: str
    line: int
    query: exp.Expression | None
    readers: set[tuple] = field(default_factory=set)   # keys of the nodes (or ("main", index)) that read it


def _has_with(query: exp.Expression) -> bool:
    return bool(query.args.get("with_") or query.args.get("with"))


def _from_table(select: exp.Select) -> exp.Table | None:
    source = select.args.get("from_") or select.args.get("from")
    table = source.this if source is not None else None
    return table if isinstance(table, exp.Table) and isinstance(table.this, exp.Identifier) else None


def _is_projection(expression: exp.Expression) -> bool:
    """A bare column, a renamed column, or an unrenamed star: what a pass-through selects."""
    inner = expression.this if isinstance(expression, exp.Alias) else expression
    if isinstance(inner, exp.Star):
        return not isinstance(expression, exp.Alias)
    return isinstance(inner, exp.Column)


def is_pass_through(query: exp.Expression | None) -> bool:
    """The query of a CTE / temporary table only selects or renames columns of one relation."""
    if not isinstance(query, exp.Select) or _has_with(query):
        return False
    if any(query.args.get(part) for part in _REAL_WORK) or _from_table(query) is None:
        return False
    if query.find(exp.Window) is not None:
        return False
    return bool(query.expressions) and all(_is_projection(item) for item in query.expressions)


def is_plain(query: exp.Expression | None) -> bool:
    """For a chain: one SELECT that does not filter, group or join (nor aggregate or window, which are
    grouping by another name)."""
    if not isinstance(query, exp.Select) or _has_with(query):
        return False
    if any(query.args.get(part) for part in _FILTER_GROUP_JOIN):
        return False
    return query.find(exp.Window) is None and query.find(exp.AggFunc) is None


def _unqualified_reads(root: exp.Expression, *, outside_ctes: bool = False, writes: frozenset[int] = frozenset()) -> set[str]:
    """Upper-cased names of the unqualified tables under `root` -- CTE names and temporary tables -- and
    nothing a placeholder or a qualifier names. `outside_ctes` leaves out tables inside a CTE definition;
    `writes` are the ids of tables that are written, not read."""
    names: set[str] = set()
    for table in root.find_all(exp.Table):
        if table.db or table.catalog or not isinstance(table.this, exp.Identifier) or id(table) in writes:
            continue
        if outside_ctes and table.find_ancestor(exp.CTE) is not None:
            continue
        names.add(table.name.upper())
    return names


def _nodes(parsed: list[tuple[Statement, exp.Expression]]) -> list[_Node]:
    """Every CTE and temporary table of the file in definition order, each knowing who reads it."""
    nodes: list[_Node] = []
    by_key: dict[tuple, _Node] = {}
    latest_temp: dict[str, tuple] = {}

    def record(node: _Node) -> None:
        nodes.append(node)
        by_key[node.key] = node

    def credit(reader: tuple, names: set[str], index: int, local: set[str]) -> None:
        for name in names:
            owner = ("cte", index, name) if name in local else latest_temp.get(name)
            if owner in by_key:
                by_key[owner].readers.add(reader)

    for index, (statement, tree) in enumerate(parsed):
        local = {cte.alias.upper() for cte in tree.find_all(exp.CTE)}
        for cte in tree.find_all(exp.CTE):
            alias = cte.args.get("alias")
            record(_Node(key=("cte", index, cte.alias.upper()), name=cte.alias,
                         line=build_parse.line_of(statement, alias.this if alias is not None else None),
                         query=cte.this))
        for cte in tree.find_all(exp.CTE):
            credit(("cte", index, cte.alias.upper()), _unqualified_reads(cte.this), index, local)
        writes = frozenset(id(table) for table in build_parse.write_tables(tree))
        top_reads = _unqualified_reads(tree, outside_ctes=True, writes=writes)
        if build_parse.is_temporary_create(tree) and build_parse.write_tables(tree):
            target = build_parse.write_tables(tree)[0]
            key = ("temp", target.name.upper(), index)
            credit(key, top_reads, index, local)               # reads the OLD table of the same name, if any
            record(_Node(key=key, name=target.name, line=build_parse.line_of(statement, target.this),
                         query=tree.expression))
            latest_temp[target.name.upper()] = key
        else:
            credit(("main", index), top_reads, index, local)
    return nodes


def _chains(nodes: list[_Node]) -> list[list[_Node]]:
    """Maximal runs of plain nodes where each one is read only by the next."""
    by_key = {node.key: node for node in nodes}
    successor: dict[tuple, tuple] = {}
    for node in nodes:
        if is_plain(node.query) and len(node.readers) == 1:
            (following,) = node.readers
            if following in by_key and is_plain(by_key[following].query):
                successor[node.key] = following
    chains = []
    for node in nodes:
        if node.key in successor and node.key not in set(successor.values()):
            chain, key = [node], node.key
            while key in successor:
                key = successor[key]
                chain.append(by_key[key])
            chains.append(chain)
    return chains


def _non_io_tools(objective: dict) -> set[str]:
    """The tool ids the objective's steps cover, less its inputs' and outputs'."""
    covered = {str(tool) for step in objective.get("steps") or [] for tool in step.get("tools") or []}
    io_tools = {str(entry.get("tool_id")) for key in ("inputs", "outputs") for entry in objective.get(key) or []}
    return covered - io_tools


def _flag(kind: str, node: _Node, note: str) -> dict:
    return {"kind": kind, "name": node.name, "line": node.line, "note": note}


def lint(parsed: list[tuple[Statement, exp.Expression]], objective: dict, *, lines: int,
         statements: int | None = None) -> dict:
    """The lint report for the statements of a build file that parsed: `{"lines", "statements", "ctes",
    "flags"}` (design §4.6). `parsed` pairs each parsed statement with its tree; `lines` is the file's
    line count; `statements` is what the report shows (default `len(parsed)`: a statement that did not
    parse still counts)."""
    nodes = _nodes(parsed)
    flags = [_flag(PASS_THROUGH, node, "only selects or renames columns of one relation; inline it")
             for node in nodes if is_pass_through(node.query)]
    tools = _non_io_tools(objective)
    if len(nodes) >= MIN_TOOL_BY_TOOL_CTES and len(nodes) >= len(tools):
        flags.append(_flag(TOOL_BY_TOOL, nodes[0],
                           f"{len(nodes)} CTEs for {len(tools)} tools is the shape of a tool-by-tool "
                           f"translation; make each tool a clause of the statement that needs it"))
    flags += [_flag(SINGLE_USE_CHAIN, chain[0],
                    f"{', '.join(node.name for node in chain)} are each read only by the next and none "
                    f"filters, groups or joins; one SELECT can say the same")
              for chain in _chains(nodes) if len(chain) >= MIN_CHAIN]
    flags.sort(key=lambda flag: (flag["line"], flag["kind"]))
    return {"lines": lines, "statements": len(parsed) if statements is None else statements,
            "ctes": len(nodes), "flags": flags}


def lint_text(text: str, objective: dict) -> dict:
    """`lint` over the file `text` itself: split it, parse each statement, skip those that do not parse."""
    statements = split_statements(text)
    parsed = []
    for statement in statements:
        try:
            tree = build_parse.parse(statement)
        except SqlglotError:
            continue
        if tree is not None:
            parsed.append((statement, tree))
    return lint(parsed, objective, lines=len(text.splitlines()), statements=len(statements))
