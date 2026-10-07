"""Derive the mechanical fields of a workflow's objective.json (objective-first design §4.2).

    objective_scaffold.py <wf_id> (--prefill | --apply) [--root .]

An objective has two kinds of field. The MECHANICAL ones follow from files the pipeline already wrote
-- `parsed/dag.json` and `intake/mappings.yaml` -- and this script derives them; the JUDGMENT ones are
the analyzer's. The mechanical fields:

* `workflow`.
* `inputs[]`: one entry per Input tool -- and per tool `intake/mappings.yaml` maps as a source -- in
  tool-id order: `tool_id`, `logical` (the mapping's logical name for the tool, `null` until intake has
  named it), `columns[].name` / `.type` (the producing anchor's RecordInfo: names upper-cased, types
  from `lib.types_map`, a variable-length string keeping its DAG size as `VARCHAR(<size>)`).
* `outputs[]`: one entry per Output tool, in tool-id order: `tool_id`, `logical`, `write_mode`
  (the mapping's `mode` -- `overwrite`, `append`, and `merge` as `update_insert` -- else the Output
  tool's own), `pre_sql` / `post_sql` (the Output tool's PreSQL and PostSQL), `columns[].name` /
  `.type` (the anchor that feeds it) and, when the write mode is `update_insert`, `keys` (the
  mapping's, else the Output tool's update keys, upper-cased).

Everything else is judgment: `title`, `purpose`, `tier`, `language`, `language_reason`, each input's
`keys`, every column's `nullable`, each output's `grain`, `row_relation`, `ordering`, `tolerances`,
`normalizations` and (unless it is an `update_insert`) `keys`, every column's `meaning`, `steps`,
`no_effect`, `rules`, `parity_risks`, `questions`, `answered`. The derivations are the ones
the per-segment contract scaffold made, now in `lib/mechanical.py`.

Modes (exactly one):
  --prefill   write the skeleton where `objective.json` does not exist yet (the orchestrator, before the
              analyzer; a resumed run keeps the analyzer's own file): mechanical fields filled, every
              judgment field present and empty (`""`, `[]`, `{}`, `null`; `tier` and `language` `null`;
              `nullable` the neutral `true`; an `ordering` with empty lists and an undecided
              `alteryx_deterministic`).
  --apply     re-apply the mechanical fields over the existing file (the orchestrator, after the
              analyzer, and again once intake has named the tables): authoritative, and it touches
              nothing else -- every judgment field stays exactly as written, other keys the analyzer
              added included. An input or output the analyzer dropped is restored from the skeleton and
              one it invented is removed; a column it dropped is restored and one it added is removed
              (judgment fields of a surviving column are kept, matched by name); a target that keeps its
              existing rows (`append`, `update_insert`) may carry target-only columns after the stream's
              own. The file is rewritten only when something changed.

A field that cannot be derived -- a type the type map does not cover, an anchor with no field list, an
Output tool with no write mode -- is reported as a `note:` line on stderr, and left as it is: it is the
analyzer's to declare. `intake/mappings.yaml` may be absent (analyze starts before intake is READY): every
`logical` is then `null`.

Exit codes: 0 done; 1 (`--apply`) no `objective.json`, or one that is not a JSON object -- the first
stderr line names it, and nothing is written; 2 usage (no `parsed/dag.json`, a workflow id that is not one,
no or two modes) or a crash, with nothing written.

Nothing here has run on Snowflake or Alteryx: it reads JSON and YAML the pipeline wrote.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Sequence

from lib.io import read_json, read_yaml, write_json
from lib.mechanical import KEEPS_ROWS, WRITE_MODES, anchor_fields, by_tool, columns_from_meta, nested_ids, tool_key
from lib.objective import dag_tool_ids, objective_path
from lib.paths import Repo, add_root_arg
from lib.vocab import DATA_LESS_TYPES
from parsers.plugin_map import node_class


class ObjectiveUnreadable(ValueError):
    """An objective.json that cannot be read as an objective: missing, not JSON, not a JSON object, or (to
    `--apply`) of a shape the re-apply cannot read. The message is already a problem line, `objective.json:
    <problem>; expected <what>`, with no machine path in it."""


_JSON_TYPES = {list: "array", dict: "object", str: "string", bool: "boolean", int: "number", float: "number",
               type(None): "null"}


def read_objective(repo: Repo, wf_id: str) -> dict:
    """`workflows/<id>/objective.json` as a dict; ObjectiveUnreadable (`objective.json: ...; expected ...`)
    when it is missing, not JSON, or not a JSON object. The three objective scripts read it this way."""
    path = objective_path(repo, wf_id)
    if not path.is_file():
        raise ObjectiveUnreadable(f"objective.json: is missing; expected the analyzer's objective for {wf_id}")
    try:
        data = read_json(path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ObjectiveUnreadable(f"objective.json: not valid JSON ({exc}); expected a JSON object") from exc
    if not isinstance(data, dict):
        raise ObjectiveUnreadable(f"objective.json: is a JSON {_JSON_TYPES.get(type(data), type(data).__name__)}; "
                                  f"expected a JSON object")
    return data


@dataclass(frozen=True)
class DagFacts:
    """What the checker needs to know about the parsed DAG besides the mechanical fields."""
    #: every top-level tool id, in the DAG's order, as strings (`lib.objective.dag_tool_ids`).
    tool_ids: tuple[str, ...]
    #: top-level tools of a data-less type (container, comment, interface, action, browse): a step need
    #: not account for them.
    exempt: frozenset[str]
    #: every `<macro>/<tool>` id inside a macro (a parity risk or a question may name one).
    nested: frozenset[str]
    #: `plugin_map.node_class` -> `(tool id, type)` of every tool (macro-internal ones included) of that class.
    classes: dict[str, tuple[tuple[str, str], ...]]
    #: every column name (upper-cased) any anchor of any tool carries.
    columns: frozenset[str]


def dag_facts(dag: dict) -> DagFacts:
    ids = dag_tool_ids(dag)
    nodes = {str(n["tool_id"]): n for n in dag.get("nodes") or []}
    classes: dict[str, list[tuple[str, str]]] = {}
    columns: set[str] = set()

    def walk(level: list, prefix: str) -> None:
        for node in level:
            tool_id = f"{prefix}{node['tool_id']}"
            classes.setdefault(node_class(node.get("type")), []).append((tool_id, str(node.get("type"))))
            for fields in (node.get("meta") or {}).values():
                columns.update(str(f.get("name")).upper() for f in fields or [])
            walk((node.get("sub_dag") or {}).get("nodes") or [], f"{tool_id}/")

    walk(list(dag.get("nodes") or []), "")
    return DagFacts(
        tool_ids=tuple(ids),
        exempt=frozenset(i for i in ids if nodes[i].get("type") in DATA_LESS_TYPES),
        nested=frozenset(inner for node in nodes.values() for inner in nested_ids(node, f"{node['tool_id']}/")),
        classes={name: tuple(found) for name, found in classes.items()},
        columns=frozenset(columns))


@dataclass
class Derivation:
    """The skeleton `--prefill` writes, and what re-applying and checking need to know about it that
    cannot be put in the objective itself."""
    objective: dict
    facts: DagFacts
    #: (section, entry index, column index) -> other spellings accepted for that column's type.
    also_accepted: dict[tuple[str, int, int], frozenset[str]] = field(default_factory=dict)
    #: (section, entry index) of an entry whose anchor has no field list: its columns are the analyzer's.
    undeclared: set[tuple[str, int]] = field(default_factory=set)
    #: indexes of `outputs[]` entries that may carry target-only columns after the stream's own.
    extras_allowed: set[int] = field(default_factory=set)
    #: derivation gaps, one line each: what the analyzer has to declare itself.
    notes: list[str] = field(default_factory=list)


def derive(repo: Repo, wf_id: str) -> Derivation:
    """The mechanical fields of `wf_id`'s objective. Raises FileNotFoundError when `parsed/dag.json` does
    not exist; `intake/mappings.yaml` is optional (every `logical` is then null)."""
    dag_path = repo.wf(wf_id, "parsed", "dag.json")
    if not dag_path.is_file():
        raise FileNotFoundError(f"{wf_id} has no parsed/dag.json; expected parse.py to have run")
    dag = read_json(dag_path)
    mappings_path = repo.wf(wf_id, "intake", "mappings.yaml")
    mappings = (read_yaml(mappings_path) if mappings_path.is_file() else None) or {}
    facts = dag_facts(dag)

    nodes = {str(node["tool_id"]): node for node in dag.get("nodes") or []}
    sources = by_tool(mappings.get("sources"))
    targets = by_tool(mappings.get("outputs"))
    edge_into: dict[str, dict] = {}
    for edge in dag.get("edges") or []:
        edge_into.setdefault(str(edge.get("dst")), edge)

    derivation = Derivation(objective={}, facts=facts)
    notes = derivation.notes

    def columns_of(section: str, index: int, fields: list | None, where: str) -> list[dict]:
        if fields is None:
            notes.append(f"{where}: the DAG has no field list for it; the analyzer declares its columns")
            derivation.undeclared.add((section, index))
            return []
        columns, accepted = columns_from_meta(fields, where, notes)
        for column_index, spellings in accepted.items():
            derivation.also_accepted[(section, index, column_index)] = spellings
        return columns

    inputs: list[dict] = []
    for tool_id in sorted((i for i, node in nodes.items() if node.get("type") == "input" or i in sources), key=tool_key):
        where = f"inputs[{len(inputs)}] (tool {tool_id})"
        logical = (sources.get(tool_id) or {}).get("logical")
        if not logical:
            notes.append(f"{where}: intake/mappings.yaml maps no logical name to it; logical stays null until intake names it")
        inputs.append({"tool_id": tool_id, "logical": str(logical) if logical else None,
                       "columns": columns_of("inputs", len(inputs), anchor_fields(nodes.get(tool_id), None), where),
                       "keys": []})

    outputs: list[dict] = []
    for tool_id in sorted((i for i, node in nodes.items() if node.get("type") == "output"), key=tool_key):
        index = len(outputs)
        where = f"outputs[{index}] (tool {tool_id})"
        config = nodes[tool_id].get("config") or {}
        mapping = targets.get(tool_id) or {}
        logical = mapping.get("logical")
        if not logical:
            notes.append(f"{where}: intake/mappings.yaml maps no logical name to it; logical stays null until intake names it")
        mode = WRITE_MODES.get(str(mapping.get("mode"))) or config.get("write_mode") or None
        if mode is None:
            notes.append(f"{where}: neither intake/mappings.yaml nor the Output tool names a write mode")
        keys: list[str] = []
        if mode == "update_insert":
            keys = [str(k).upper() for k in (mapping.get("keys") or config.get("keys") or [])]
            if not keys:
                notes.append(f"{where}: write mode update_insert, but neither intake/mappings.yaml nor the Output "
                             f"tool lists update keys; the analyzer declares them")
        if mode in KEEPS_ROWS:
            derivation.extras_allowed.add(index)
        feed = edge_into.get(tool_id)
        if feed is not None:
            fields = anchor_fields(nodes.get(str(feed.get("src"))), str(feed.get("src_anchor")))
        else:
            notes.append(f"{where}: no edge feeds Output tool {tool_id}; its own field list is used")
            fields = anchor_fields(nodes[tool_id], None)
        columns = columns_of("outputs", index, fields, where)
        for column in columns:
            column["meaning"] = ""
        outputs.append({
            "tool_id": tool_id, "logical": str(logical) if logical else None, "write_mode": mode,
            "pre_sql": config.get("pre_sql") or None, "post_sql": config.get("post_sql") or None,
            "grain": "", "columns": columns, "keys": keys, "row_relation": None,
            "ordering": {"keys": [], "alteryx_deterministic": None, "order_dependent_columns": []},
            "tolerances": {}, "normalizations": []})

    derivation.objective = {
        "workflow": wf_id, "title": "", "purpose": "", "tier": None, "language": None, "language_reason": None,
        "inputs": inputs, "outputs": outputs, "steps": [], "no_effect": [], "rules": [], "parity_risks": [],
        "questions": [], "answered": []}
    return derivation


# --- re-applying ----------------------------------------------------------------------------------

def _match(derived: list[dict], written: list) -> list[dict | None]:
    """For each skeleton entry, the written entry it corresponds to (or None), each written entry used at
    most once: by tool id first, then -- for an entry whose tool id the analyzer lost or got wrong -- by
    logical name."""
    pool = [(i, e) for i, e in enumerate(written) if isinstance(e, dict)]
    used: set[int] = set()
    found: list[int | None] = [None] * len(derived)

    def take(index: int, predicate) -> None:
        match = next((i for i, e in pool if i not in used and predicate(e)), None)
        if match is not None:
            used.add(match)
            found[index] = match

    for index, mine in enumerate(derived):
        take(index, lambda e, t=str(mine["tool_id"]): e.get("tool_id") is not None and str(e["tool_id"]) == t)
    for index, mine in enumerate(derived):
        if found[index] is None and mine.get("logical"):
            take(index, lambda e, name=str(mine["logical"]).upper():
                 isinstance(e.get("logical"), str) and e["logical"].upper() == name)
    return [written[i] if i is not None else None for i in found]


def _apply_columns(derived: list[dict], written, accepted: dict[int, frozenset[str]], extras: bool,
                   meaning: bool) -> list[dict]:
    theirs = [c for c in written if isinstance(c, dict)] if isinstance(written, list) else []
    by_name: dict[str, dict] = {}
    for column in theirs:
        by_name.setdefault(str(column.get("name")).upper(), column)
    out: list[dict] = []
    for index, mine in enumerate(derived):
        other = by_name.pop(mine["name"], None)
        if other is None:
            out.append(copy.deepcopy(mine))
            continue
        column = copy.deepcopy(other)
        column["name"] = mine["name"]
        if "type" in mine:
            spelled = other.get("type")
            # Only a string can be an accepted spelling; anything else (a list, an object) is replaced
            # by the derived type rather than looked up in a set.
            column["type"] = spelled if isinstance(spelled, str) and spelled in accepted.get(index, ()) else mine["type"]
        # `nullable` is judgment: a column the analyzer kept without one is left without one, so
        # objective_check.py names it instead of a silent `true` passing for a NOT NULL column.
        if meaning:
            column.setdefault("meaning", "")
        out.append(column)
    if extras:
        derived_names = {c["name"] for c in derived}
        out.extend(copy.deepcopy(c) for c in theirs if str(c.get("name")).upper() not in derived_names)
    return out


def _apply_entry(mine: dict, theirs: dict | None, section: str, index: int, derivation: Derivation) -> dict:
    if theirs is None:
        return copy.deepcopy(mine)
    out = copy.deepcopy(theirs)
    out["tool_id"] = mine["tool_id"]
    out["logical"] = mine["logical"]
    if section == "outputs":
        if mine["write_mode"] is not None:              # not derivable: the analyzer's to declare
            out["write_mode"] = mine["write_mode"]
        out["pre_sql"] = mine["pre_sql"]
        out["post_sql"] = mine["post_sql"]
        if mine["write_mode"] == "update_insert" and mine["keys"]:
            out["keys"] = list(mine["keys"])
    if (section, index) not in derivation.undeclared:
        accepted = {c: spellings for (s, e, c), spellings in derivation.also_accepted.items() if (s, e) == (section, index)}
        out["columns"] = _apply_columns(mine["columns"], theirs.get("columns"), accepted,
                                        extras=section == "outputs" and index in derivation.extras_allowed,
                                        meaning=section == "outputs")
    return out


def apply(objective: dict, derivation: Derivation) -> dict:
    """`objective` with every mechanical field re-applied from `derivation` (pure). See the module
    docstring for what is kept, restored and removed."""
    derived = derivation.objective
    new = copy.deepcopy(objective)
    new["workflow"] = derived["workflow"]
    for section in ("inputs", "outputs"):
        written = objective.get(section) if isinstance(objective.get(section), list) else []
        matched = _match(derived[section], written)
        new[section] = [_apply_entry(mine, theirs, section, index, derivation)
                        for index, (mine, theirs) in enumerate(zip(derived[section], matched))]
    return new


def prefill(repo: Repo, wf_id: str, derivation: Derivation | None = None) -> bool:
    """Writes the skeleton as objective.json when there is none yet; True when it wrote."""
    path = objective_path(repo, wf_id)
    if path.exists():
        return False
    write_json(path, (derivation or derive(repo, wf_id)).objective)
    return True


def apply_file(repo: Repo, wf_id: str, derivation: Derivation | None = None) -> bool:
    """Re-applies the mechanical fields over the existing objective.json, rewriting it only when
    something changed; True when it did. Raises ObjectiveUnreadable -- before writing anything -- for a
    missing file, one that is not a JSON object, or a shape the re-apply cannot read."""
    derivation = derivation or derive(repo, wf_id)
    objective = read_objective(repo, wf_id)
    try:
        new = apply(objective, derivation)
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        # A shape `apply` did not foresee is still the analyzer's objective, never a crash of this
        # script: exit 1 with the file named, and objective_check.py names the fields.
        raise ObjectiveUnreadable(f"objective.json: has a shape the re-apply cannot read "
                                  f"({type(exc).__name__}: {exc}); expected an objective "
                                  f"scripts/objective_check.py can check") from exc
    if new == objective:
        return False
    write_json(objective_path(repo, wf_id), new)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prefill", action="store_true", help="write the skeleton where objective.json does not exist")
    mode.add_argument("--apply", action="store_true", help="re-apply the mechanical fields over objective.json")
    add_root_arg(parser)
    args = parser.parse_args(argv)
    repo = Repo(args.root)

    try:
        derivation = derive(repo, args.wf_id)
    except (FileNotFoundError, ValueError) as exc:          # exit 2, nothing written
        print(f"objective_scaffold: {exc}", file=sys.stderr)
        return 2
    except Exception:                                       # exit 2: a crash, never a verdict
        traceback.print_exc()
        return 2
    def print_notes() -> None:
        # Only after a success: on a failure the first stderr line must be the failure itself, because the
        # orchestrator records that line as the park reason (stages.ts objectiveFailure).
        for note in derivation.notes:
            print(f"note: {note}", file=sys.stderr)

    try:
        if args.prefill:
            wrote = prefill(repo, args.wf_id, derivation)
            print_notes()
            print(f"{args.wf_id}: wrote objective.json (mechanical fields filled, judgment fields empty)" if wrote
                  else f"{args.wf_id}: objective.json already exists; kept as it is")
            return 0
        changed = apply_file(repo, args.wf_id, derivation)
        print_notes()
        print(f"{args.wf_id}: re-applied the mechanical fields; objective.json {'rewritten' if changed else 'unchanged'}")
        return 0
    except ObjectiveUnreadable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
