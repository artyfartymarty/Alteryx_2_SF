"""Per-tool text lines for a model's context, shared by `prompt_context.py` (intake's DAG summary)
and `digest.py` (the analyzer's and translator's reading of a whole workflow).

Every value that reaches a line was written by whoever built the original Alteryx workflow -- an
annotation, a field name, an expression -- and flows verbatim into an agent's task text, so one
`esc` makes it line-safe (control characters, line and paragraph separators and bidirectional
overrides become backslash escapes) and every line below goes through it. The two renderers share
the escaping, the tool ordering, the data-less filter and the per-tool DAG lines; the digest adds
`config_summary` (what decides a tool's behaviour) and `Graph` (the order, the sections and the
`T<id> <Type> ...` line).

Nothing here has run on Snowflake or Alteryx: it reads a parsed `dag.json`.
"""
from __future__ import annotations

import heapq
import json
import re
import unicodedata

from .vocab import DATA_LESS_TYPES

# Fix round 1 (I2a): every C0 control character is escaped to its backslash-letter form, so no
# workflow-authored value can ever start a new line, a blank line, or anything else that would
# make it look like the boundary of the sentence it is embedded in. `\r`/`\n`/`\t` keep these
# ordinary spellings; everything else in the categories below is spelled `\uXXXX` (fix round 2).
_CONTROL_ESCAPES = {"\r": "\\r", "\n": "\\n", "\t": "\\t"}

# Fix round 2 (I2a): C0/C1 controls are category `Cc` (this already covered `\x00`-`\x1f`/`\x7f`,
# but not U+0085 NEL, also `Cc`, which `str.splitlines()` treats as a line break just like `\n` --
# the reviewer's own probe substituted it, and others below, for `\n` in the fix-round-1 fixtures
# and got a `USER:`/`SYSTEM:` line back). U+2028 LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR are
# categories `Zl`/`Zp` -- `splitlines()` treats both as breaks too, and neither is `Cc`, so the old
# `ord(ch) < 0x20` check never saw them at all. The bidirectional override/isolate controls
# (U+202A-U+202E, U+2066-U+2069) are category `Cf`, not `Cc`/`Zl`/`Zp`, and don't break a line --
# but a right-to-left override can make rendered text visually read as something the literal
# characters do not say, so they are escaped too, defense in depth against the same "looks like
# something it isn't" family of attack the fence and the one-line-per-value rule exist for.
_ESCAPED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
_BIDI_CONTROLS = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))


def esc(value: object) -> str:
    text = str(value)
    out: list[str] = []
    for ch in text:
        if ch in _CONTROL_ESCAPES:
            out.append(_CONTROL_ESCAPES[ch])
        elif unicodedata.category(ch) in _ESCAPED_CATEGORIES or ord(ch) in _BIDI_CONTROLS:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def field_names(fields: list[dict] | None) -> str:
    """The `name` of every field of an anchor's meta, line-safe, comma-separated."""
    return ", ".join(esc(f["name"]) for f in fields or [])


def tool_key(tool_id: str) -> tuple[int, int, str]:
    """The same ordering `compare.py::_tool_sort_key` uses: numeric tool ids sort numerically,
    anything else (a macro's nested `"<id>/<id>"`) sorts after, alphabetically. The one copy of
    it in `lib/`: `lib/mechanical.py` imports it (and `nested_tool_key` builds on it).

    Fix round 1 (M3): kept as a local copy rather than `from compare import _tool_sort_key` --
    `compare.py` imports `lib.backend.DuckDBBackend`, which imports `duckdb` and `sqlglot`.
    Measured directly: importing this module alone costs ~33 ms; importing `compare` costs
    ~109 ms, more than 3x, for a two-line function. `prompt_context.py` is a script the
    orchestrator runs on every intake and every analyze, for every workflow, specifically to save
    a model's context window cheaply -- paying a compiled-extension import for one comparator
    function would work against that same goal. No cycle risk either way: nothing imports
    `prompt_context`, so it isn't a correctness concern, only a cost one.
    """
    text = str(tool_id)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def dag_lines_by_tool(dag: dict) -> dict[str, str]:
    """`dag_summary_lines`' line for every data-carrying top-level tool, keyed by tool id, in tool
    order. Inbound edges are indexed once (Task W2: a batched workflow is large by definition, and
    scanning every edge for every node was quadratic in the DAG's size)."""
    inbound: dict[str, list[str]] = {}
    for e in dag.get("edges") or []:
        inbound.setdefault(e["dst"], []).append(f"{esc(e['dst_anchor'])} <- {esc(e['src'])}.{esc(e['src_anchor'])}")
    nodes = {n["tool_id"]: n for n in dag.get("nodes") or []}
    lines: dict[str, str] = {}
    for node in sorted(nodes.values(), key=lambda n: tool_key(n["tool_id"])):
        if node.get("type") in DATA_LESS_TYPES:
            continue
        outs = [f"{esc(anchor)}[{field_names(fields)}]" for anchor, fields in (node.get("meta") or {}).items()]
        label_text = f' "{esc(node["annotation"][:60])}"' if node.get("annotation") else ""
        macro = " (macro; its sub-DAG is in parsed/dag.json)" if node.get("type") == "macro" else ""
        lines[node["tool_id"]] = (f"- tool {esc(node['tool_id'])} {esc(node['type'])}{label_text}{macro}: "
                                  f"in {', '.join(inbound.get(node['tool_id'], [])) or 'none'}; "
                                  f"out {' '.join(outs) or 'none'}")
    return lines


def dag_summary_lines(dag: dict, tool_ids=None) -> list[str]:
    """One line per data-carrying top-level tool: id, type, its annotation (if any), the
    macro it needs a reader to expand (never expanded inline here -- the file it names is the
    source of truth), its inbound edges and its named output columns per anchor. With `tool_ids`,
    only those tools' lines; their inbound edges still name every source."""
    return [line for tool, line in dag_lines_by_tool(dag).items() if tool_ids is None or tool in tool_ids]


# --- the digest's lines (`digest.py`, objective-first design §4.1) ---------------------------------

#: A tool type's name as Alteryx's own tool palette spells it. A type not listed is capitalised word
#: by word (`some_tool` -> `SomeTool`), never dropped.
TYPE_LABELS = {
    "input": "Input", "output": "Output", "select": "Select", "filter": "Filter", "formula": "Formula",
    "join": "Join", "union": "Union", "summarize": "Summarize", "sort": "Sort", "unique": "Unique",
    "sample": "Sample", "record_id": "RecordID", "multi_row_formula": "MultiRowFormula",
    "cross_tab": "CrossTab", "transpose": "Transpose", "regex": "RegEx", "datetime": "DateTime",
    "append_fields": "AppendFields", "block_until_done": "BlockUntilDone", "browse": "Browse",
    "run_command": "RunCommand", "python": "Python", "macro_input": "MacroInput",
    "macro_output": "MacroOutput", "data_cleansing": "DataCleansing", "macro": "Macro",
    "unknown": "Unknown",
}

#: Types whose out anchors split the stream, so an anchor nothing reads is information (the rows that
#: leave on it are dropped): a line names it as `none`. Other types list only the anchors they feed.
_SPLITTERS = frozenset({"filter", "unique", "join"})

#: Alteryx field types whose size says something about the data (a string's width truncates, a
#: decimal's precision rounds); every other type's size is a storage hint and is left out.
_SIZED_TYPES = frozenset({"String", "WString", "V_String", "V_WString", "FixedDecimal"})


def label(tool_type: object) -> str:
    text = str(tool_type or "")
    return TYPE_LABELS.get(text) or "".join(part.capitalize() for part in text.split("_")) or "Unknown"


def type_text(field_type, size, scale=None) -> str:
    """`V_String(10)`, `FixedDecimal(19,2)`, `Double`; empty when the config names no type."""
    if not field_type:
        return ""
    if field_type in _SIZED_TYPES and size:
        return f"{field_type}({size},{scale})" if scale else f"{field_type}({size})"
    return str(field_type)


def _typed(name, field_type, size, scale=None) -> str:
    text = type_text(field_type, size, scale)
    return f"{esc(name)}:{esc(text)}" if text else esc(name)


def _basename(path) -> str:
    return re.split(r"[\\/]+", str(path))[-1] if path else ""


def _list(values) -> str:
    return ", ".join(esc(v) for v in values or [])


def _input(node: dict, c: dict) -> str:
    if c.get("alias"):
        parts = [f"read db:{esc(c['alias'])}"]
        if c.get("query"):
            parts.append(f"query: {esc(c['query'])}")
    else:
        details = [esc(c["format"])] if c.get("format") else []
        csv = c.get("csv")
        if csv:
            if csv.get("delimiter") is not None:
                details.append(f'delimiter "{esc(csv["delimiter"])}"')
            details.append("header row" if csv.get("header") else "no header row")
            if csv.get("codepage"):
                details.append(f"codepage {esc(csv['codepage'])}")
        name = esc(_basename(c.get("source")) or "(no source)")
        parts = [f"read {name}" + (f" ({', '.join(details)})" if details else "")]
    if c.get("record_limit") is not None:
        parts.append(f"first {esc(c['record_limit'])} records")
    return "; ".join(parts)


def _output(node: dict, c: dict) -> str:
    if c.get("alias"):
        target = f"db:{esc(c['alias'])}" + (f" table {esc(c['table'])}" if c.get("table") else "")
    else:
        target = esc(_basename(c.get("source")) or "(no target)") + (f" ({esc(c['format'])})" if c.get("format") else "")
    head = f"write {target}, mode {esc(c.get('write_mode') or '?')}"
    if c.get("keys"):
        head += f", keys {_list(c['keys'])}"
    parts = [head]
    if c.get("pre_sql"):
        parts.append(f"PreSQL: {esc(c['pre_sql'])}")
    if c.get("post_sql"):
        parts.append(f"PostSQL: {esc(c['post_sql'])}")
    return "; ".join(parts)


def _field_changes(select: dict, include_unchanged: bool) -> list[str]:
    """What a Select (or a Join's output selection) does to its fields: each renamed or retyped field
    (every selected one when the unlisted fields are dropped, since the list is then the whole
    output), then the deselected ones."""
    items: list[str] = []
    drops: list[str] = []
    for field in select.get("fields") or []:
        if not field.get("selected"):
            drops.append(esc(field.get("name")))
            continue
        text = esc(field.get("name"))
        if field.get("rename"):
            text += f" → {esc(field['rename'])}"
        kind = type_text(field.get("type"), field.get("size"))
        if kind:
            text += f":{esc(kind)}"
        if field.get("rename") or field.get("type") or include_unchanged:
            items.append(text)
    return items + ([f"drop {', '.join(drops)}"] if drops else [])


def _select(node: dict, c: dict) -> str:
    kept = bool(c.get("unknown_selected"))
    tail = "other fields kept" if kept else "other fields dropped"
    return "; ".join(_field_changes(c, include_unchanged=not kept) + [tail])


def _join(node: dict, c: dict) -> str:
    keys = [f"Left.{esc(k.get('left'))} = Right.{esc(k.get('right'))}" for k in c.get("keys") or []]
    select = c.get("select") or {}
    parts = ["on " + " and ".join(keys) if keys else "no join keys"]
    parts += _field_changes(select, include_unchanged=False)
    if select and not select.get("unknown_selected", True):
        parts.append("other fields dropped")
    return "; ".join(parts)


def _filter(node: dict, c: dict) -> str:
    return esc(c["expression"]) if c.get("expression") else "(no expression)"


def _formula(node: dict, c: dict) -> str:
    return "; ".join(f"{_typed(f.get('field'), f.get('type'), f.get('size'), f.get('scale'))} = {esc(f.get('expression'))}"
                     for f in c.get("formulas") or [])


def _union(node: dict, c: dict) -> str:
    return {"name": "by name", "position": "by position"}.get(c.get("mode"), "mode ?")


def _summarize(node: dict, c: dict) -> str:
    groups: list[str] = []
    actions: list[str] = []
    for field in c.get("fields") or []:
        name, rename = esc(field.get("field")), field.get("rename")
        as_text = f" as {esc(rename)}" if rename and rename != field.get("field") else ""
        if field.get("action") == "GroupBy":
            groups.append(name + as_text)
            continue
        extra = f', sep "{esc(field["separator"])}"' if field.get("separator") is not None else ""
        actions.append(f"{esc(field.get('action'))}({name}{extra}){as_text}")
    return "; ".join(([f"group by {', '.join(groups)}"] if groups else []) + actions)


def _sort(node: dict, c: dict) -> str:
    return ", ".join(f"{esc(f.get('field'))} {esc(f.get('order') or '?')}" for f in c.get("fields") or [])


def _unique(node: dict, c: dict) -> str:
    return f"keys {_list(c.get('fields'))}"


def _sample(node: dict, c: dict) -> str:
    n = esc(c.get("n"))
    text = {"first": f"first {n} rows", "last": f"last {n} rows", "skip": f"skip first {n} rows",
            "one_in_n": f"1 in every {n} rows"}.get(c.get("mode"), f"mode ? n={n}")
    return text + (f"; group by {_list(c['group_by'])}" if c.get("group_by") else "")


def _record_id(node: dict, c: dict) -> str:
    return (f"new field {_typed(c.get('field'), c.get('type'), None)} starting at {esc(c.get('start'))}, "
            f"position {esc(c.get('position') or '?')}")


def _multi_row_formula(node: dict, c: dict) -> str:
    verb = "update field" if c.get("update_existing") else "new field"
    parts = [f"{verb} {_typed(c.get('field'), c.get('type'), c.get('size'))} = {esc(c.get('expression'))}"]
    if c.get("num_rows") is not None:
        parts.append(f"rows {esc(c['num_rows'])}")
    if c.get("group_by"):
        parts.append(f"group by {_list(c['group_by'])}")
    if c.get("unknown_rows"):
        parts.append(f"missing rows → {esc(c['unknown_rows'])}")
    return "; ".join(parts)


def _cross_tab(node: dict, c: dict) -> str:
    return (f"group by {_list(c.get('group_by'))}; header {esc(c.get('header_field'))}; "
            f"data {esc(c.get('data_field'))}; methods {_list(c.get('methods'))}")


def _transpose(node: dict, c: dict) -> str:
    return f"key fields {_list(c.get('key_fields'))}; data fields {_list(c.get('data_fields'))}"


def _regex(node: dict, c: dict) -> str:
    parts = [f"{esc(c.get('method') or 'regex')} on {esc(c.get('field'))}: expression \"{esc(c.get('expression'))}\""]
    if c.get("replace") is not None:
        parts.append(f"replacement \"{esc(c['replace'])}\"")
    parts.append("case-insensitive" if c.get("case_insensitive") else "case-sensitive")
    if c.get("copy_unmatched"):
        parts.append("copy unmatched")
    if c.get("output_fields"):
        parts.append("output fields " + ", ".join(_typed(f.get("name"), f.get("type"), f.get("size"))
                                                  for f in c["output_fields"]))
    if c.get("match_field"):
        parts.append(f"match into {esc(c['match_field'])}")
    return "; ".join(parts)


def _datetime(node: dict, c: dict) -> str:
    return (f"{esc(c.get('direction'))} {esc(c.get('field'))} with format \"{esc(c.get('format'))}\" "
            f"→ {esc(c.get('out_field'))}")


#: Data Cleansing's options in the macro's own order (it applies them in this order), in words.
_CLEANSING = (
    ("replace_null_strings_blank", "replace null text with blank"),
    ("replace_null_numeric_zero", "replace null numbers with 0"),
    ("trim_whitespace", "trim whitespace"),
    ("remove_tabs_linebreaks_dupspaces", "remove tabs, line breaks and duplicate spaces"),
    ("remove_all_whitespace", "remove all whitespace"),
    ("remove_letters", "remove letters"),
    ("remove_numbers", "remove numbers"),
    ("remove_punctuation", "remove punctuation"),
)


def _data_cleansing(node: dict, c: dict) -> str:
    options = [words for key, words in _CLEANSING if c.get(key)]
    if c.get("modify_case"):
        options.append(f"{esc(c['modify_case'])} case")
    return f"fields {_list(c.get('fields')) or '(none)'}: {', '.join(options) or 'no options'}"


def _macro(node: dict, c: dict) -> str:
    name = esc(_basename(node.get("macro_path")) or "macro")
    if node.get("unresolved"):
        name += " (file not found)"
    defaults = {q.get("name"): q.get("default") for q in node.get("interface") or [] if isinstance(q, dict)}
    parts = [name]
    for question, value in (c.get("values") or {}).items():
        default = f" (default {esc(defaults[question])})" if question in defaults else ""
        parts.append(f"{esc(question)} = {esc(value)}{default}")
    return "; ".join(parts)


def _run_command(node: dict, c: dict) -> str:
    return f"command {esc(c.get('command'))}" + (f"; arguments {esc(c['args'])}" if c.get("args") else "")


def _python(node: dict, c: dict) -> str:
    return f"script: {esc(c.get('script') or '')}"


def _pairs(c: dict) -> str:
    return ", ".join(f"{esc(k)}={esc(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))}"
                     for k, v in c.items())


def _unknown(node: dict, c: dict) -> str:
    parts = [f"plugin {esc(node['plugin'])}"] if node.get("plugin") else []
    if c:
        parts.append(_pairs(c))
    elif node.get("raw_config"):
        parts.append("raw XML: " + esc(" ".join(str(node["raw_config"]).split())))
    return "; ".join(parts)


_SUMMARIES = {
    "input": _input, "output": _output, "select": _select, "filter": _filter, "formula": _formula,
    "join": _join, "union": _union, "summarize": _summarize, "sort": _sort, "unique": _unique,
    "sample": _sample, "record_id": _record_id, "multi_row_formula": _multi_row_formula,
    "cross_tab": _cross_tab, "transpose": _transpose, "regex": _regex, "datetime": _datetime,
    "data_cleansing": _data_cleansing, "macro": _macro, "run_command": _run_command, "python": _python,
    "unknown": _unknown,
}


def config_summary(node: dict) -> str:
    """What decides one tool's behaviour, on one line (design §4.1): the filter expression, each
    formula field and its expression, join keys, summarize group-bys and actions, sort keys, select
    renames and retypes, union mode, unique keys, sample settings, multi-row expressions, cross-tab and
    transpose keys, input and output names with their write mode, a macro's name and answers. Long
    expressions are kept whole -- they are the logic. A type this module has no reading for lists its
    configuration as `key=value`; a tool with nothing configured gives the empty string."""
    config = node.get("config") or {}
    reader = _SUMMARIES.get(node.get("type"))
    if reader is not None:
        return reader(node, config)
    return _pairs(config)


def _rank(anchors: list, name) -> int:
    return anchors.index(name) if name in anchors else len(anchors)


class Graph:
    """The data-carrying tools of one DAG level -- the top level, or a macro's `sub_dag` -- and the edges
    between them, indexed once. Containers, comments, Browse tools and the like carry no data and are
    not in it (`hidden` counts them); an edge to or from one is ignored.

    Everything is ordered by tool id (`tool_key`) and then by anchor, so the same DAG gives the same
    order whatever order its nodes and edges were written in.
    """

    def __init__(self, dag: dict):
        everything = list(dag.get("nodes") or [])
        data = [n for n in everything if n.get("type") not in DATA_LESS_TYPES]
        self.hidden = len(everything) - len(data)
        self.nodes: dict[str, dict] = {str(n["tool_id"]): n
                                       for n in sorted(data, key=lambda n: tool_key(str(n["tool_id"])))}
        self.inbound: dict[str, list[dict]] = {i: [] for i in self.nodes}
        self.outbound: dict[str, list[dict]] = {i: [] for i in self.nodes}
        for edge in dag.get("edges") or []:
            src, dst = str(edge["src"]), str(edge["dst"])
            if src in self.nodes and dst in self.nodes:
                self.inbound[dst].append(edge)
                self.outbound[src].append(edge)
        for tool_id, edges in self.inbound.items():
            anchors = self.nodes[tool_id].get("in_anchors") or []
            edges.sort(key=lambda e: (_rank(anchors, e["dst_anchor"]), e.get("dst_order") or 0,
                                      tool_key(str(e["src"])), str(e["src_anchor"])))
        for tool_id, edges in self.outbound.items():
            anchors = self.nodes[tool_id].get("out_anchors") or []
            edges.sort(key=lambda e: (_rank(anchors, e["src_anchor"]), tool_key(str(e["dst"]))))
        self.preds = {i: {str(e["src"]) for e in edges} for i, edges in self.inbound.items()}
        self.succs = {i: {str(e["dst"]) for e in edges} for i, edges in self.outbound.items()}

    def order(self) -> list[str]:
        """A topological order, ties broken by tool id; tools left in a cycle follow in tool-id order
        rather than being dropped."""
        waiting = {i: len(p) for i, p in self.preds.items()}
        ready = [(tool_key(i), i) for i, n in waiting.items() if n == 0]
        heapq.heapify(ready)
        done: list[str] = []
        while ready:
            _, tool_id = heapq.heappop(ready)
            done.append(tool_id)
            for nxt in self.succs[tool_id]:
                waiting[nxt] -= 1
                if waiting[nxt] == 0:
                    heapq.heappush(ready, (tool_key(nxt), nxt))
        placed = set(done)
        return done + [i for i in self.nodes if i not in placed]

    def ancestors(self, tool_id: str) -> set[str]:
        """Every tool upstream of `tool_id` (itself only when it is in a cycle)."""
        seen: set[str] = set()
        stack = list(self.preds[tool_id])
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(self.preds[current])
        return seen

    def sinks(self) -> list[str]:
        """The tools a section is written for: every Output tool, and any other tool nothing reads."""
        return [i for i, n in self.nodes.items() if n.get("type") == "output" or not self.succs[i]]

    def line(self, tool_id: str, prefix: str = "") -> str:
        """`T<id> <Type>  <config>  ← <inbound>   (<anchor> → <consumers>)  — <annotation>`; a part with
        nothing to say is left out. `prefix` is a macro's id and a slash, for a tool inside it."""
        node = self.nodes[tool_id]
        text = f"T{esc(prefix)}{esc(tool_id)} {label(node.get('type'))}"
        config = config_summary(node)
        if config:
            text += f"  {config}"
        inbound = self._inbound_text(tool_id, node, prefix)
        if inbound:
            text += f"  ← {inbound}"
        outbound = self._outbound_text(tool_id, node, prefix)
        if outbound:
            text += f"   ({outbound})"
        if node.get("annotation"):
            text += f"  — {esc(node['annotation'])}"
        return text

    def lines(self, tool_id: str, prefix: str = "", depth: int = 0) -> list[str]:
        """The tool's line, then (for a macro) its inner tools' lines, indented and numbered under it."""
        out = ["  " * depth + self.line(tool_id, prefix)]
        inner = self.nodes[tool_id].get("sub_dag") or {}
        if inner.get("nodes"):
            graph = Graph(inner)
            for inner_id in graph.order():
                out.extend(graph.lines(inner_id, f"{prefix}{tool_id}/", depth + 1))
        return out

    def _inbound_text(self, tool_id: str, node: dict, prefix: str) -> str:
        sides = len(node.get("in_anchors") or []) > 1
        items = []
        for edge in self.inbound[tool_id]:
            source = self.nodes[str(edge["src"])]
            ref = f"T{esc(prefix)}{esc(edge['src'])}"
            if len(source.get("out_anchors") or []) > 1:
                ref += f".{esc(edge['src_anchor'])}"
            items.append(f"{esc(edge['dst_anchor'])}: {ref}" if sides else ref)
        return ", ".join(items)

    def _outbound_text(self, tool_id: str, node: dict, prefix: str) -> str:
        anchors = list(node.get("out_anchors") or [])
        if len(anchors) < 2:
            return ""
        readers: dict[str, list[str]] = {}
        for edge in self.outbound[tool_id]:
            readers.setdefault(edge["src_anchor"], []).append(f"T{esc(prefix)}{esc(edge['dst'])}")
        keep_unread = node.get("type") in _SPLITTERS
        return ", ".join(f"{esc(anchor)} → {'+'.join(readers.get(anchor, [])) or 'none'}"
                         for anchor in anchors if anchor in readers or keep_unread)
