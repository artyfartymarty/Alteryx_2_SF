"""What the pipeline can derive about a workflow's data from files it already wrote, shared by the
objective scaffold (one objective per workflow).

Column names and types come from the parsed DAG's RecordInfo; a source's or target's identity and its
write mode come from `intake/mappings.yaml` and the Output tool. Both scaffolds derive them the same
way, so the derivation is here once: `objective_scaffold.py` imports it. It was written for the per-segment
contract scaffold the objective replaced.

Nothing here has run on Snowflake or Alteryx: these are pure functions over parsed JSON.
"""
from __future__ import annotations

from .tool_lines import tool_key        # the one tool-id ordering (compare.py's `_tool_sort_key`), also used here
from .types_map import alteryx_to_snowflake

#: mappings.yaml `mode` -> the Output tool's own write-mode vocabulary
#: (`parsers/tool_config.WRITE_MODES`; intake_prompt maps `update_insert` to `merge` the other way).
WRITE_MODES = {"overwrite": "overwrite", "append": "append", "merge": "update_insert"}

#: Write modes that keep the target's existing rows, so its table can carry columns the stream lacks.
KEEPS_ROWS = frozenset({"append", "update_insert"})

VARIABLE_STRINGS = frozenset({"V_String", "V_WString"})


def nested_tool_key(tool_id: str) -> tuple:
    """`tool_key` per `/`-separated part, so `2` < `2/1` < `2/2` < `2/10` < `3`."""
    return tuple(tool_key(part) for part in str(tool_id).split("/"))


def nested_ids(node: dict, prefix: str) -> list[str]:
    """`<prefix><id>` for every node of a resolved macro's `sub_dag`, recursively (the
    id a tool inside a macro is known by)."""
    ids: list[str] = []
    for inner in ((node.get("sub_dag") or {}).get("nodes") or []):
        inner_id = f"{prefix}{inner.get('tool_id')}"
        ids.append(inner_id)
        ids.extend(nested_ids(inner, f"{inner_id}/"))
    return ids


def column_type(field: dict) -> str:
    """A DAG meta field -> its column type. Raises ValueError/KeyError/TypeError when the type map
    cannot say (the caller leaves the type to the analyzer). A variable-length string keeps its DAG
    size, `VARCHAR(<size>)`, as six of the seven committed samples declare it."""
    if field.get("type") in VARIABLE_STRINGS:
        size = field.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
            return f"VARCHAR({size})"
        return "VARCHAR"
    return alteryx_to_snowflake(field)


def columns_from_meta(fields: list | None, where: str, notes: list[str]) -> tuple[list[dict], dict[int, frozenset[str]]]:
    """DAG meta fields -> `[{name, type, nullable: true}]` (name upper-cased, every consumer compares
    names upper-cased; a type the type map does not cover is left out and noted) and, per column
    index, the other spellings accepted for its type (a variable-length string may also be the type
    map's unsized `VARCHAR`)."""
    columns: list[dict] = []
    accepted: dict[int, frozenset[str]] = {}
    for index, meta in enumerate(fields or []):
        name = str(meta.get("name")).upper()
        column: dict = {"name": name}
        try:
            column["type"] = column_type(meta)
        except (KeyError, ValueError, TypeError):
            notes.append(f"{where}: column {name} has Alteryx type {meta.get('type')!r} (size "
                         f"{meta.get('size')!r}), which lib/types_map.py does not map; the analyzer declares its type")
        column["nullable"] = True
        if meta.get("type") in VARIABLE_STRINGS and column.get("type", "").startswith("VARCHAR("):
            accepted[index] = frozenset({"VARCHAR"})
        columns.append(column)
    return columns, accepted


def anchor_fields(node: dict | None, anchor: str | None) -> list | None:
    """The meta of one anchor of a node; for `anchor=None`, the node's `Output` anchor or its only one."""
    meta = (node or {}).get("meta") or {}
    if anchor is not None:
        return meta.get(anchor)
    if "Output" in meta:
        return meta["Output"]
    return next(iter(meta.values())) if len(meta) == 1 else None


def by_tool(section: dict | None) -> dict[str, dict]:
    """mappings.yaml `sources`/`outputs` -> {tool id: entry} (the first entry naming a tool wins)."""
    out: dict[str, dict] = {}
    for entry in (section or {}).values():
        if not isinstance(entry, dict):
            continue
        for tool_id in entry.get("tool_ids") or []:
            out.setdefault(str(tool_id), entry)
    return out
