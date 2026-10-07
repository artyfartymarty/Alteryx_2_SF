"""Check a workflow's objective.json before any later stage reads it (objective-first design §4.2).

    objective_check.py <wf_id> [--ready] [--root .]

Every problem is printed on its own line to stderr as `<field>: <problem>; expected <what>` -- the exact
text the orchestrator records and shows the analyzer on its retry, and what the analyzer sees when it
runs this script itself:

    steps: tool 12 is in no step and not in no_effect; expected every tool id of parsed/dag.json exactly once

Three kinds of rule:

1. **Mechanical agreement** with `objective_scaffold.py`, derived afresh in memory (nothing is written):
   `workflow`; every input's and output's `tool_id` and `logical`; every output's `write_mode`, `pre_sql`
   and `post_sql`; every column's name (in order) and type; an `update_insert` output's `keys`. A variable-
   length string may be spelled `VARCHAR(<size>)` or the type map's unsized `VARCHAR`; a target that keeps
   its rows (`append`, `update_insert`) may list target-only columns after the stream's own.
2. **Shape of what scripts read**: `title`, `purpose` and every output's `grain` non-empty; `tier` one of
   `T1`/`T2`/`T3` and `language` one of `sql`/`python`; `language_reason` a sentence when the language is
   `python` and null when it is `sql`; a `snowpark`-class tool (`parsers.plugin_map.node_class`, macro
   internals included) forces `python` and a `manual`-class tool forces `T3`; every step has an id
   `s<N>`, a name, a description, tools and feeds, each feed another step (never the step's own id) or
   `out:<output tool id>`; every tool of `parsed/dag.json` is in exactly one step's `tools` or in
   `no_effect` -- containers, comments, Browse tools and the other data-less nodes
   (`lib.vocab.DATA_LESS_TYPES`) are exempt and need not be in either -- an Input tool is never in
   `no_effect` (it belongs in the step that reads it), and every output's tool is inside a step; every
   column has a boolean `nullable`, every output column a `meaning`; every entry's `keys` are a list of its
   own columns.
3. **Judgment well-formed**, with the vocabularies of `lib/judgment.py`: per
   output `row_relation` one of `1:1`, `filter`, `aggregate`, `expand`; `ordering` exactly `{keys,
   alteryx_deterministic, order_dependent_columns}` (`keys` any column the workflow carries,
   `order_dependent_columns` output columns); `tolerances` keyed by output column, each `{float_abs,
   float_rel}` non-negative numbers; `normalizations` `trim:<COLUMN>` / `upper:<COLUMN>` on output columns;
   `parity_risks` exactly `{tool_id, class, note}` naming a tool of the workflow (or `<macro>/<tool>`) and a
   class of `lib.vocab.DIFF_CLASSES`; `rules` non-empty sentences; `questions` and `answered` with ids
   `O<N>` (unique across both), each question with the tools it is about, its text and its assumption.

`--ready` adds what golden needs: every input's and output's `logical` non-null (intake has named it) and
`questions` empty. If `objective.md` exists its fixed headings are checked too; its absence is the
orchestrator's missing-output check, not this one's.

Exit codes: 0 the objective passes; 1 any problem (a missing or unreadable objective.json included); 2
usage (no `parsed/dag.json`, a workflow id that is not one) or a crash. Writes nothing.

Nothing here has run on Snowflake or Alteryx: it compares JSON the analyzer wrote with JSON the pipeline
derived.
"""
from __future__ import annotations

import argparse
import re
import sys
import traceback
from typing import Sequence

import objective_scaffold as osc
from lib.judgment import (NORMALIZATION_OPS, NORMALIZATIONS_SHAPE, ORDERING_FIELDS, ORDERING_SHAPE, RISK_FIELDS,
                          ROW_RELATIONS, TOLERANCE_KEYS, TOLERANCE_SHAPE, TYPE_GRAMMAR, TYPE_SHAPE,
                          is_number, names, shown)
from lib.mechanical import nested_tool_key
from lib.objective import LANGUAGES, QUESTION_ID, STEP_ID, TIERS, WRITE_MODES
from lib.paths import Repo, add_root_arg
from lib.vocab import DIFF_CLASSES

#: The headings of `objective.md`, in order (design §4.2); the first is `# <id> — <title>`.
HEADINGS = ("## Purpose", "## Inputs", "## Outputs", "## Steps", "## Business rules",
            "## Alteryx behaviour to keep", "## Questions")

#: What each list-valued key holds, for "expected a list ..." problems.
LIST_KEYS = {"inputs": "with one entry per Input tool", "outputs": "with one entry per Output tool",
             "steps": "of steps {id, name, does, tools, feeds}", "no_effect": "of {tool_id, reason}",
             "rules": "of non-empty sentences", "parity_risks": "of {tool_id, class, note} (may be empty)",
             "questions": "of open questions {id, tools, question, assumption} (may be empty)",
             "answered": "of answered questions {id, question, answer} (may be empty)"}


class _Problems(list):
    def add(self, field: str, problem: str, expected: str) -> None:
        self.append(f"{field}: {problem}; expected {expected}")

    def wrong(self, field: str, value, expected: str) -> None:
        self.add(field, f"is {shown(value)}", expected)

    def missing(self, field: str, expected: str) -> None:
        self.add(field, "is missing", expected)

    def bad(self, owner: dict, key: str, field: str, expected: str) -> None:
        """`missing` when `owner` has no `key`, else `wrong` with the value it has."""
        if key in owner:
            self.wrong(field, owner[key], expected)
        else:
            self.missing(field, expected)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _check_text(p: _Problems, owner: dict, key: str, field: str, expected: str = "a non-empty string") -> None:
    if not _text(owner.get(key)):
        p.bad(owner, key, field, expected)


def _check_bool(p: _Problems, owner: dict, key: str, field: str) -> None:
    if not isinstance(owner.get(key), bool):
        p.bad(owner, key, field, "true or false")


# --- identity, language and tier ------------------------------------------------------------------------

def _check_header(p: _Problems, wf_id: str, objective: dict, facts: osc.DagFacts) -> None:
    if objective.get("workflow") != wf_id:
        p.bad(objective, "workflow", "workflow", shown(wf_id))
    _check_text(p, objective, "title", "title")
    _check_text(p, objective, "purpose", "purpose")
    tier, language = objective.get("tier"), objective.get("language")
    if tier not in TIERS:
        p.bad(objective, "tier", "tier", "one of " + ", ".join(f'"{t}"' for t in TIERS))
    if language not in LANGUAGES:
        p.bad(objective, "language", "language", "one of " + ", ".join(f'"{x}"' for x in LANGUAGES))
    reason = objective.get("language_reason")
    if "language_reason" not in objective:
        p.missing("language_reason", "null (language sql) or a sentence saying why SQL cannot express the logic (python)")
    elif language == "python" and not _text(reason):
        p.add("language_reason", f"is {shown(reason)} but language is python",
              "a sentence saying why SQL cannot express the logic")
    elif language == "sql" and not _blank(reason):
        p.add("language_reason", f"is {shown(reason)} but language is sql", "null")
    if language in LANGUAGES and language != "python" and facts.classes.get("snowpark"):
        tool_id, tool_type = facts.classes["snowpark"][0]
        p.add("language", f"is {shown(language)} but tool {tool_id} ({tool_type}) is a snowpark-class tool", '"python"')
    if tier in TIERS and tier != "T3" and facts.classes.get("manual"):
        tool_id, tool_type = facts.classes["manual"][0]
        p.add("tier", f"is {shown(tier)} but tool {tool_id} ({tool_type}) is a manual-class tool", '"T3"')


# --- steps and tool accounting ---------------------------------------------------------------------------

def _check_steps(p: _Problems, objective: dict, facts: osc.DagFacts, input_ids: list[str],
                 output_ids: list[str]) -> None:
    steps = objective.get("steps")
    if not isinstance(steps, list):
        p.bad(objective, "steps", "steps", f"a list {LIST_KEYS['steps']}")
        return
    known = set(facts.tool_ids)
    step_ids = {s["id"] for s in steps if isinstance(s, dict) and isinstance(s.get("id"), str)
                and STEP_ID.fullmatch(s["id"])}
    seen_ids: set[str] = set()
    placed: dict[str, list[str]] = {}
    for index, step in enumerate(steps):
        where = f"steps[{index}]"
        if not isinstance(step, dict):
            p.wrong(where, step, "an object {id, name, does, tools, feeds}")
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not STEP_ID.fullmatch(sid):
            p.bad(step, "id", f"{where}.id", "s<N> such as s1")
        elif sid in seen_ids:
            p.add(f"{where}.id", f"{shown(sid)} is used by more than one step", "unique step ids")
        else:
            seen_ids.add(sid)
        _check_text(p, step, "name", f"{where}.name")
        _check_text(p, step, "does", f"{where}.does")
        tools = step.get("tools")
        if not isinstance(tools, list) or not tools:
            p.bad(step, "tools", f"{where}.tools", "a non-empty list of tool ids")
        else:
            for position, tool in enumerate(tools):
                if not isinstance(tool, str) or tool not in known:
                    p.add(f"{where}.tools[{position}]", f"is {shown(tool)}, not a tool of parsed/dag.json",
                          "a tool id of the DAG")
                else:
                    placed.setdefault(tool, []).append(sid if isinstance(sid, str) else where)
        feeds = step.get("feeds")
        if not isinstance(feeds, list):
            p.bad(step, "feeds", f"{where}.feeds", "a list of step ids and out:<output tool id>")
        else:
            for feed in feeds:
                if isinstance(sid, str) and feed == sid:
                    p.add(f"{where}.feeds", "names its own step", "later steps or out:<output tool id>")
                else:
                    _check_feed(p, f"{where}.feeds", feed, step_ids, output_ids)
    no_effect = objective.get("no_effect")
    if isinstance(no_effect, list):
        for index, entry in enumerate(no_effect):
            where = f"no_effect[{index}]"
            if not isinstance(entry, dict):
                p.wrong(where, entry, "an object {tool_id, reason}")
                continue
            tool = entry.get("tool_id")
            if not isinstance(tool, str) or tool not in known:
                p.add(f"{where}.tool_id", f"is {shown(tool)}, not a tool of parsed/dag.json", "a tool id of the DAG")
            else:
                placed.setdefault(tool, []).append("no_effect")
                if tool in input_ids:
                    p.add(where, f"tool {tool} is an Input tool", "it in the step that reads it")
            _check_text(p, entry, "reason", f"{where}.reason", "a non-empty reason")
    else:
        p.bad(objective, "no_effect", "no_effect", f"a list {LIST_KEYS['no_effect']}")
    for tool in facts.tool_ids:
        places = placed.get(tool, [])
        if not places and tool not in facts.exempt:
            p.add("steps", f"tool {tool} is in no step and not in no_effect",
                  "every tool id of parsed/dag.json exactly once")
        elif len(places) > 1:
            p.add("steps", f"tool {tool} is listed more than once (steps {', '.join(places)})",
                  "every tool id of parsed/dag.json exactly once")


def _check_feed(p: _Problems, field: str, feed, step_ids: set[str], output_ids: list[str]) -> None:
    expected = "a step id or out:<output tool id>"
    if isinstance(feed, str) and feed.startswith("out:"):
        if feed[4:] not in output_ids:
            p.add(field, f"{shown(feed)} is not an output tool", expected)
    elif not isinstance(feed, str) or feed not in step_ids:
        p.add(field, f"{shown(feed)} is not a step", expected)


# --- inputs and outputs ----------------------------------------------------------------------------------

def _label(section: str, position: int, tool_id) -> str:
    return f"{section}[{position}] (tool {tool_id})"


def _check_entries(p: _Problems, section: str, objective: dict, derivation: osc.Derivation,
                   facts: osc.DagFacts) -> None:
    """The list's shape and which entry is which, then each entry the DAG shows."""
    derived = derivation.objective[section]
    ids = [e["tool_id"] for e in derived]
    noun = "Input" if section == "inputs" else "Output"
    written = objective.get(section)
    if not isinstance(written, list):
        p.bad(objective, section, section, f"a list {LIST_KEYS[section]}: {names(ids)}")
        return
    positions: dict[str, int] = {}
    for position, entry in enumerate(written):
        if not isinstance(entry, dict):
            p.wrong(f"{section}[{position}]", entry, "an object")
            continue
        tool = str(entry.get("tool_id"))
        if tool not in ids:
            p.add(section, f"tool {entry.get('tool_id')} is not an {noun} tool of parsed/dag.json",
                  f"only: {names(ids)}")
        elif tool in positions:
            p.add(section, f"tool {tool} has more than one entry", "one entry per tool")
        else:
            positions[tool] = position
    for tool in ids:
        if tool not in positions:
            p.add(section, f"has no entry for tool {tool}", f"one entry per {noun} tool: {names(ids)}")
    as_written = [tool for tool, _ in sorted(positions.items(), key=lambda item: item[1])]
    if as_written != [tool for tool in ids if tool in positions]:
        p.add(section, f"are in the order {names(as_written)}", names(t for t in ids if t in positions))
    for index, mine in enumerate(derived):
        position = positions.get(mine["tool_id"])
        if position is not None:
            _check_entry(p, section, index, position, written[position], mine, derivation, facts)


def _compare(p: _Problems, where: str, entry: dict, key: str, want) -> None:
    if key not in entry:
        p.missing(f"{where}.{key}", shown(want))
    elif entry[key] != want:
        p.wrong(f"{where}.{key}", entry[key], shown(want))


def _check_entry(p: _Problems, section: str, index: int, position: int, entry: dict, mine: dict,
                 derivation: osc.Derivation, facts: osc.DagFacts) -> None:
    where = _label(section, position, mine["tool_id"])
    outputs = section == "outputs"
    _compare(p, where, entry, "tool_id", mine["tool_id"])      # a number where the DAG's id is a string
    _compare(p, where, entry, "logical", mine["logical"])
    if outputs:
        if mine["write_mode"] is None:
            if entry.get("write_mode") not in WRITE_MODES:      # not derivable: the analyzer declares it
                p.bad(entry, "write_mode", f"{where}.write_mode", "one of " + ", ".join(f'"{m}"' for m in WRITE_MODES))
        else:
            _compare(p, where, entry, "write_mode", mine["write_mode"])
        _compare(p, where, entry, "pre_sql", mine["pre_sql"])
        _compare(p, where, entry, "post_sql", mine["post_sql"])
        if mine["write_mode"] == "update_insert" and mine["keys"]:
            _compare(p, where, entry, "keys", mine["keys"])
    column_names = _check_columns(p, where, mine, entry, section, index, derivation)
    keys = entry.get("keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        p.bad(entry, "keys", f"{where}.keys", "a list of its column names (may be empty)")
    else:
        upper = {n.upper() for n in column_names}
        for key in keys:
            if key.upper() not in upper:
                p.add(f"{where}.keys", f"names {key}, which is not one of its columns",
                      f"a subset of {names(column_names)}")
    if outputs:
        _check_output_judgment(p, where, entry, column_names, facts, derivation.objective["workflow"])


def _check_columns(p: _Problems, where: str, mine: dict, entry: dict, section: str, index: int,
                   derivation: osc.Derivation) -> list[str]:
    """Columns against the derivation (or, when it has none, their shape only); returns their names."""
    outputs = section == "outputs"
    fields = "{name, type, nullable" + (", meaning" if outputs else "") + "}"
    theirs = entry.get("columns")
    if not isinstance(theirs, list):
        p.bad(entry, "columns", f"{where}.columns", f"a list of {fields} objects")
        return []
    for position, column in enumerate(theirs):
        if not isinstance(column, dict):
            p.wrong(f"{where}.columns[{position}]", column, f"a {fields} object")
    column_names = [str(c.get("name")) for c in theirs if isinstance(c, dict)]
    declared = (section, index) not in derivation.undeclared
    derived = {c["name"]: (i, c) for i, c in enumerate(mine["columns"])}
    accepted = {c: s for (sec, e, c), s in derivation.also_accepted.items() if (sec, e) == (section, index)}
    if declared:
        wanted = [c["name"] for c in mine["columns"]]
        extras = outputs and index in derivation.extras_allowed
        head = column_names[:len(wanted)] if extras else column_names
        if head != wanted:
            tail = " followed by any column only the existing target table has" if extras else ""
            p.add(f"{where}.columns", f"are named [{', '.join(column_names)}]", f"[{', '.join(wanted)}]{tail}")
    for position, column in enumerate(theirs):
        if not isinstance(column, dict):
            continue
        path = f"{where}.columns[{position}] ({column.get('name')})"
        if not _text(column.get("name")):
            p.wrong(f"{where}.columns[{position}].name", column.get("name"), "a column name")
        at = derived.get(str(column.get("name")))
        if declared and at is not None and "type" in at[1]:
            want = at[1]["type"]
            also = sorted(accepted.get(at[0], ()))
            if column.get("type") != want and column.get("type") not in also:
                p.bad(column, "type", f"{path}.type", shown(want) + "".join(f" (or {shown(a)})" for a in also))
        elif not isinstance(column.get("type"), str) or not TYPE_GRAMMAR.match(column["type"]):
            # No derived type to compare with (a derivation gap, a target-only column, an entry the DAG has no
            # field list for): the shape compile_check.py can build a table from.
            p.bad(column, "type", f"{path}.type", TYPE_SHAPE)
        _check_bool(p, column, "nullable", f"{path}.nullable")
        if outputs and not _text(column.get("meaning")):
            p.bad(column, "meaning", f"{path}.meaning", "what the column means, in words")
    return column_names


def _check_output_judgment(p: _Problems, where: str, entry: dict, column_names: list[str], facts: osc.DagFacts,
                           wf_id: str) -> None:
    _check_text(p, entry, "grain", f"{where}.grain", "a non-empty sentence")
    if entry.get("row_relation") not in ROW_RELATIONS:
        p.bad(entry, "row_relation", f"{where}.row_relation", "one of " + ", ".join(f'"{r}"' for r in ROW_RELATIONS))
    upper = [n.upper() for n in column_names]
    _check_ordering(p, where, entry, upper, column_names, facts, wf_id)
    _check_tolerances(p, where, entry, upper, column_names)
    _check_normalizations(p, where, entry, upper, column_names)


def _check_ordering(p: _Problems, where: str, entry: dict, upper: list[str], column_names: list[str],
                    facts: osc.DagFacts, wf_id: str) -> None:
    field = f"{where}.ordering"
    ordering = entry.get("ordering")
    if not isinstance(ordering, dict):
        p.bad(entry, "ordering", field, ORDERING_SHAPE)
        return
    keys = ordering.get("keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        p.bad(ordering, "keys", f"{field}.keys", "a list of column names (may be empty)")
    else:
        data = set(facts.columns) | set(upper)
        for key in keys:
            if key.upper() not in data:
                p.add(f"{field}.keys", f"names {key}, which is not a column of {wf_id}",
                      "a column of its inputs, its tools' anchors or its outputs")
    _check_bool(p, ordering, "alteryx_deterministic", f"{field}.alteryx_deterministic")
    dependent = ordering.get("order_dependent_columns")
    if not isinstance(dependent, list) or not all(isinstance(k, str) for k in dependent):
        p.bad(ordering, "order_dependent_columns", f"{field}.order_dependent_columns",
              "a list of output column names (may be empty)")
    else:
        for name in dependent:
            if name.upper() not in upper:
                p.add(f"{field}.order_dependent_columns", f"names {name}, which is not an output column",
                      f"one of {names(column_names)}")
    for key in ordering:
        if key not in ORDERING_FIELDS:
            p.add(f"{field}.{key}", "is not an ordering field",
                  "only keys, alteryx_deterministic and order_dependent_columns")


def _check_tolerances(p: _Problems, where: str, entry: dict, upper: list[str], column_names: list[str]) -> None:
    tolerances = entry.get("tolerances")
    if not isinstance(tolerances, dict):
        p.bad(entry, "tolerances", f"{where}.tolerances", "an object keyed by output column (may be empty: {})")
        return
    for name, directive in tolerances.items():
        if str(name).upper() not in upper:
            p.add(f"{where}.tolerances.{name}", "names no output column", f"one of {names(column_names)}")
            continue
        ok = (isinstance(directive, dict) and set(directive) <= TOLERANCE_KEYS
              and all(is_number(v) and v >= 0 for v in directive.values()))
        if not ok:
            p.wrong(f"{where}.tolerances.{name}", directive, TOLERANCE_SHAPE)


def _check_normalizations(p: _Problems, where: str, entry: dict, upper: list[str], column_names: list[str]) -> None:
    normalizations = entry.get("normalizations")
    if not isinstance(normalizations, list):
        p.bad(entry, "normalizations", f"{where}.normalizations", NORMALIZATIONS_SHAPE)
        return
    for index, directive in enumerate(normalizations):
        operation, _, name = str(directive).partition(":") if isinstance(directive, str) else ("", "", "")
        if operation.strip().lower() not in NORMALIZATION_OPS or not name.strip():
            p.wrong(f"{where}.normalizations[{index}]", directive, '"trim:<COLUMN>" or "upper:<COLUMN>"')
        elif name.strip().upper() not in upper:
            p.add(f"{where}.normalizations[{index}]", f"names {name.strip()}, which is not an output column",
                  f"one of {names(column_names)}")


# --- rules, parity risks, questions -----------------------------------------------------------------------

def _check_rules(p: _Problems, objective: dict) -> None:
    rules = objective.get("rules")
    if not isinstance(rules, list):
        p.bad(objective, "rules", "rules", f"a list {LIST_KEYS['rules']}")
        return
    for index, rule in enumerate(rules):
        if not _text(rule):
            p.wrong(f"rules[{index}]", rule, "a non-empty sentence")


def _check_risks(p: _Problems, wf_id: str, objective: dict, facts: osc.DagFacts) -> None:
    risks = objective.get("parity_risks")
    if not isinstance(risks, list):
        p.bad(objective, "parity_risks", "parity_risks", f"a list {LIST_KEYS['parity_risks']}")
        return
    tools = sorted(set(facts.tool_ids) | facts.nested, key=nested_tool_key)
    shape = f'{{"tool_id": "<a tool of {wf_id}>", "class": "<diff class>", "note": "<why>"}}'
    for index, risk in enumerate(risks):
        where = f"parity_risks[{index}]"
        if not isinstance(risk, dict):
            p.wrong(where, risk, shape)
            continue
        if not isinstance(risk.get("tool_id"), str) or risk["tool_id"] not in set(tools):
            p.bad(risk, "tool_id", f"{where}.tool_id", f"a tool of {wf_id}: {names(tools)}")
        if risk.get("class") not in DIFF_CLASSES:
            p.bad(risk, "class", f"{where}.class", f"one of {', '.join(DIFF_CLASSES)}")
        _check_text(p, risk, "note", f"{where}.note", "a non-empty sentence")
        for key in risk:
            if key not in RISK_FIELDS:
                p.add(f"{where}.{key}", "is not a parity-risk field", "only tool_id, class and note")


def _check_questions(p: _Problems, objective: dict, facts: osc.DagFacts) -> None:
    known = set(facts.tool_ids) | facts.nested
    seen: set[str] = set()
    for key, shape, texts in (("questions", "an object {id, tools, question, assumption}", ("question", "assumption")),
                              ("answered", "an object {id, question, answer}", ("question", "answer"))):
        entries = objective.get(key)
        if not isinstance(entries, list):
            p.bad(objective, key, key, f"a list {LIST_KEYS[key]}")
            continue
        for index, entry in enumerate(entries):
            where = f"{key}[{index}]"
            if not isinstance(entry, dict):
                p.wrong(where, entry, shape)
                continue
            qid = entry.get("id")
            if not isinstance(qid, str) or not QUESTION_ID.fullmatch(qid):
                p.bad(entry, "id", f"{where}.id", "O<N> such as O1")
            elif qid in seen:
                p.add(f"{where}.id", f"{shown(qid)} is used by more than one question", "unique question ids")
            else:
                seen.add(qid)
            if key == "questions":
                tools = entry.get("tools")
                if not isinstance(tools, list):
                    p.bad(entry, "tools", f"{where}.tools", "a list of the tool ids the question is about")
                else:
                    for position, tool in enumerate(tools):
                        if not isinstance(tool, str) or tool not in known:
                            p.add(f"{where}.tools[{position}]", f"is {shown(tool)}, not a tool of parsed/dag.json",
                                  "a tool id of the DAG")
            for text in texts:
                _check_text(p, entry, text, f"{where}.{text}")


def _check_ready(p: _Problems, objective: dict) -> None:
    for section in ("inputs", "outputs"):
        for position, entry in enumerate(objective.get(section) if isinstance(objective.get(section), list) else []):
            if isinstance(entry, dict) and entry.get("logical") is None:
                p.add(_label(section, position, entry.get("tool_id")) + ".logical", "is null",
                      "the logical name intake mapped to it (intake/mappings.yaml names it once intake is READY)")
    questions = objective.get("questions")
    if isinstance(questions, list) and questions:
        ids = ", ".join(str(q.get("id")) if isinstance(q, dict) else "?" for q in questions)
        many = len(questions) != 1
        p.add("questions", f"{len(questions)} question{'s' if many else ''} {'are' if many else 'is'} still open ({ids})",
              "none: every question answered and moved to answered")


# --- objective.md -------------------------------------------------------------------------------------------

def _check_markdown(p: _Problems, repo: Repo, wf_id: str) -> None:
    path = repo.wf(wf_id, "objective.md")
    if not path.is_file():
        return
    lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()]
    first = next((line for line in lines if line.strip()), "")
    if not re.match(rf"#\s+{re.escape(wf_id)}\b", first):
        p.add("objective.md", f"the first heading is '{first}'", f"'# {wf_id} — <title>'")
    found = [line for line in lines if line in HEADINGS]
    missing = [heading for heading in HEADINGS if heading not in found]
    for heading in missing:
        p.add("objective.md", f"heading '{heading}' is missing",
              f"the fixed headings: # {wf_id} — <title>, " + ", ".join(HEADINGS))
    if not missing and found != list(HEADINGS):
        p.add("objective.md", "the headings are out of order", ", ".join(HEADINGS))


# --- the whole check ------------------------------------------------------------------------------------------

def check(repo: Repo, wf_id: str, ready: bool = False) -> list[str]:
    """Every problem of `wf_id`'s objective, in a fixed order: identity and language, steps, inputs, outputs,
    parity risks, rules, questions, then (with `ready`) what golden needs and objective.md's headings.
    Raises FileNotFoundError without `parsed/dag.json`."""
    derivation = osc.derive(repo, wf_id)
    try:
        objective = osc.read_objective(repo, wf_id)
    except osc.ObjectiveUnreadable as exc:
        return [str(exc)]

    p = _Problems()
    facts = derivation.facts
    input_ids = [e["tool_id"] for e in derivation.objective["inputs"]]
    output_ids = [e["tool_id"] for e in derivation.objective["outputs"]]
    _check_header(p, wf_id, objective, facts)
    _check_steps(p, objective, facts, input_ids, output_ids)
    _check_entries(p, "inputs", objective, derivation, facts)
    _check_entries(p, "outputs", objective, derivation, facts)
    steps = objective.get("steps")
    if isinstance(steps, list):
        in_steps = {t for s in steps if isinstance(s, dict) and isinstance(s.get("tools"), list) for t in s["tools"]
                    if isinstance(t, str)}   # a non-string entry is already named by _check_steps
        outputs = objective.get("outputs") if isinstance(objective.get("outputs"), list) else []
        for position, entry in enumerate(outputs):
            tool = str(entry.get("tool_id")) if isinstance(entry, dict) else None
            if tool in output_ids and tool not in in_steps:
                p.add(_label("outputs", position, tool), "is in no step", "every output's tool inside a step")
    _check_risks(p, wf_id, objective, facts)
    _check_rules(p, objective)
    _check_questions(p, objective, facts)
    if ready:
        _check_ready(p, objective)
    _check_markdown(p, repo, wf_id)
    return list(p)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id")
    parser.add_argument("--ready", action="store_true",
                        help="also require every logical name and no open questions (what golden needs)")
    add_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        problems = check(Repo(args.root), args.wf_id, ready=args.ready)
    except (FileNotFoundError, ValueError) as exc:          # exit 2: nothing to check against
        print(f"objective_check: {exc}", file=sys.stderr)
        return 2
    except Exception:                                       # exit 2: a crash, never a verdict
        traceback.print_exc()
        return 2
    if not problems:
        print(f"{args.wf_id}: objective passes every check" + (", including --ready" if args.ready else ""))
        return 0
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{args.wf_id}: {len(problems)} problem{'' if len(problems) == 1 else 's'} in objective.json")
    return 1


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
