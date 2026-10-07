"""A workflow's final targets and the write mode each must be written in (live hardening, Task L4;
objective-first design §4.4).

`compile_check.py`'s `write_mode` check (a `build.sql`) and `lib.snowpark_rules`' `rule:write_mode`
(a `build.py`) read a target's write mode here. `objective_targets` reads it from the objective
(`outputs[].write_mode`, `keys`, `pre_sql`, `post_sql`, every one of them written by
`objective_scaffold.py` from intake's mappings and the parsed Output tool, so there is no second place to
look). `lib.vocab.TARGET_WRITE_MODES` names every spelling of a write mode.
"""
from __future__ import annotations

from dataclasses import dataclass

from .vocab import TARGET_WRITE_MODES


def _has_sql(config: dict, key: str) -> bool:
    return bool(str(config.get(key) or "").strip())


class ContractError(ValueError):
    """The objective (or intake's mappings) does not say what a check needs -- a target's write mode,
    or a merge target's keys. Nothing in the build file can fix that, so it is a usage error (exit 2),
    not a compile error: the objective, or intake's mappings, has to say it."""


@dataclass(frozen=True)
class TargetWrite:
    """One final target of a workflow, as `write_mode` (and the Snowpark `rule:write_mode`) judge
    it: its logical name, its write mode (`mode`, the `TARGET_WRITE_MODES` form; `spelling`, as the
    objective wrote it), the MERGE keys, and whether its Output tool has a PreSQL / PostSQL -- the only
    statements besides the write that may touch it."""
    logical: str
    mode: str
    spelling: str
    keys: tuple[str, ...]
    tool_id: str
    pre_sql: bool
    post_sql: bool


def objective_targets(objective: dict) -> list[TargetWrite]:
    """Every output of `objective` with its write mode (design §4.4). The objective is complete when intake
    is READY: an output with no `logical`, with no `write_mode` or one outside `TARGET_WRITE_MODES`, or an
    `update_insert` output with no `keys`, is a `ContractError` -- nothing in a build file fixes it, so
    `compile_check.py` exits 2 rather than reporting a compile error."""
    targets = []
    for position, output in enumerate(objective.get("outputs") or []):
        where = f"objective.json outputs[{position}] (tool {output.get('tool_id')})"
        logical = output.get("logical")
        if not logical:
            raise ContractError(f"{where} has no logical name; expected intake to have named it (run "
                                f"objective_scaffold.py --apply once intake is READY)")
        spelling = output.get("write_mode")
        mode = TARGET_WRITE_MODES.get(str(spelling)) if spelling else None
        if mode is None:
            raise ContractError(f"{where} ({logical}) has write_mode {spelling!r}; expected one of "
                                f"{', '.join(sorted(TARGET_WRITE_MODES))}")
        keys = [str(key) for key in output.get("keys") or []]
        if mode == "update_insert" and not keys:
            raise ContractError(f"{where} ({logical}) is {spelling} and names no keys; expected the keys its "
                                f"MERGE matches on in the objective's keys")
        targets.append(TargetWrite(logical=str(logical), mode=mode, spelling=str(spelling), keys=tuple(keys),
                                   tool_id=str(output.get("tool_id") or ""), pre_sql=_has_sql(output, "pre_sql"),
                                   post_sql=_has_sql(output, "post_sql")))
    return targets
