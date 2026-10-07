"""Write `workflows/<id>/docs/mapping.md`: how the Alteryx workflow maps to its build file (objective-first
design §4.9).

    render_mapping.py <wf_id> [--root .]

The page is generated, never written by a model: a Mermaid flowchart of the objective's business steps
(with the Alteryx tool ids each covers) beside the build file's regions (their line ranges and what each
creates or writes), then a table of EVERY tool of `parsed/dag.json` -- its type, what it does, the step
that covers it and the lines that implement that step. The documenter links the page and never rewrites it.

Inputs: `objective.json` (the steps, their `feeds`, `no_effect`), the step markers in `build.sql`
(`-- step s1, s3`) or `build.py` (`# step s1`) read through `lib/build_sql.step_regions`, and the parsed DAG.
Solid edges are `feeds` between steps (a feed to `out:<tool id>` is not an edge: the output is the region
that writes it); dotted edges go from each step to the region(s) carrying its marker. A region's label names
its statements by kind and table (`CREATE TABLE SALES_SUMMARY`, `TEMPORARY TABLE orders`, `MERGE INTO X`,
`INSERT INTO X`); a Python region is only `lines a–b`. A "what it does" cell is the digest's configuration
text for the tool (`lib/tool_lines.config_summary`), cut at 120 characters with an ellipsis.

Every label is escaped for Mermaid (`lib/mermaid_lint.escape_label`) and the finished block must pass the
structural lint the README's diagrams pass (`lint_mermaid`). That lint counts brackets and quotes; it does
not render, so whether Mermaid draws the diagram is a person's check in a browser.

Exit codes: 0 written (the path is printed); 1 the objective, the build file and the DAG disagree -- a step
with no region, a marker naming an unknown step, a tool that is in no step and not in `no_effect` (a container,
comment or Browse node is exempt, as in `objective_check.py`, and shows as `no effect`), a diagram that fails
the lint -- every such problem is printed and nothing is written; 2 usage -- a missing
`objective.json`, `parsed/dag.json` or build file, both `build.sql` and `build.py`, a workflow id that is not
one, an objective with no `steps`.

Nothing here has run on Snowflake or Alteryx: it reads files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Sequence

from lib import build_sql
from lib import objective as objective_lib
from lib.io import read_json, write_text
from lib.mermaid_lint import escape_label, lint_mermaid
from lib.paths import Repo, add_root_arg
from lib.tool_lines import config_summary, label, tool_key
from lib.vocab import DATA_LESS_TYPES

#: A "what it does" cell is at most this many characters, the last of them `…` when it was cut.
CELL_LIMIT = 120
#: A step node names this many of its tools (`T3 Filter`); the table lists them all.
TOOLS_PER_STEP_LABEL = 8
#: A region node names this many of its statements.
STATEMENTS_PER_REGION_LABEL = 3

_DASH = "–"
_NONE = "—"


class MappingError(Exception):
    """The objective, the build file and the DAG do not agree (exit 1). `problems` is every disagreement."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# --- what a region does -----------------------------------------------------------------------------

_QUALIFIER = re.compile(r"\{\{[^{}]*\}\}\s*\.\s*")
_NAME = r'((?:"[^"]*"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:"[^"]*"|[A-Za-z_][\w$]*))*)'
_IF_NOT_EXISTS = r"(?:IF\s+NOT\s+EXISTS\s+)?"
#: (statement start, label): the table is the last dotted part of the name, so `{{tgt}}.X` is `X`.
_KINDS = (
    (re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:LOCAL|GLOBAL|VOLATILE)\s+)?(?:TEMP|TEMPORARY)\s+TABLE\s+"
                + _IF_NOT_EXISTS + _NAME, re.IGNORECASE), "TEMPORARY TABLE {}"),
    (re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?TABLE\s+" + _IF_NOT_EXISTS + _NAME,
                re.IGNORECASE), "CREATE TABLE {}"),
    (re.compile(r"MERGE\s+INTO\s+" + _NAME, re.IGNORECASE), "MERGE INTO {}"),
    (re.compile(r"INSERT\s+(?:OVERWRITE\s+)?INTO\s+" + _NAME, re.IGNORECASE), "INSERT INTO {}"),
    (re.compile(r"TRUNCATE\s+(?:TABLE\s+)?(?:IF\s+EXISTS\s+)?" + _NAME, re.IGNORECASE), "TRUNCATE TABLE {}"),
    (re.compile(r"DELETE\s+FROM\s+" + _NAME, re.IGNORECASE), "DELETE FROM {}"),
    (re.compile(r"UPDATE\s+" + _NAME, re.IGNORECASE), "UPDATE {}"),
    (re.compile(r"ALTER\s+SESSION\b", re.IGNORECASE), "ALTER SESSION"),
)


def statement_label(statement: build_sql.Statement) -> str:
    """The statement's kind and table in the diagram's words: `CREATE TABLE SALES_SUMMARY` for a
    `CREATE [OR REPLACE] TABLE`, `TEMPORARY TABLE orders` for a temporary one, `MERGE INTO X`,
    `INSERT INTO X`, `TRUNCATE TABLE X`, `DELETE FROM X`, `UPDATE X`, `ALTER SESSION`; any other statement
    is its first keyword."""
    text = _QUALIFIER.sub("", " ".join(build_sql.without_comments(statement.text).split()))
    for pattern, template in _KINDS:
        found = pattern.match(text)
        if found:
            if found.lastindex is None:
                return template
            return template.format(re.split(r"\s*\.\s*", found.group(1))[-1].strip('"'))
    return text.split(" ", 1)[0].upper() or "statement"


def _line_range(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}{_DASH}{end}"


def _region_label(region: build_sql.Region, statements: list[build_sql.Statement]) -> str:
    where = f"line {region.start_line}" if region.start_line == region.end_line else \
        f"lines {region.start_line}{_DASH}{region.end_line}"
    inside = [statement_label(s) for s in statements if region.start_line <= s.start_line <= region.end_line]
    shown = inside[:STATEMENTS_PER_REGION_LABEL]
    if len(inside) > len(shown):
        shown.append(f"+{len(inside) - len(shown)} more")
    return escape_label(f"{where} · {', '.join(shown)}" if shown else where)


# --- the objective, read defensively ------------------------------------------------------------------

def _steps(objective: dict) -> list[dict]:
    if not isinstance(objective.get("steps"), list):
        raise ValueError("objective.json: field 'steps' is missing or is not a list; expected the analyzer's "
                         "steps (design §4.2)")
    steps = []
    for number, step in enumerate(objective["steps"], 1):
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            raise ValueError(f"objective.json: steps[{number - 1}] has no string 'id'; expected s<N>")
        for key in ("tools", "feeds"):
            if not isinstance(step.get(key) or [], list):
                raise ValueError(f"objective.json: steps[{number - 1}].{key} is not a list; expected a list of ids")
        steps.append({"id": step["id"], "name": str(step.get("name") or ""),
                      "tools": [str(t) for t in step.get("tools") or []],
                      "feeds": [str(f) for f in step.get("feeds") or []]})
    return steps


def _no_effect(objective: dict) -> dict[str, str]:
    entries = objective.get("no_effect") or []
    if not isinstance(entries, list) or not all(isinstance(e, dict) and "tool_id" in e for e in entries):
        raise ValueError("objective.json: field 'no_effect' is not a list of {tool_id, reason}; expected that")
    return {str(entry["tool_id"]): str(entry.get("reason") or "") for entry in entries}


# --- the problems ---------------------------------------------------------------------------------------

def _problems(steps: list[dict], no_effect: dict[str, str], regions: list[build_sql.Region], nodes: dict[str, dict],
              build_name: str, comment: str) -> list[str]:
    ids = [step["id"] for step in steps]
    problems = []
    for step in steps:
        if not objective_lib.STEP_ID.match(step["id"]):
            problems.append(f"objective.json: step id {step['id']!r} is not s<N>; expected an id like s1")
    for region in regions:
        problems += [f"{build_name} line {region.marker_line}: the marker names step {name}, which objective.json "
                     f"does not have; expected one of {', '.join(ids)}" for name in region.steps if name not in ids]
    carried = {name for region in regions for name in region.steps}
    for step in steps:
        if step["id"] not in carried:
            problems.append(f"step {step['id']} has no region in {build_name}: expected a '{comment} step "
                            f"{step['id']}' comment line followed by the code that implements it")
        problems += [f"step {step['id']} feeds {feed}, which objective.json does not have; expected a step id "
                     f"or out:<tool id>" for feed in step["feeds"] if feed not in ids and not feed.startswith("out:")]
        problems += [f"step {step['id']} names tool {tool}, which parsed/dag.json does not have"
                     for tool in step["tools"] if tool not in nodes]
    covered = {tool for step in steps for tool in step["tools"]} | set(no_effect)
    problems += [f"tool {tool} ({label(node.get('type'))}) is in no step's tools and not in no_effect: add it to "
                 f"the step that covers it, or to no_effect with a reason"
                 for tool, node in nodes.items() if tool not in covered and node.get("type") not in DATA_LESS_TYPES]
    return problems


# --- the page -------------------------------------------------------------------------------------------

def _plain(text: object) -> str:
    """Ordinary text as one Markdown table cell: one line, no pipe, backtick, angle bracket or ampersand."""
    text = " ".join(str(text).split())
    for char, entity in (("\\", "\\\\"), ("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ("|", "\\|"), ("`", "\\`")):
        text = text.replace(char, entity)
    return text


def _code(text: str) -> str:
    """`text` as an inline code span inside a table cell: a pipe escaped, the fence one backtick longer than
    any run inside, padded when the text starts or ends with a backtick."""
    text = text.replace("|", "\\|")
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _what_it_does(node: dict) -> str:
    text = config_summary(node)
    if not text:
        return _NONE
    return _code(text if len(text) <= CELL_LIMIT else text[:CELL_LIMIT - 1] + "…")


def _step_label(step: dict, nodes: dict[str, dict]) -> str:
    tools = sorted(dict.fromkeys(step["tools"]), key=tool_key)
    shown = [f"T{tool} {label(nodes[tool].get('type'))}" for tool in tools[:TOOLS_PER_STEP_LABEL]]
    if len(tools) > len(shown):
        shown.append(f"+{len(tools) - len(shown)} more")
    head = escape_label(" ".join(f"{step['id']} · {step['name']}".split()))
    return head + (f"<br/>{' · '.join(escape_label(part) for part in shown)}" if shown else "")


def _diagram(steps: list[dict], regions: list[build_sql.Region], statements: list[build_sql.Statement],
             nodes: dict[str, dict], source_name: str, build_name: str, sql: bool) -> str:
    ax = escape_label(f"Alteryx · {source_name}" if source_name else "Alteryx workflow")
    lines = ["flowchart LR", f'  subgraph AX["{ax}"]']
    lines += [f'    {step["id"]}["{_step_label(step, nodes)}"]' for step in steps]
    lines += ["  end", f'  subgraph CODE["{escape_label(build_name)}"]']
    for index, region in enumerate(regions, 1):
        text = _region_label(region, statements if sql else [])
        lines.append(f'    r{index}["{text}"]')
    lines.append("  end")
    lines += [f"  {step['id']} --> {feed}" for step in steps for feed in dict.fromkeys(step["feeds"])
              if not feed.startswith("out:")]
    lines += [f"  {step['id']} -.-> r{index}" for step in steps
              for index, region in enumerate(regions, 1) if step["id"] in region.steps]
    return "\n".join(lines) + "\n"


def _table(steps: list[dict], no_effect: dict[str, str], regions: list[build_sql.Region],
           nodes: dict[str, dict]) -> str:
    step_of: dict[str, list[str]] = {}
    for step in steps:
        for tool in dict.fromkeys(step["tools"]):
            step_of.setdefault(tool, []).append(step["id"])
    rows = ["| tool | type | what it does | step | lines |", "|---|---|---|---|---|"]
    for tool in sorted(nodes, key=tool_key):
        node = nodes[tool]
        if tool in step_of:
            ranges = [_line_range(r.start_line, r.end_line) for r in regions if set(step_of[tool]) & set(r.steps)]
            where, lines = ", ".join(step_of[tool]), ", ".join(ranges) or _NONE
        else:
            # `objective_check.py` exempts data-less nodes (containers, comments, Browse): they need not be in
            # `no_effect`, so one that is not gets the reason the type itself gives.
            reason = no_effect.get(tool) or f"{label(node.get('type'))} tool: carries no data"
            where, lines = _plain(f"no effect: {reason}"), _NONE
        rows.append(f"| T{_plain(tool)} | {_plain(label(node.get('type')))} | {_what_it_does(node)} | "
                    f"{where} | {lines} |")
    return "\n".join(rows) + "\n"


def render(wf_id: str, objective: dict, dag: dict, build_text: str, build_name: str) -> str:
    """The mapping page for one workflow. `MappingError` listing every disagreement between the objective, the
    build file and the DAG (or a diagram that fails the lint); `ValueError` for an objective that is not shaped
    like one."""
    sql = not build_name.endswith(".py")
    comment = "--" if sql else "#"
    steps, no_effect = _steps(objective), _no_effect(objective)
    by_id = {str(node["tool_id"]): node for node in dag["nodes"]}
    nodes = {tool: by_id[tool] for tool in objective_lib.dag_tool_ids(dag)}
    regions = build_sql.step_regions(build_text, comment)
    problems = _problems(steps, no_effect, regions, nodes, build_name, comment)
    if problems:
        raise MappingError(problems)

    statements = build_sql.split_statements(build_text) if sql else []
    source_name = re.split(r"[\\/]+", str(dag.get("source_file") or ""))[-1]
    diagram = _diagram(steps, regions, statements, nodes, source_name, build_name, sql)
    lint = lint_mermaid(diagram)
    if lint:
        raise MappingError([f"the Mermaid diagram fails the structural lint ({problem})" for problem in lint])
    return (f"# {wf_id} — how the Alteryx workflow maps to {build_name}\n\n"
            f"_Generated by `scripts/render_mapping.py` from `objective.json`, the `{comment} step` markers in "
            f"`{build_name}` and `parsed/dag.json`. Do not edit it: the line numbers are those of "
            f"`{build_name}` when it was rendered._\n\n"
            f"```mermaid\n{diagram}```\n\n{_table(steps, no_effect, regions, nodes)}")


def write(repo: Repo, wf_id: str) -> Path:
    """Reads the workflow's objective, parsed DAG and build file, writes `docs/mapping.md`, returns its path.
    `FileNotFoundError` for a missing input; see `render` for the rest."""
    objective = objective_lib.load(repo, wf_id)
    dag_path = repo.wf(wf_id, "parsed", "dag.json")
    if not dag_path.is_file():
        raise FileNotFoundError(f"{dag_path}: {wf_id} has no parsed/dag.json; expected parse.py to have run")
    try:
        dag = read_json(dag_path)
    except json.JSONDecodeError as error:
        raise ValueError(f"{dag_path}: not valid JSON ({error}); expected the parser's dag.json") from error
    build = objective_lib.build_path(repo, wf_id)
    if build is None:
        raise FileNotFoundError(f"{repo.wf(wf_id)}: {wf_id} has no build.sql or build.py; expected the translate "
                                f"stage to write one")
    text = render(wf_id, objective, dag, build.read_text(encoding="utf-8"), build.name)
    target = repo.wf(wf_id, "docs", "mapping.md")
    write_text(target, text)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id", help="workflow id, e.g. wf_0001")
    add_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        path = write(Repo(args.root), args.wf_id)
    except MappingError as exc:                      # the inputs disagree: a domain failure, nothing written
        for problem in exc.problems:
            print(f"render_mapping: {args.wf_id}: {problem}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:   # a missing or malformed prerequisite, or a bad id
        print(f"render_mapping: {args.wf_id}: {exc}", file=sys.stderr)
        return 2
    except Exception:                                # a bug: never a bare exit 1
        traceback.print_exc()
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
