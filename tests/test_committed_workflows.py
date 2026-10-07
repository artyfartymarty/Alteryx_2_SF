"""Checks on the committed `workflows/` tree -- the worked examples from the offline end-to-end run (README §6's
command sequence), one directory per sample: the digest and the objective, the one build file with its compile
check, the generated mapping, the validation reports and, for a SQL workflow, the dbt project converted from the build
file and its own validation reports. Unlike every other test file here, this one asserts against the REAL,
git-tracked `workflows/` directory at the repo root, not a `tmp_path` fixture: these are the actual worked examples a
reader of this repo opens, and they must stay correct as later tasks touch the scripts that produced them.

Nothing here has run on Snowflake or Alteryx (repo-wide honesty rule): `workflows/` is the
`--runner mock --no-interactive` offline run's own output, replaying hand-written canned artifacts
(`samples/<wf>/canned/`) and running every deterministic script for real against DuckDB (dbt-duckdb for the dbt
project, the Snowpark Local Testing Framework for the one Python workflow).

The last section is the one exception to "only `workflows/`": the hand-off scan for machine paths, login names and
scratchpad references runs over every shipped tree, because the final whole-branch review (I6) found two such
references in `scripts/` and `tests/`, where no test was looking.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"

EXPECTED_TERMINAL = {
    "wf_0001": "VALIDATED",
    "wf_0002": "VALIDATED",
    "wf_0003": "VALIDATED",
    "wf_0004": "VALIDATED",
    "wf_0005": "MANUAL",
    "wf_0006": "VALIDATED",
    "wf_0007": "VALIDATED",
}
WORKFLOW_IDS = sorted(EXPECTED_TERMINAL)
VALIDATED_WORKFLOW_IDS = [wf for wf, terminal in EXPECTED_TERMINAL.items() if terminal == "VALIDATED"]

#: The tier and the build language README §6's terminal-states table gives each sample. wf_0005 is tier T3 (a Run
#: Command and a vendor plugin): its objective is written in full, but nothing is translated, so it has no build file.
EXPECTED_TIER = {"wf_0001": "T1", "wf_0002": "T1", "wf_0003": "T1", "wf_0004": "T1", "wf_0005": "T3",
                 "wf_0006": "T2", "wf_0007": "T1"}
EXPECTED_LANGUAGE = {"wf_0001": "sql", "wf_0002": "sql", "wf_0003": "sql", "wf_0004": "sql", "wf_0005": "sql",
                     "wf_0006": "python", "wf_0007": "sql"}
#: A SQL workflow is also delivered as a dbt project (design D9); a Python workflow and the T3 one have none.
SQL_WORKFLOW_IDS = [wf for wf in VALIDATED_WORKFLOW_IDS if EXPECTED_LANGUAGE[wf] == "sql"]
PYTHON_WORKFLOW_IDS = [wf for wf in VALIDATED_WORKFLOW_IDS if EXPECTED_LANGUAGE[wf] == "python"]

VALID_VERDICTS = {"PASS", "PASS_WITH_ACCEPTED_DIFF"}

#: What a workflow directory held while the pipeline translated segment by segment, plus the files an agent wrote into
#: a dbt project: none of it is written any more, so a committed tree that still has one was produced by the old
#: pipeline and has to be regenerated.
RETIRED_ARTIFACTS = ("segments", "procs", "analysis.md", "unsupported.json", "validation_workflow.json",
                     "golden/intermediates", "dbt/compile_check.json", "dbt/review.json", "dbt/translation_notes.md")

# An absolute path of *this* PC -- never legitimate in a committed artifact (task-17-addendum.md).
_ABS_PATH_RE = re.compile(r"[a-zA-Z]:[\\/]Users[\\/]", re.IGNORECASE)


def _require_workflows_dir() -> None:
    if not WORKFLOWS.is_dir():
        pytest.fail("workflows/ is missing: run the offline sequence in README §6 before "
                     "running this test (scripts/dev/build_samples.py seed, orchestrate.ts twice "
                     "with scripts/dev/answer_samples.py in between)")


@pytest.fixture(autouse=True, scope="module")
def _workflows_dir_must_exist():
    _require_workflows_dir()


def _manifest(wf: str) -> dict:
    return json.loads((WORKFLOWS / wf / "manifest.json").read_text(encoding="utf-8"))


def _json(wf: str, *parts: str) -> dict:
    return json.loads((WORKFLOWS / wf / Path(*parts)).read_text(encoding="utf-8"))


def _build_files(wf: str) -> list[Path]:
    return [path for path in (WORKFLOWS / wf / "build.sql", WORKFLOWS / wf / "build.py") if path.is_file()]


def _tracked(*trees: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", *trees], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _tracked_workflow_files() -> list[str]:
    return _tracked("workflows")


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None  # a binary artifact (e.g. a generated .yxdb): not this check's concern


# --- terminal states (README §6: wf_0001-4, wf_0006 and wf_0007 VALIDATED, wf_0005 MANUAL) -----

@pytest.mark.parametrize("wf", WORKFLOW_IDS)
def test_manifest_reaches_the_expected_terminal_state(wf):
    manifest = _manifest(wf)
    assert manifest["id"] == wf
    assert manifest["status"]["translate"] == EXPECTED_TERMINAL[wf]
    assert manifest["status"]["parse"] in ("PARSED", "RECOVERED")
    assert manifest["status"]["intake"] == "READY"
    assert manifest["status"]["analyze"] == "DONE"
    assert manifest["tier"] == EXPECTED_TIER[wf]
    assert manifest["language"] == EXPECTED_LANGUAGE[wf]


@pytest.mark.parametrize("wf", VALIDATED_WORKFLOW_IDS)
def test_a_validated_workflow_also_finished_golden_and_document(wf):
    manifest = _manifest(wf)
    assert manifest["status"]["golden"] == "DONE"
    assert manifest["status"]["document"] == "DONE"
    assert manifest["tier"] in ("T1", "T2")
    assert manifest["golden_sets"], wf
    assert manifest["language"] == _json(wf, "objective.json")["language"], (
        f"{wf}: the manifest's language is not the objective's")
    assert not manifest.get("reasons"), f"{wf}: a VALIDATED workflow carries no park reason"


def test_the_manual_t3_workflow_never_reached_golden_or_document():
    """docs/spec/01-copilot-setup.md's state diagram draws `analyze --> MANUAL: tier T3` as a dead
    end: golden/document/pr never apply, and `orchestrator/stages.ts`'s `shouldRun` refuses them
    outright for a T3 workflow (task-15-int-report.md, Defect 4). Its objective IS written, in full: that is
    what a person reads to translate it by hand."""
    manifest = _manifest("wf_0005")
    assert manifest["tier"] == "T3"
    assert "golden" not in manifest["status"]
    assert "document" not in manifest["status"]
    assert manifest["status"].get("translate") == "MANUAL"
    assert manifest["reasons"]["translate"] == "tier-T3"
    assert not _build_files("wf_0005"), "a T3 workflow has nothing to translate, so it has no build file"
    for name in ("objective.json", "objective.md", "digest.md"):
        assert (WORKFLOWS / "wf_0005" / name).is_file(), f"wf_0005 has no {name}"
    assert _json("wf_0005", "objective.json")["tier"] == "T3"
    # `build_samples.py seed` still puts the sample's golden INPUTS in place; the golden stage never ran, so there are
    # no golden sets and no golden outputs.
    assert not _manifest("wf_0005").get("golden_sets")
    for absent in ("golden/outputs", "dbt", "docs", "compile_check.json", "review.json", "validation.json"):
        assert not (WORKFLOWS / "wf_0005" / absent).exists(), f"wf_0005 holds {absent}, which a T3 workflow never writes"


# --- one build file per workflow ----------------------------------------------------------------

@pytest.mark.parametrize("wf", WORKFLOW_IDS)
def test_a_committed_workflow_holds_nothing_of_the_segment_pipeline(wf):
    """A workflow is one build file (objective-first design D1): no segments, no master procedure, no per-tool analysis,
    no work-stream golden data -- neither on disk nor tracked."""
    left = [name for name in RETIRED_ARTIFACTS if (WORKFLOWS / wf / name).exists()]
    assert not left, f"{wf}: {left} were written by the retired segment pipeline; regenerate workflows/"
    assert not list((WORKFLOWS / wf).glob("validation_workflow.*.json"))
    tracked = [path for path in _tracked(f"workflows/{wf}")
               if any(path.startswith(f"workflows/{wf}/{name}") for name in RETIRED_ARTIFACTS)]
    assert not tracked, f"{wf}: tracked files of the retired pipeline: {tracked}"


def _step_markers(build: Path) -> set[str]:
    """The step ids a build file's `-- step s1, s3` (SQL) or `# step s1` (Python) markers cite."""
    ids: set[str] = set()
    for line in build.read_text(encoding="utf-8").splitlines():
        marker = re.match(r"\s*(?:--|#) step ((?:s[1-9][0-9]*)(?:\s*,\s*s[1-9][0-9]*)*)\b", line)
        if marker:
            ids.update(re.findall(r"s[1-9][0-9]*", marker.group(1)))
    return ids


@pytest.mark.parametrize("wf", VALIDATED_WORKFLOW_IDS)
def test_a_validated_workflow_has_one_build_file_in_the_objectives_language(wf):
    objective = _json(wf, "objective.json")
    builds = _build_files(wf)
    assert [path.name for path in builds] == ["build.py" if objective["language"] == "python" else "build.sql"], (
        f"{wf}: expected exactly the one build file the objective's language ({objective['language']}) names, found "
        f"{[path.name for path in builds]}")
    assert (WORKFLOWS / wf / "objective.md").is_file()
    assert (WORKFLOWS / wf / "digest.md").is_file()
    assert (WORKFLOWS / wf / "translation_notes.md").is_file()
    assert _json(wf, "review.json")["verdict"] == "PASS"
    assert not (WORKFLOWS / wf / "fix_log.md").exists(), f"{wf}: the committed run needed no fixer, so it left no fix_log.md"
    assert _step_markers(builds[0]) == {step["id"] for step in objective["steps"]}, (
        f"{wf}: the build file's step markers are not the objective's steps")


@pytest.mark.parametrize("wf", VALIDATED_WORKFLOW_IDS)
def test_a_validated_workflow_compiles_with_no_unresolved_lint(wf):
    """`compile_check.json` is `compile_check.py`'s own report: ok, no error, and (for a SQL build, the only language
    that is linted) no lint flag left -- the minimality lint is what keeps the build from growing back into one CTE per
    tool, so a flag standing in a committed example would be the pipeline's own regression."""
    report = _json(wf, "compile_check.json")
    assert report["ok"] is True
    assert report["errors"] == []
    if EXPECTED_LANGUAGE[wf] == "sql":
        assert report["lint"]["flags"] == [], f"{wf}: unresolved lint flags {report['lint']['flags']}"
    else:
        assert "lint" not in report, f"{wf}: a Python build is not linted"


@pytest.mark.parametrize("wf", VALIDATED_WORKFLOW_IDS)
def test_a_validated_workflow_carries_its_generated_mapping(wf):
    mapping = (WORKFLOWS / wf / "docs" / "mapping.md").read_text(encoding="utf-8")
    assert mapping.startswith(f"# {wf}"), mapping[:80]
    assert "```mermaid" in mapping
    build = _build_files(wf)[0].name
    assert f"how the Alteryx workflow maps to {build}" in mapping.splitlines()[0]
    for step in _json(wf, "objective.json")["steps"]:
        assert f"{step['id']} · {step['name']}" in mapping, f"{wf}: docs/mapping.md does not show step {step['id']}"
    assert (WORKFLOWS / wf / "docs" / "migration.md").is_file()


def test_the_readmes_mapping_diagram_is_the_generated_one_for_wf_0001():
    """README §1 shows the diagram `render_mapping.py` wrote for `wf_0001`, not a hand-drawn one: the block is copied
    from the committed `docs/mapping.md`, and this keeps it from drifting when the sample is regenerated."""
    fence = "`" * 3
    mapping = (WORKFLOWS / "wf_0001" / "docs" / "mapping.md").read_text(encoding="utf-8")
    block = re.search(fence + r"mermaid\n(.*?)\n" + fence, mapping, re.DOTALL)
    assert block, "workflows/wf_0001/docs/mapping.md has no mermaid block"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"{fence}mermaid\n{block.group(1)}\n{fence}" in readme


# --- the dbt project of a SQL workflow (design D9, §4.10) ----------------------------------------

@pytest.mark.parametrize("wf", SQL_WORKFLOW_IDS)
def test_a_sql_workflow_carries_the_dbt_project_converted_from_its_build_file(wf):
    dbt = WORKFLOWS / wf / "dbt"
    for name in ("dbt_project.yml", "profiles.yml", "README.md", "models/sources.yml", "models/schema.yml"):
        assert (dbt / name).is_file(), f"{wf}: dbt/{name} is missing"
    assert re.search(rf"^name: mig_{wf}$", (dbt / "dbt_project.yml").read_text(encoding="utf-8"), re.MULTILINE)
    for output in _json(wf, "objective.json")["outputs"]:
        model = dbt / "models" / f"{output['logical'].lower()}.sql"
        assert model.is_file(), f"{wf}: no dbt model for output {output['logical']}"
        assert "{{ config(" in model.read_text(encoding="utf-8")


@pytest.mark.parametrize("wf", SQL_WORKFLOW_IDS)
def test_a_sql_workflows_dbt_project_passed_on_every_golden_set(wf):
    """`validate_dbt.py` ran the project with dbt-duckdb on the same golden data as the build and compared every
    output model: `dbt/validation.json` is the worst verdict across the sets plus idempotency, one
    `dbt/validation.<set>.json` per golden set."""
    report = _json(wf, "dbt", "validation.json")
    assert report["verdict"] == "PASS"
    assert report["language"] == "sql"
    assert report["idempotent"] is True
    assert report["needs_human"] is False
    sets = _manifest(wf)["golden_sets"]
    assert set(report["sets"]) == set(sets)
    for golden_set in sets:
        per_set = _json(wf, "dbt", f"validation.{golden_set}.json")
        assert per_set["golden_set"] == golden_set
        assert per_set["verdict"] == "PASS", f"{wf} dbt {golden_set}: {per_set['verdict']!r}"
    on_disk = sorted(p.name for p in (WORKFLOWS / wf / "dbt").glob("validation.*.json"))
    assert on_disk == sorted(f"validation.{s}.json" for s in sets), on_disk


@pytest.mark.parametrize("wf", SQL_WORKFLOW_IDS)
def test_the_migration_record_names_the_dbt_command_with_the_arguments_deploy_py_requires(wf):
    """`deploy.py --dbt` needs `--database` and `--schema`; the canned record the mock replays and the committed one
    (what that replay produced) say so, so a reader who copies the command does not get a usage error."""
    command = f"`scripts/deploy.py {wf} --dbt --database <DB> --schema <SCHEMA>`"
    for path in (ROOT / "samples" / wf / "canned" / "docs" / "migration.md", WORKFLOWS / wf / "docs" / "migration.md"):
        text = path.read_text(encoding="utf-8")
        assert command in text, path.relative_to(ROOT).as_posix()
        assert f"`scripts/deploy.py {wf} --dbt`" not in text, path.relative_to(ROOT).as_posix()


@pytest.mark.parametrize("wf", PYTHON_WORKFLOW_IDS + ["wf_0005"])
def test_a_python_or_manual_workflow_has_no_dbt_project(wf):
    assert not (WORKFLOWS / wf / "dbt").exists(), f"{wf}: only a validated SQL workflow is delivered as a dbt project"


# --- every committed validation report is a real pass, not a fabricated one ---------------------

def test_every_committed_validation_json_carries_a_passing_verdict():
    checked = 0
    for path in sorted(WORKFLOWS.glob("*/validation*.json")) + sorted(WORKFLOWS.glob("*/dbt/validation*.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        verdict = report.get("verdict")
        assert verdict in VALID_VERDICTS, f"{path.relative_to(ROOT)}: verdict {verdict!r}"
        checked += 1
    assert checked > 0, "no validation*.json files found under workflows/ -- was Step 1 ever run?"


@pytest.mark.parametrize("wf", VALIDATED_WORKFLOW_IDS)
def test_every_validated_workflow_has_a_passing_validation_report_for_every_golden_set(wf):
    """The VALIDATED gate is `validate_build.py`: `validation.json` (the worst verdict across sets, idempotency, the
    language) plus one `validation.<set>.json` per golden set. `deploy.py` refuses a workflow without it."""
    report = _json(wf, "validation.json")
    assert report["verdict"] in VALID_VERDICTS, report["verdict"]
    assert report["language"] == _manifest(wf)["language"]
    assert report["idempotent"] is True
    sets = _manifest(wf)["golden_sets"]
    assert set(report["sets"]) == set(sets)
    for golden_set in sets:
        per_set = _json(wf, f"validation.{golden_set}.json")
        assert per_set["golden_set"] == golden_set
        assert per_set["verdict"] in VALID_VERDICTS, f"{wf} {golden_set}: {per_set['verdict']!r}"
        assert per_set["diff_clusters"] == [] or per_set["verdict"] == "PASS_WITH_ACCEPTED_DIFF"
    on_disk = sorted(p.name for p in (WORKFLOWS / wf).glob("validation.*.json"))
    assert on_disk == sorted(f"validation.{s}.json" for s in sets), on_disk


# --- nothing machine-local or volatile-by-design is tracked --------------------------------------

def test_no_duckdb_or_audit_log_files_are_tracked():
    """Nor anything a dbt run leaves behind: `validate_dbt.py`'s per-set sandboxes
    (`dbt_sandbox_<set>.duckdb`), dbt's logs (`dbt/logs/`) and its compiled output (`dbt/target/`)
    are machine-local by-products, git-ignored, and never part of the worked example."""
    tracked = _tracked_workflow_files()
    assert tracked, "workflows/ has nothing tracked in git -- was it committed?"
    for path in tracked:
        lower = path.lower()
        assert not lower.endswith(".duckdb"), path
        assert not lower.endswith(".duckdb.wal"), path
        assert not lower.endswith("audit.jsonl"), path
        assert "__pycache__" not in lower, path
        assert "/dbt/logs/" not in lower, path
        assert "/dbt/target/" not in lower, path
        assert "dbt_sandbox_" not in lower, path


# --- no absolute path or OS login name of this PC leaked into a committed artifact ---------------

def test_no_absolute_path_of_this_pc_is_committed_anywhere_under_workflows():
    tracked = _tracked_workflow_files()
    assert tracked
    for rel in tracked:
        text = _read_text_or_none(ROOT / rel)
        if text is None:
            continue
        assert not _ABS_PATH_RE.search(text), f"{rel} contains an absolute path of this PC"


_GENERIC_LOGINS = {"user", "admin", "root", "runner", "build", "test", "guest", "owner"}

#: Login names to ban beyond the machine running the suite: a comma-separated list in this
#: environment variable. The list lives OUTSIDE the repository on purpose -- a committed list of
#: build machines' login names (phase 1's final review M10) would itself publish the very names
#: this check protects, so every machine guards its own login (`_login_names`) and a CI job or a
#: maintainer can add the names of other build machines here.
LOGIN_NAMES_ENV = "MIGRATION_BANNED_LOGIN_NAMES"


def _login_names() -> set[str]:
    """The login names no shipped file may contain: whoever runs the suite (`getpass.getuser()`,
    the home directory's name, `USERNAME`/`USER`) plus `LOGIN_NAMES_ENV`, each kept only when it
    is specific enough to mean anything (a name like `user` or `build` would match half the
    English in the tree)."""
    candidates = {getpass.getuser(), Path.home().name, os.environ.get("USERNAME", ""),
                  os.environ.get("USER", "")}
    candidates |= set(os.environ.get(LOGIN_NAMES_ENV, "").split(","))
    return {name.strip() for name in candidates
            if len(name.strip()) >= 4 and name.strip().lower() not in _GENERIC_LOGINS}


def test_the_login_probe_honours_the_environment_list(monkeypatch):
    monkeypatch.setenv(LOGIN_NAMES_ENV, "someone-else, zz, qwertyuiop ")
    names = {name.lower() for name in _login_names()}
    assert {"someone-else", "qwertyuiop"} <= names
    assert "zz" not in names                       # too short to mean anything


def test_only_the_fixture_owner_name_appears_never_the_os_login_name():
    """`wf_owner` is every `samples/wf_000N/sample.json`'s hand-authored fixture value for the
    workflow owner (asserted on by name in
    tests/test_samples_wellformed.py::test_sample_json_has_the_planned_keys) -- it flows verbatim
    into `manifest.json["source"]["owner"]`, `open_questions.md`'s header and the generated docs
    for every one of these seven workflows, on every machine, and is not the leak this check exists
    to catch.

    A build machine's login name is a different thing entirely: it must never appear, because it
    would mean a non-interactive resume (the orchestrator's own `--no-interactive` calls, and this
    offline sample run) silently baked whoever's account happened to run it into a committed
    artifact -- exactly the `intake/mappings.yaml["confirmed_by"]` defect Task 17 found and fixed
    in `scripts/intake_prompt.py`.

    The names come from `_login_names`: the login of whoever runs the suite, plus any listed in
    `LOGIN_NAMES_ENV` -- never from a committed list, which would publish them."""
    names = _login_names()
    tracked = _tracked_workflow_files()
    assert tracked
    for rel in tracked:
        text = _read_text_or_none(ROOT / rel)
        if text is None:
            continue
        for name in sorted(names):
            assert name.lower() not in text.lower(), f"{rel} leaks the login name {name!r}"


# --- the same hand-off scan, over EVERY tracked file (final review I6; round 2 R3) ---------------

#: Spec §1's hand-off bar is "no machine paths", and it is not a property of `workflows/` alone:
#: the final whole-branch review found two `scratchpad/…` pointers in `scripts/lib/` and `tests/`,
#: which no test was looking at. Round 2 found the replacement scan had the same shape of hole --
#: it named five trees, so root-level files, `catalog/`, `cookbook/`, `mappings/`, `snowflake/`
#: and `.github/` were unscanned. The scans now walk `git ls-files` whole: a tree that does not
#: exist yet cannot be forgotten.

#: The user segment of an absolute path on somebody's PC: `C:\\Users\\<name>`, `C:/Users/<name>`,
#: or Git Bash's `/c/Users/<name>`. Doubled separators are matched too: a path quoted inside JSON
#: in a build report arrives as `C:\\\\Users\\\\<name>`.
_USER_PATH_RE = re.compile(r"(?:[a-zA-Z]:[\\/]+|/c/)Users[\\/]+([^\\/\s\"'`,;)*]*)", re.IGNORECASE)

#: The only user segments a committed file may carry. A redaction (`<user>`, `<you>`), an ellipsis
#: standing in for the rest of the path, the character class of a grep recipe, or the plainly
#: fictional account a unit test invents. Any other segment is somebody's real login name.
_PLACEHOLDER_USER_SEGMENTS = {"<user>", "<you>", "<name>", "\u2026", "...", "[a-za-z]",
                              "nobody", "someone", ""}

#: Sentence punctuation is stripped off the captured segment before it is judged. This is what
#: admits README §6's two documented Git-Bash path explanations -- "`pwd` prints /c/Users/…" and
#: "the drive-letter form C:/Users/…." -- whose second one ends the sentence, so the regex
#: captures `….` rather than `…`. It weakens nothing: a real login name with a full stop after it
#: is still a real login name.
_SEGMENT_PUNCTUATION = ".,;:)"

#: `scratchpad` is a directory on the machine an agent happened to run on; a shipped file that
#: points at one is a dead pointer the moment it is read anywhere else. The build reports are the
#: deliberate exception: their subject IS how and where the work ran, and saying "every run
#: happened in a scratch root outside the repo" is the evidence, not a leak. They carry no real
#: path -- the scan above still holds them to `<user>`.
_SCRATCHPAD_EXEMPT = ("docs/superpowers/build-reports/", "docs/live-smoke-test.md")

#: This file is where the checks themselves live, so it is the one file that must spell out the
#: word `scratchpad` it bans everywhere else. It spells out no login name, so the login scan covers
#: it too.
_CHECKS_OWN_FILE = f"tests/{Path(__file__).name}"


def _hand_off_files() -> list[str]:
    """Every tracked file in the repository. Binary files are skipped where they are read."""
    tracked = _tracked()
    assert len(tracked) > 500, f"git ls-files returned only {len(tracked)} files"
    return tracked


def test_no_shipped_file_carries_a_real_machine_path():
    offenders = []
    for rel in _hand_off_files():
        text = _read_text_or_none(ROOT / rel)
        if text is None:
            continue
        for match in _USER_PATH_RE.finditer(text):
            segment = match.group(1).rstrip(_SEGMENT_PUNCTUATION).lower()
            if segment not in _PLACEHOLDER_USER_SEGMENTS:
                offenders.append(f"{rel}: {match.group(0)!r}")
    assert not offenders, "absolute paths of a real machine are committed:\n" + "\n".join(offenders)


def test_no_shipped_file_carries_a_build_machine_login_name():
    offenders = []
    names = _login_names()
    for rel in _hand_off_files():
        text = _read_text_or_none(ROOT / rel)
        if text is None:
            continue
        for name in sorted(names):
            if name.lower() in text.lower():
                offenders.append(f"{rel}: {name}")
    assert not offenders, "a build machine's login name is committed:\n" + "\n".join(offenders)


def test_no_shipped_file_points_at_a_scratchpad():
    offenders = []
    for rel in _hand_off_files():
        if rel.startswith(_SCRATCHPAD_EXEMPT) or rel == _CHECKS_OWN_FILE:
            continue
        text = _read_text_or_none(ROOT / rel)
        if text is None:
            continue
        if "scratchpad" in text.lower():
            offenders.append(rel)
    assert not offenders, ("these files point at a scratchpad directory that exists on no other "
                           "machine:\n" + "\n".join(offenders))


def test_no_shipped_file_carries_a_bidirectional_control_or_a_line_separator():
    """A literal bidirectional override or isolate (U+202A-U+202E, U+2066-U+2069) or a literal line or
    paragraph separator (U+2028, U+2029) is invisible in review and can make code read as something it
    is not (the Trojan Source class; review of live-hardening L3, M4). Code that needs one writes the
    escape, `\\u202e`."""
    offenders = []
    for rel in _hand_off_files():
        text = _read_text_or_none(ROOT / rel)
        if text is None:
            continue
        found = sorted({f"U+{ord(ch):04X}" for ch in text
                        if 0x202A <= ord(ch) <= 0x202E or 0x2066 <= ord(ch) <= 0x2069 or ord(ch) in (0x2028, 0x2029)})
        if found:
            offenders.append(f"{rel}: {', '.join(found)}")
    assert not offenders, "literal invisible control characters are committed:\n" + "\n".join(offenders)


def test_no_shipped_file_carries_a_literal_format_character():
    """The same, for every Unicode format (Cf) character -- U+180E, U+200B-U+200F, U+2060, U+FEFF and the
    rest: invisible in review, so a regex class or a string holding one reads as something it is not (the
    combined re-review of live hardening found U+180E/U+200B/U+200D/U+2060 inside two regex classes in
    `orchestrator/policy.ts`). Code that needs one writes the escape, `\\u200b`. The one exception is a
    byte-order mark as a file's very first character: an encoding marker, which a parser fixture keeps on
    purpose (`tests/parser_corpus/bom/`)."""
    offenders = []
    for rel in _hand_off_files():
        text = _read_text_or_none(ROOT / rel)
        if text is None:
            continue
        body = text[1:] if text.startswith("\ufeff") else text
        found = sorted({f"U+{ord(ch):04X}" for ch in body if unicodedata.category(ch) == "Cf"})
        if found:
            offenders.append(f"{rel}: {', '.join(found)}")
    assert not offenders, "literal invisible format characters are committed:\n" + "\n".join(offenders)
