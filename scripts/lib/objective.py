"""A workflow's objective (`workflows/<id>/objective.json`) and where its build file is (objective-first
design §4.2 and §4.4).

The analyzer writes the objective -- what the workflow is for and what each output must contain -- and
the translator writes one `build.sql` (or `build.py`) from it. Every script that reads either goes
through this module so they agree on the vocabularies and the paths:

* `contract_view` gives `compare.py` the contract shape it has always read, for ONE objective output.
  `compare.py` was written for per-segment contracts; the objective keeps the same meanings for `row_relation`, `ordering`, `tolerances`, `normalizations` and
  `parity_risks`, now per output, so the comparison engine is unchanged and only its input is converted.
* `dag_tool_ids` is the list of tool ids the objective has to account for: each is in exactly one step's
  `tools` or in `no_effect`. Tools inside a macro belong to the macro tool, which is itself a node.

Nothing here has run against a real Snowflake account or Alteryx.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .io import read_json
from .paths import Repo

#: The canonical write modes (`lib.vocab.TARGET_WRITE_MODES` maps every spelling to one of these).
WRITE_MODES = ("overwrite", "append", "truncate_append", "update_insert")
LANGUAGES = ("sql", "python")
TIERS = ("T1", "T2", "T3")
STEP_ID = re.compile(r"^s[1-9][0-9]*$")
QUESTION_ID = re.compile(r"^O[1-9][0-9]*$")


def objective_path(repo: Repo, wf_id: str) -> Path:
    """`workflows/<id>/objective.json` (whether or not it exists yet)."""
    return repo.wf(wf_id, "objective.json")


def load(repo: Repo, wf_id: str) -> dict:
    """The objective as written. `FileNotFoundError` when there is none (the analyze stage writes it),
    `ValueError` naming the file when it is not JSON or not a JSON object."""
    path = objective_path(repo, wf_id)
    if not path.is_file():
        raise FileNotFoundError(f"{path}: {wf_id} has no objective.json; expected the analyze stage to write it")
    try:
        data = read_json(path)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: not valid JSON ({error}); expected a JSON object") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path}: found a JSON {type(data).__name__}; expected a JSON object")
    return data


def build_path(repo: Repo, wf_id: str) -> Path | None:
    """The workflow's `build.sql` or `build.py`, whichever exists; None when neither does. Both existing
    is a `ValueError`: the deliverable is one file, and the objective's `language` says which."""
    found = [path for path in (repo.wf(wf_id, "build.sql"), repo.wf(wf_id, "build.py")) if path.is_file()]
    if len(found) > 1:
        raise ValueError(f"{repo.wf(wf_id)}: both build.sql and build.py exist; expected exactly one, the one "
                         f"the objective's language names")
    return found[0] if found else None


def _required(objective: dict, key: str):
    if key not in objective:
        raise ValueError(f"objective.json: field '{key}' is missing; expected it (design §4.2) before the "
                         f"objective can be viewed as a comparison contract")
    return objective[key]


def contract_view(objective: dict, output: dict) -> dict:
    """The segment-contract shape `compare.py` reads, for the one `output` of `objective`.

    `compare.compare` reads `output` / `outputs[].stream`, `segment`, `tolerances`, `ordering` and
    `normalizations` from the contract and `stream`, `write_mode`, `columns` and `keys` from the output
    entry; each is present here (`segment` is None: there are no segments any more, and `compare.py`
    carries it into its report and into the approvals match as it always has). The entry is the objective
    output plus `kind: "target"` and, when it has no `stream`, its `logical` name as the stream: `compare.py`
    names the output it is judging by `stream` in its advisories, and `validation.py` tags every diff cluster
    with it, so a stream of None would call every output of a workflow "the output". A copy: changing the
    view never changes the objective.
    """
    entry = dict(output)
    entry.setdefault("kind", "target")
    if not entry.get("stream"):
        entry["stream"] = output.get("logical")
    return copy.deepcopy({
        "workflow": _required(objective, "workflow"),
        "segment": None,
        "target": _required(objective, "language"),
        "inputs": _required(objective, "inputs"),
        "outputs": [entry],
        "output": entry,
        "row_relation": output.get("row_relation"),
        "ordering": output.get("ordering") or {},
        "tolerances": output.get("tolerances") or {},
        "normalizations": output.get("normalizations") or [],
        "parity_risks": objective.get("parity_risks") or [],
    })


def dag_tool_ids(dag: dict) -> list[str]:
    """Every top-level tool id of a parsed `dag.json`, in its order, as strings. Containers and comment
    tools are nodes and so are listed; the tools inside a macro (`sub_dag`) are not, the macro tool is."""
    ids: list[str] = []
    for node in dag.get("nodes") or []:
        if not isinstance(node, dict) or node.get("tool_id") in (None, ""):
            raise ValueError(f"dag.json: a node has no tool_id ({node!r}); expected every node to carry one")
        tool_id = str(node["tool_id"])
        if tool_id not in ids:
            ids.append(tool_id)
    return ids
