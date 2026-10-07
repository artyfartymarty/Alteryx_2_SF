"""Triage a directory of real Alteryx workflow files before any of them enters this pipeline.

    python scripts/survey_corpus.py <dir> [--out report.md] [--json report.json]

Point this at a directory holding a company's real `.yxmd`/`.yxwz`/`.yxzp` exports (any layout;
every such file anywhere under `<dir>` is one workflow) and it reports, per workflow: parse
status, tool counts by target class, unknown plugins by name, unresolved macros, a proposed tier
and the language its build file would be written in (SQL, or Python when a tool needs Snowpark) --
plus a corpus-wide plugin frequency table, so the company knows which Alteryx tools to teach the
parser and cookbook first (docs/reference/real-workflows.md is the how-to for taking one of these
all the way through).

Nothing here runs Alteryx or writes into `workflows/`: every workflow is parsed straight off disk
with `parse.parse_file` (a `.yxzp` is unzipped into a throwaway temp directory first) and never
touches the corpus directory. A parse failure is data, not a crash -- it becomes an `"error"` row
so one bad file never stops the rest of the corpus from being surveyed.

Exit codes follow the plan's Global Constraints as narrowed by this script's own contract: 0
whenever a report is produced, even when some files failed to parse (their rows say so); 2 for a
usage error (no such directory) or a crash outside the survey itself.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Sequence

import parse
from lib.io import write_json
from lib.vocab import DATA_LESS_TYPES
from parsers.plugin_map import node_class

WORKFLOW_SUFFIXES = (".yxmd", ".yxwz", ".yxzp")

# An absolute path of the machine this ran on -- never allowed to reach the report (hand-off
# hygiene): a wrapped exception's own text is the one place a stray absolute path could leak in
# (e.g. a `.yxzp` with no workflow inside names its own temp extraction directory).
_ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]*")


def _sanitize(message: str) -> str:
    return _ABS_PATH_RE.sub("<path>", message)


def _tier(classes: dict[str, int]) -> str:
    """T1 all `sql`; T2 any `snowpark`; T3 any `manual` -- extended here to also cover `unknown`,
    since a plugin the parser cannot name is exactly the case a human has to look at before any
    tier below T3 could be trusted (design spec §2's vocabulary table fixes T1/T2/T3 only for the
    three classes a finished parse can produce; a pre-intake survey also has to place `unknown`)."""
    if classes["manual"] or classes["unknown"]:
        return "T3"
    if classes["snowpark"]:
        return "T2"
    return "T1"


def _expand(nodes: list[dict] | None, prefix: str = "") -> list[tuple[str, dict]]:
    """Every data-carrying node, paired with the id it is known by -- and then, recursively, the
    nodes of a resolved macro's own `sub_dag` under `"<macro_tool_id>/<sub_tool_id>"` (nested
    again for a macro inside a macro).

    A macro is not a tool: it is a workflow someone folded up. `plugin_map` maps the TYPE `macro`
    to `sql` because the node itself transforms nothing, so a Python tool, a Run Command or an
    unknown tool hidden inside one would be invisible to the survey unless this descended."""
    expanded: list[tuple[str, dict]] = []
    for node in nodes or []:
        if node.get("type") in DATA_LESS_TYPES:
            continue
        node_id = f"{prefix}{node['tool_id']}"
        expanded.append((node_id, node))
        sub_dag = node.get("sub_dag")
        if sub_dag:
            expanded.extend(_expand(sub_dag.get("nodes"), f"{node_id}/"))
    return expanded


def _classify(node: dict) -> str:
    """`plugin_map.node_class`, except that an UNRESOLVED macro (`sub_dag: null` -- the `.yxmc`
    was never found) is `unknown`: nothing at all is known about what it does, so calling it
    `sql` would be a guess, and `_expand` has no sub-DAG to descend into."""
    tool_type = node.get("type") or "unknown"
    if tool_type == "macro" and not node.get("sub_dag"):
        return "unknown"
    return node_class(tool_type)


def _language(classes: dict[str, int]) -> str | None:
    """The language a workflow's one build file is written in (objective-first design D1): `python` when any
    tool is `snowpark`-class (the Python tool, which SQL cannot express), else `sql`. `None` for a workflow a
    person has to look at first (any `manual` or `unknown` tool, tier T3): it has no build file to name."""
    if classes["manual"] or classes["unknown"]:
        return None
    return "python" if classes["snowpark"] else "sql"


def _safe_extract(archive: Path, dest: Path) -> None:
    """Unzip `archive` into `dest`, refusing any member that would land outside it (the same
    zip-slip guard `parse._unzip_packages` applies, written fresh here because that function is
    tied to unzipping in place inside a workflow's own `source/`, not into a throwaway temp dir)."""
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise ValueError(f"{archive.name} contains a path outside the package: {member}")
        zf.extractall(dest)


def _parse_one(path: Path) -> dict:
    """One workflow's dag, however it is packaged. A `.yxzp` is extracted to a throwaway temp
    directory first (never into the corpus directory itself) and its workflow found with
    `parse.workflow_file`, exactly as `parse.run` finds one after unpacking in place."""
    if path.suffix.lower() != ".yxzp":
        dag, _aliases = parse.parse_file(path)
        return dag
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _safe_extract(path, tmp_dir)
        workflow_path = parse.workflow_file(tmp_dir)
        dag, _aliases = parse.parse_file(workflow_path)
        return dag


def _discover(directory: Path) -> list[Path]:
    return sorted((p for p in directory.rglob("*")
                  if p.is_file() and p.suffix.lower() in WORKFLOW_SUFFIXES),
                 key=lambda p: p.relative_to(directory).as_posix())


def _build_row(rel: str, expanded: list[tuple[str, dict]]) -> dict:
    classes = {c: 0 for c in ("sql", "snowpark", "manual", "unknown")}
    for _, node in expanded:
        classes[_classify(node)] += 1

    unknown_plugins = sorted({node["plugin"] for _, node in expanded
                              if node["type"] == "unknown" and node.get("plugin")})
    unresolved_macros = sorted({node["macro_path"] for _, node in expanded
                                if node["type"] == "macro" and node.get("unresolved")
                                and node.get("macro_path")})

    return {
        "path": rel,
        "status": "ok",
        "tools": len(expanded),
        "classes": classes,
        "unknown_plugins": unknown_plugins,
        "unresolved_macros": unresolved_macros,
        "tier": _tier(classes),
        "language": _language(classes),
    }


# --- the survey ---

def survey(directory: str | Path) -> dict:
    """Every `.yxmd`/`.yxwz`/`.yxzp` under `directory`, one report row each, plus a corpus-wide
    plugin frequency table.

    Deterministic: file discovery is sorted by relative path and every node traversal follows the
    dag's own (parse-order) sequence, so two calls over the same directory produce byte-identical
    JSON.
    """
    directory = Path(directory)
    workflows: list[dict] = []
    plugin_counts: dict[tuple[str, str], int] = {}

    for path in _discover(directory):
        rel = path.relative_to(directory).as_posix()
        try:
            dag = _parse_one(path)
        except Exception as exc:  # a parse failure is data for this row, not a crash of the survey
            workflows.append({"path": rel, "status": "error",
                              "error": _sanitize(f"{type(exc).__name__}: {exc}")})
            continue

        expanded = _expand(dag["nodes"])
        for _, node in expanded:
            plugin = node.get("plugin")
            if plugin:
                key = (plugin, node["type"])
                plugin_counts[key] = plugin_counts.get(key, 0) + 1
        workflows.append(_build_row(rel, expanded))

    plugins = [{"plugin": plugin, "type": tool_type, "count": count}
              for (plugin, tool_type), count in plugin_counts.items()]
    plugins.sort(key=lambda e: (-e["count"], e["plugin"], e["type"]))

    return {"workflows": workflows, "plugins": plugins}


def render_markdown(report: dict) -> str:
    lines = ["# Real-corpus survey", ""]
    lines.append(f"{len(report['workflows'])} workflow file(s) found.")
    lines.append("")
    lines.append("| path | status | tier | language | tools | sql | snowpark | manual | "
                 "unknown | unknown plugins | unresolved macros |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for wf in report["workflows"]:
        if wf["status"] == "error":
            lines.append(f"| {wf['path']} | error | | | | | | | | | {wf['error']} |")
            continue
        c = wf["classes"]
        lines.append(
            f"| {wf['path']} | ok | {wf['tier']} | {wf['language'] or '-'} | {wf['tools']} | "
            f"{c['sql']} | {c['snowpark']} | {c['manual']} | {c['unknown']} | "
            f"{', '.join(wf['unknown_plugins']) or '-'} | "
            f"{', '.join(wf['unresolved_macros']) or '-'} |")
    lines.append("")
    lines.append("## Plugin frequency")
    lines.append("")
    lines.append("Teach `scripts/parsers/plugin_map.py` (or write a parser-recovery extension "
                 "under `scripts/parsers/ext/`, `scripts/parsers/ext/README.md`) the plugins "
                 "with `type: unknown` here first -- they block every workflow that uses them.")
    lines.append("")
    lines.append("| plugin | type | count |")
    lines.append("|---|---|---|")
    for p in report["plugins"]:
        lines.append(f"| {p['plugin']} | {p['type']} | {p['count']} |")
    lines.append("")
    return "\n".join(lines)


# --- CLI ---

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="a directory of real .yxmd/.yxwz/.yxzp files")
    parser.add_argument("--out", help="write the markdown report here")
    parser.add_argument("--json", help="write the JSON report here")
    args = parser.parse_args(argv)

    directory = Path(args.directory)
    if not directory.is_dir():
        parser.error(f"{args.directory} is not a directory")

    try:
        report = survey(directory)
    except Exception:  # a crash outside the per-file survey itself, which writes nothing
        traceback.print_exc()
        return 2

    ok = sum(1 for w in report["workflows"] if w["status"] == "ok")
    errors = len(report["workflows"]) - ok
    print(f"{len(report['workflows'])} workflow(s) surveyed: {ok} ok, {errors} error, "
         f"{len(report['plugins'])} distinct plugin(s)")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_markdown(report))
    if args.json:
        write_json(Path(args.json), report)
    return 0


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
