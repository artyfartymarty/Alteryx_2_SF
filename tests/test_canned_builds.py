"""The samples' canned builds (`samples/<wf>/canned/build.sql`, or `build.py` for wf_0006): what a correct
translator writes for each canned objective, condensed by hand from the per-segment procedures the samples
used to carry (which pass the golden data) into the one minimal file of design §4.4 / §4.5.

Each must pass `compile_check.py` with `ok: true` and no lint flag (a flag is allowed only with a
`lint <kind> <name>: kept because ...` line in the sample's canned `translation_notes.md`), mark every step
of its canned objective with that objective's own step ids, and be shorter than the procedures it replaces.
wf_0005 is tier T3 (its workflow has a tool no translation can express): it has an objective and no build.

Golden validation of these builds is `validate_build.py`'s, in `test_validate_build.py`. Nothing here has run
on Snowflake: passing means sqlglot, DuckDB (or the Snowpark Local Testing Framework) accepted the file.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

import compile_check
from lib import build_sql
from lib.io import read_json
from lib.objective import build_path, load
from lib.paths import Repo

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

#: wf_id -> the build file's name.
BUILDS = {"wf_0001": "build.sql", "wf_0002": "build.sql", "wf_0003": "build.sql", "wf_0004": "build.sql",
          "wf_0006": "build.py", "wf_0007": "build.sql"}

#: The lines of the per-segment procedures each canned build replaces, counted from the files the samples
#: carried before the objective-first rewrite (`canned/segments/*/proc.sql` plus a Snowpark segment's
#: `proc.py`; for wf_0007, whose output was a dbt project, the three `canned/dbt/models/*.sql`). Recorded here
#: because those files are deleted with the rest of the segment machinery.
OLD_LINES = {"wf_0001": 194, "wf_0002": 259, "wf_0003": 251, "wf_0004": 251, "wf_0006": 178, "wf_0007": 123}


def stage(tmp_path: Path, wf_id: str) -> Repo:
    """A workflow directory holding the sample's canned objective and canned build, nothing else."""
    repo = Repo(tmp_path)
    directory = repo.wf(wf_id)
    directory.mkdir(parents=True)
    canned = SAMPLES / wf_id / "canned"
    shutil.copy(canned / "objective.json", directory / "objective.json")
    shutil.copy(canned / BUILDS[wf_id], directory / BUILDS[wf_id])
    return repo


def kept_lines(wf_id: str) -> set[str]:
    """`lint <kind> <name>` of every `lint <kind> <name>: kept because ...` line of the canned notes."""
    notes = SAMPLES / wf_id / "canned" / "translation_notes.md"
    if not notes.is_file():
        return set()
    found = re.findall(r"^\s*(?:[-*]\s*)?lint (\w+) (\S+): kept because \S", notes.read_text(encoding="utf-8"), re.MULTILINE)
    return {f"lint {kind} {name}" for kind, name in found}


@pytest.mark.parametrize("wf_id", sorted(BUILDS))
def test_the_canned_build_compiles_with_no_unresolved_lint_flag(tmp_path, wf_id):
    report = compile_check.compile_check(stage(tmp_path, wf_id), wf_id)
    assert report["errors"] == [] and report["ok"] is True
    flags = (report.get("lint") or {}).get("flags") or []
    unresolved = [flag for flag in flags if f"lint {flag['kind']} {flag['name']}" not in kept_lines(wf_id)]
    assert unresolved == [], f"{wf_id}: fix these or justify them in canned/translation_notes.md: {unresolved}"


@pytest.mark.parametrize("wf_id", sorted(BUILDS))
def test_the_canned_build_is_shorter_than_the_procedures_it_replaces(wf_id):
    path = SAMPLES / wf_id / "canned" / BUILDS[wf_id]
    new = len(path.read_text(encoding="utf-8").splitlines())
    assert new < OLD_LINES[wf_id], (f"{wf_id}: the canned {path.name} has {new} lines; the per-segment procedures it "
                                    f"replaces had {OLD_LINES[wf_id]}")


@pytest.mark.parametrize("wf_id", sorted(BUILDS))
def test_the_canned_build_marks_every_step_of_its_objective_and_only_those(wf_id):
    objective = read_json(SAMPLES / wf_id / "canned" / "objective.json")
    path = SAMPLES / wf_id / "canned" / BUILDS[wf_id]
    comment = "#" if path.suffix == ".py" else "--"
    marked = {step for marker in build_sql.step_markers(path.read_text(encoding="utf-8"), comment) for step in marker.steps}
    assert marked == {step["id"] for step in objective["steps"]}


@pytest.mark.parametrize("wf_id", sorted(BUILDS))
def test_the_canned_build_is_the_language_the_objective_names(wf_id):
    objective = read_json(SAMPLES / wf_id / "canned" / "objective.json")
    assert objective["language"] == ("python" if BUILDS[wf_id] == "build.py" else "sql")
    assert [p.name for p in (SAMPLES / wf_id / "canned").glob("build.*")] == [BUILDS[wf_id]]


@pytest.mark.parametrize("wf_id", sorted(BUILDS))
def test_the_canned_build_carries_no_machine_path_and_has_lf_endings(wf_id):
    raw = (SAMPLES / wf_id / "canned" / BUILDS[wf_id]).read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    text = raw.decode("utf-8")
    assert not re.search(r"[A-Za-z]:[\\/]", text) and "/home/" not in text


def test_wf_0005_is_tier_t3_and_has_no_build(tmp_path):
    canned = SAMPLES / "wf_0005" / "canned"
    assert read_json(canned / "objective.json")["tier"] == "T3"
    assert list(canned.glob("build.*")) == []
    repo = Repo(tmp_path)
    repo.wf("wf_0005").mkdir(parents=True)
    shutil.copy(canned / "objective.json", repo.wf("wf_0005", "objective.json"))
    assert build_path(repo, "wf_0005") is None and load(repo, "wf_0005")["tier"] == "T3"
    assert compile_check.main(["wf_0005", "--root", str(tmp_path)]) == 2
