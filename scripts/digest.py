"""Write `workflows/<id>/digest.md`: a deterministic, one-line-per-tool reading of a parsed workflow
(objective-first design §4.1).

    digest.py <wf_id> [--root .]

The analyzer reads the digest to write the objective, and the translator consults it for the exact
expressions; neither reads `parsed/dag.json` tool by tool, which on a 60-200 tool workflow would spend
the model's context on JSON. The digest is never written by a model: the same `parsed/dag.json` gives
byte-identical text.

Layout: a header (workflow id, tool count, output count), then one section per output -- an Output
tool, or any other tool nothing reads, in topological order with ties broken by tool id. A tool that
feeds several outputs is listed once, in a leading "Shared" section, and every output's section names
the shared tools it uses by id. One line per tool (a macro's inner tools follow it, indented):

    T3 Filter  [REGION] != "WEST"  ← T2   (T → T5, F → T8)  — Exclude WEST

The line carries the tool id, its type, what decides its behaviour (`lib/tool_lines.config_summary`:
expressions whole, keys, write modes), its inbound tools (`T3.T` is anchor T of tool 3) and, for a tool
with several out anchors, where each goes (`none` for an anchor a Filter, Unique or Join leaves
unwired: those rows are dropped), then its annotation. Containers, comments and Browse tools carry no
data and are counted in the header, not listed.

Exit codes: 0 written (the path is printed); 2 usage -- no `parsed/dag.json`, a workflow id that is
not one, a dag this cannot read.

Nothing here has run on Snowflake or Alteryx: it reads the parsed DAG.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Sequence

from lib.io import read_json, write_text
from lib.paths import Repo, add_root_arg
from lib.tool_lines import Graph, esc, tool_key

LEGEND = ("Each line: `T<id> <Type>  <what it does>  ← <where its rows come from>  "
          "(<anchor> → <where they go>)  — <annotation>`. `T3.T` is anchor T of tool 3. "
          "Expressions are kept whole.")


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _output_name(node: dict) -> str:
    """What an Output tool writes to, for its section's heading: the file name or the table."""
    config = node.get("config") or {}
    if config.get("alias"):
        return str(config.get("table") or f"db:{config['alias']}")
    source = str(config.get("source") or "")
    return source.replace("\\", "/").rsplit("/", 1)[-1]


def render(dag: dict, wf_id: str) -> str:
    """The digest text for a parsed dag (see the module docstring for its layout)."""
    graph = Graph(dag)
    order = graph.order()
    sinks = sorted(graph.sinks(), key=tool_key)
    upstream = {sink: graph.ancestors(sink) for sink in sinks}
    served: dict[str, set[str]] = {tool: set() for tool in order}
    for sink in sinks:
        for tool in upstream[sink] | {sink}:
            served[tool].add(sink)
    shared = [t for t in order if len(served[t]) > 1]
    own = {sink: [t for t in order if served[t] == {sink}] for sink in sinks}
    lost = [t for t in order if not served[t]]          # only a cycle nothing reads: still listed

    outputs = len(sinks)
    lines = [f"# {esc(wf_id)} — digest", "",
             f"{_count(len(order), 'tool')} · {_count(outputs, 'output')} · from parsed/dag.json "
             f"(deterministic; never edited by hand)"]
    if graph.hidden == 1:
        lines.append("1 node that carries no data (a container, comment or browse tool) is not listed.")
    elif graph.hidden:
        lines.append(f"{graph.hidden} nodes that carry no data (containers, comments, browse tools) are not listed.")
    lines += ["", LEGEND]
    constants = sorted((dag.get("constants") or {}).items())
    if constants:
        lines += ["", "## Workflow constants (as parsed; intake may override them)", ""]
        lines += [f"{esc(name)} = {esc(value)}" for name, value in constants]

    def section(heading: str, tools: list[str], uses: list[str] | None = None) -> None:
        lines.extend(["", heading, ""])
        if uses:
            lines.append("Shared tools used: " + ", ".join(f"T{esc(t)}" for t in uses))
        for tool in tools:
            lines.extend(graph.lines(tool))

    if shared:
        section("## Shared by more than one output", shared)
    for sink in sinks:
        node = graph.nodes[sink]
        if node.get("type") == "output":
            name = _output_name(node)
            heading = f"## Output T{esc(sink)}" + (f" · {esc(name)}" if name else "")
        else:
            heading = f"## Terminal stream T{esc(sink)} (no Output tool)"
        section(heading, own[sink], [t for t in shared if t in upstream[sink]])
    if lost:
        section("## Not upstream of any output", lost)
    return "\n".join(lines) + "\n"


def write(repo: Repo, wf_id: str) -> Path:
    """Renders `workflows/<id>/parsed/dag.json` and writes `workflows/<id>/digest.md`; returns its path.
    `FileNotFoundError` when there is no parsed dag."""
    dag_path = repo.wf(wf_id, "parsed", "dag.json")
    if not dag_path.is_file():
        raise FileNotFoundError(f"{wf_id} has no parsed/dag.json; expected parse.py to have run")
    path = repo.wf(wf_id, "digest.md")
    write_text(path, render(read_json(dag_path), wf_id))
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id")
    add_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        path = write(Repo(args.root), args.wf_id)
    except FileNotFoundError as exc:
        print(f"digest: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:                       # a workflow id that is not one (lib.paths)
        print(f"digest: {exc}", file=sys.stderr)
        return 2
    except Exception:                               # a dag this cannot read is exit 2, never a digest
        traceback.print_exc()
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
