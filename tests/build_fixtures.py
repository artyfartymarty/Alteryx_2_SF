"""Small objective and build-file fixtures shared by the `compile_check.py` tests.

A test writes `objective.json` and a build file into a tmp workflow (`make`), calls
`compile_check.compile_check`, and reads the named checks out of the report. Nothing here is a real
workflow: `wf_0009` has one input (ITEMS) and one output (ITEMS_OUT), two steps, and the smallest build
that satisfies every rule; each test mutates one thing and expects one named check to say so.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import compile_check as cc
from lib.io import write_yaml
from lib.paths import Repo

WF = "wf_0009"

COLUMNS = [{"name": "ID", "type": "NUMBER(38,0)", "nullable": True},
           {"name": "NOTE", "type": "VARCHAR(20)", "nullable": True}]


def objective(*, write_mode: str = "overwrite", keys=("ID",), pre_sql=None, post_sql=None, language: str = "sql",
              extra_steps: bool = False) -> dict:
    """The fixture objective: ITEMS in, ITEMS_OUT out, steps s1 (tools 1, 2) and s2 (tool 7)."""
    steps = [{"id": "s1", "name": "Read the items", "does": "Keeps items with a note.", "tools": ["1", "2"], "feeds": ["s2"]},
             {"id": "s2", "name": "Write the items", "does": "Writes ITEMS_OUT.", "tools": ["7"], "feeds": ["out:7"]}]
    if extra_steps:
        steps.append({"id": "s3", "name": "Spare", "does": "Spare step.", "tools": ["8"], "feeds": []})
    return {
        "workflow": WF, "title": "Items", "purpose": "Keeps the items that have a note.", "tier": "T1",
        "language": language, "language_reason": "row-sequential carry-over" if language == "python" else None,
        "inputs": [{"tool_id": "1", "logical": "ITEMS", "columns": copy.deepcopy(COLUMNS), "keys": ["ID"]}],
        "outputs": [{"tool_id": "7", "logical": "ITEMS_OUT", "write_mode": write_mode, "pre_sql": pre_sql,
                     "post_sql": post_sql, "grain": "one row per item", "columns": copy.deepcopy(COLUMNS),
                     "keys": list(keys), "row_relation": "filter",
                     "ordering": {"keys": ["ID"], "alteryx_deterministic": True, "order_dependent_columns": []},
                     "tolerances": {}, "normalizations": []}],
        "steps": steps, "no_effect": [], "rules": [], "parity_risks": [], "questions": [], "answered": [],
    }


#: The smallest build.sql every rule accepts.
GOOD_SQL = """-- wf_0009 · Items
-- Written from objective.md. {{src}} = source schema, {{tgt}} = target schema.
ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;

-- step s1
CREATE OR REPLACE TEMPORARY TABLE kept AS
SELECT ID, NOTE FROM {{src}}.ITEMS WHERE NOTE IS NOT NULL;

-- step s2
CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS
SELECT ID, NOTE FROM kept;
"""


def make(root: Path, build: str | None = None, obj: dict | None = None, *, name: str = "build.sql") -> Repo:
    """A tmp workflow: `objective.json` (the fixture unless `obj`) and the build file (`GOOD_SQL` unless
    `build`; pass `None` for `build` and `name=None` to write no build at all)."""
    repo = Repo(root)
    directory = repo.wf(WF)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "objective.json").write_text(json.dumps(obj if obj is not None else objective(), indent=2) + "\n",
                                              encoding="utf-8", newline="\n")
    if name is not None:
        (directory / name).write_text(GOOD_SQL if build is None else build, encoding="utf-8", newline="\n")
    return repo


def dbt_off(root: Path) -> None:
    """`program.dbt: false` in the tmp repo's `mappings/global.yaml`: the workflow is not delivered as a dbt project,
    so a build the exporter cannot convert is not an error (`compile_check.py`'s `dbt:form`)."""
    write_yaml(Repo(root).global_mappings, {"program": {"dbt": False}})


def check(root: Path, build: str | None = None, obj: dict | None = None, *, name: str = "build.sql") -> dict:
    return cc.compile_check(make(root, build, obj, name=name), WF)


def named(report: dict, check_name: str) -> list[str]:
    """The report's errors of exactly this check (`write_mode` does not match `write_mode:pre_post`)."""
    return [error for error in report["errors"] if error.split(": ", 1)[0] == check_name]
