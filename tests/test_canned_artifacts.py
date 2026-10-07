"""The canned agent artifacts under `samples/<wf>/canned/` are what the orchestrator's mock runner replays instead of
calling an agent, and `samples/<wf>/broken/` holds the whole-file broken builds that prove `compare.py` catches a
bad translation. This module checks the shape of the canned tree, so a hand-written fixture cannot drift away from
what the real pipeline writes and reads.

Every check here is static or structural. Whether a build actually *produces* the golden data is
`tests/test_validate_build.py`'s job, whether it compiles with no unresolved lint flag
`tests/test_canned_builds.py`'s, and what an objective says `tests/test_canned_objectives.py`'s; every number
about data comes from `scripts/` -- nothing in this file computes one. Nothing in the repository has run against a
real Snowflake account or a real Alteryx engine; the DAGs these tests read are built by the local parser into
`tmp_path`.

The tests are parametrized over whichever `samples/wf_*/canned/` directories exist, split by what each workflow's
canned set replays: `TRANSLATED_WORKFLOWS` for the ones that carry a build file, and `RECOVERY_WORKFLOWS` for the
ones whose replay is a parser extension because the workflow never gets far enough to be translated (tier T3).
"""
from __future__ import annotations

import csv
import json
import re
import shutil
from pathlib import Path

import pytest

from dev import build_samples
from lib import io
from lib.paths import Repo
from tests.helpers import copy_pristine_mappings_and_catalog

ROOT = Path(__file__).parents[1]
SAMPLES = ROOT / "samples"
CATALOG = ROOT / "catalog" / "columns.csv"

TIERS = frozenset({"T1", "T2", "T3"})

CANNED_WORKFLOWS = sorted(path.parent.name for path in SAMPLES.glob("wf_*/canned"))

#: The canned workflows that carry a translation: a build file, its notes, a review verdict and the documenter's
#: record. Everything a *migrated* workflow's artifacts have to satisfy is parametrized over these, not over
#: `CANNED_WORKFLOWS`, because a workflow that parks at MANUAL never gets a build file at all
#: (`orchestrator/stages.ts` stops a T3 workflow after the analyzer) and would otherwise fail every one of them
#: for the right reason.
TRANSLATED_WORKFLOWS = sorted({path.parents[1].name for pattern in ("wf_*/canned/build.sql", "wf_*/canned/build.py")
                               for path in SAMPLES.glob(pattern)})

#: The canned workflows whose replay is a parser extension rather than a build file -- the files a
#: `parser-recovery` agent writes when `scripts/parse.py` cannot get a workflow past its invariants.
RECOVERY_WORKFLOWS = sorted(path.parents[1].name for path in SAMPLES.glob("wf_*/canned/parser-recovery"))

#: Where `orchestrator/runner.ts`'s `replayRecovery` puts each canned recovery file: a path under `parsed/` goes
#: into the workflow's own directory, and everything else into the repository root. Only these roots are
#: legitimate, and an extension may write nowhere else (program spec §6.4, `orchestrator/policy.ts`).
_RECOVERY_ROOTS = ("parsed/", "scripts/parsers/ext/", "tests/parser_corpus/")

#: What a translated workflow's canned tree holds, and nothing else besides `docs/` and `intake/`.
_TRANSLATED_FILES = ("objective.json", "objective.md", "translation_notes.md", "review.json",
                     "docs/migration.md", "intake/plan.md")

#: The sections `.github/agents/documenter.agent.md` requires of `docs/migration.md`, in its order.
_MIGRATION_SECTIONS = ("overview", "purpose", "source mappings", "steps", "assumptions", "accepted differences",
                       "unsupported / manual items", "validation summary", "deployment", "runbook", "open items")

#: The names of the retired segment pipeline: none may survive in a canned tree or a sample README.
_RETIRED = re.compile(r"seg_0\d|\bproc\.(?:sql|py)\b|contract\.json|broken_sql|unsupported\.json|analysis\.md|"
                      r"segments/|validate_segment|validate_snowpark|validate_workflow|target_check|render_snowpark|"
                      r"translation_scaffold|IDENTIFIER\(:")


def test_at_least_one_workflow_has_canned_artifacts():
    """Guards the parametrizations below: an empty glob would turn every test in this module into an empty
    parameter set that quietly passes."""
    assert CANNED_WORKFLOWS, f"no samples/wf_*/canned/ directory exists under {SAMPLES}"
    assert TRANSLATED_WORKFLOWS, f"no samples/wf_*/canned/build.sql or build.py exists under {SAMPLES}"
    assert RECOVERY_WORKFLOWS, f"no samples/wf_*/canned/parser-recovery/ directory exists under {SAMPLES}"
    assert set(TRANSLATED_WORKFLOWS) | set(RECOVERY_WORKFLOWS) == set(CANNED_WORKFLOWS), (
        f"these canned workflows replay neither a translation nor a parser recovery: "
        f"{sorted(set(CANNED_WORKFLOWS) - set(TRANSLATED_WORKFLOWS) - set(RECOVERY_WORKFLOWS))}")
    assert not set(TRANSLATED_WORKFLOWS) & set(RECOVERY_WORKFLOWS)


@pytest.fixture(scope="module")
def build_workflow(tmp_path_factory):
    """`build_workflow(wf_id)` -> a `Repo` holding that sample parsed and simulated.

    Built once per workflow per module run: every test below that needs a parsed DAG shares it.
    `build_samples.build` chains the parser and the Alteryx simulator, both local doubles.
    """
    built: dict[str, Repo] = {}

    def build(wf_id: str) -> Repo:
        if wf_id not in built:
            root = tmp_path_factory.mktemp(wf_id)
            copy_pristine_mappings_and_catalog(root)
            repo = Repo(root)
            build_samples.build(repo, SAMPLES, wf_id)
            built[wf_id] = repo
        return built[wf_id]

    return build


# --- helpers -----------------------------------------------------------------------------------


def _canned(wf_id: str) -> Path:
    return SAMPLES / wf_id / "canned"


def _objective(wf_id: str) -> dict:
    return json.loads((_canned(wf_id) / "objective.json").read_text(encoding="utf-8"))


def _build_file(wf_id: str) -> Path:
    found = [path for path in (_canned(wf_id) / "build.sql", _canned(wf_id) / "build.py") if path.is_file()]
    assert len(found) == 1, f"{wf_id}: expected exactly one of canned/build.sql and canned/build.py, found {found}"
    return found[0]


def _catalog_names() -> set[str]:
    """Every real table in `catalog/columns.csv`, as the qualified names a build must never write: `DB.SCHEMA.TABLE`
    and `SCHEMA.TABLE`.

    The bare table name is deliberately not included: a logical name may legitimately equal it (`ORDERS` is both
    `SALES.RAW.ORDERS`'s table name and tool 1's logical name), and the logical name is exactly what the build
    file is told to use.
    """
    names: set[str] = set()
    with CATALOG.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            database, schema, table = row["database"], row["schema"], row["table"]
            names.add(f"{database}.{schema}.{table}".upper())
            names.add(f"{schema}.{table}".upper())
    return names


# --- the translated workflows ----------------------------------------------------------------------


@pytest.mark.parametrize("wf_id", TRANSLATED_WORKFLOWS)
def test_the_canned_artifact_set_is_complete(wf_id):
    """What the mock runner replays for a T1/T2 workflow, one file per agent stage -- and nothing from the retired
    segment pipeline: no `segments/`, no `analysis.md`, no `unsupported.json`, no `dbt/` project."""
    canned = _canned(wf_id)
    for relative in _TRANSLATED_FILES:
        assert (canned / relative).is_file(), f"{wf_id}: canned/{relative} is missing"
    build = _build_file(wf_id)
    objective = _objective(wf_id)
    assert build.name == ("build.py" if objective["language"] == "python" else "build.sql"), (
        f"{wf_id}: the objective's language is {objective['language']!r}, so the canned build is not {build.name}")
    on_disk = sorted(path.relative_to(canned).as_posix() for path in canned.rglob("*") if path.is_file())
    assert on_disk == sorted([*_TRANSLATED_FILES, build.name]), (
        f"{wf_id}: canned/ holds {on_disk}; expected exactly {sorted([*_TRANSLATED_FILES, build.name])}")
    review = json.loads((canned / "review.json").read_text(encoding="utf-8"))
    assert review == {"verdict": "PASS", "findings": []}, f"{wf_id}: review.json is {review}, expected a clean PASS"


@pytest.mark.parametrize("wf_id", CANNED_WORKFLOWS)
def test_the_canned_objective_declares_a_valid_tier_and_the_plan_says_the_same(wf_id):
    objective = _objective(wf_id)
    assert objective["tier"] in TIERS, f"{wf_id}: objective.json tier is {objective['tier']!r}, expected one of {sorted(TIERS)}"
    plan = (_canned(wf_id) / "intake" / "plan.md").read_text(encoding="utf-8")
    stated = re.search(r"\*\*(T[123])\b", plan)
    assert stated and stated.group(1) == objective["tier"], (
        f"{wf_id}: intake/plan.md says {stated and stated.group(1)}, objective.json says {objective['tier']}")


@pytest.mark.parametrize("wf_id", CANNED_WORKFLOWS)
def test_no_build_names_a_real_catalog_table(wf_id):
    """A mapped source or target is reached only as `{{src}}.<LOGICAL>` / `{{tgt}}.<LOGICAL>` (`f"{src}.<LOGICAL>"`
    in a module), so the same file runs against the golden view schema and against production. The broken builds
    are checked too -- a fixture is allowed one wrong translation, not a hard-coded table name."""
    forbidden = _catalog_names()
    files = sorted(_canned(wf_id).glob("build.*"))
    files += sorted(path for path in (SAMPLES / wf_id / "broken").glob("*") if path.suffix in (".sql", ".py"))
    for path in files:
        text = path.read_text(encoding="utf-8").upper()
        named = sorted(name for name in forbidden if name in text)
        assert not named, (
            f"{path.relative_to(SAMPLES)} names the mapped table(s) {named}; a build file reads "
            f"{{{{src}}}}.<LOGICAL> and writes {{{{tgt}}}}.<LOGICAL> instead")


@pytest.mark.parametrize("wf_id", TRANSLATED_WORKFLOWS)
def test_the_translation_notes_are_one_assumption_per_line(wf_id):
    text = (_canned(wf_id) / "translation_notes.md").read_text(encoding="utf-8")
    assert text.startswith(f"# {wf_id}"), f"{wf_id}: translation_notes.md does not open with its workflow's heading"
    bullets = [line for line in text.splitlines() if line.startswith("- ")]
    assert bullets, f"{wf_id}: translation_notes.md lists no assumption"
    for line in bullets:
        if line.startswith("- lint "):
            assert re.match(r"- lint \w+ \S+: kept because \S", line), (
                f"{wf_id}: a kept lint flag is `lint <kind> <name>: kept because <reason>`, not {line!r}")


@pytest.mark.parametrize("wf_id", TRANSLATED_WORKFLOWS)
def test_the_documenter_sections_are_all_present(wf_id):
    """The sections `.github/agents/documenter.agent.md` requires of `docs/migration.md` -- `## Deployment` and the
    link to the generated mapping included: what gets deployed, by whom, and that nothing here has run on
    Snowflake."""
    text = (_canned(wf_id) / "docs" / "migration.md").read_text(encoding="utf-8")
    headings = {line.lstrip("# ").strip().lower() for line in text.splitlines() if line.startswith("## ")}
    missing = [heading for heading in _MIGRATION_SECTIONS if heading not in headings]
    assert not missing, f"{wf_id}: docs/migration.md has no section for {missing}"
    assert "](mapping.md)" in text, f"{wf_id}: docs/migration.md does not link the generated docs/mapping.md"
    assert "Nothing in this migration has been run" in text


@pytest.mark.parametrize("wf_id", TRANSLATED_WORKFLOWS)
def test_the_migration_record_names_every_step_the_objective_has(wf_id):
    text = (_canned(wf_id) / "docs" / "migration.md").read_text(encoding="utf-8")
    for step in _objective(wf_id)["steps"]:
        assert f"**{step['id']} · {step['name']}**" in text, f"{wf_id}: docs/migration.md does not list step {step['id']}"


@pytest.mark.parametrize("wf_id", CANNED_WORKFLOWS)
def test_no_canned_file_or_sample_readme_names_the_retired_pipeline(wf_id):
    """The segment procedures, their contracts, the per-tool analysis and the dbt project agents wrote are gone; a
    canned tree or a sample README that still names one has drifted from what the pipeline writes."""
    files = [path for path in _canned(wf_id).rglob("*") if path.is_file() and path.suffix in (".md", ".json", ".sql", ".py")
             and "parser-recovery" not in path.parts]
    files.append(SAMPLES / wf_id / "README.md")
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not _RETIRED.search(line), f"{path.relative_to(SAMPLES)}:{number} names the retired pipeline: {line.strip()!r}"


# --- the parser-recovery artifacts ----------------------------------------------------------------
#
# A workflow the parser cannot get past its invariants replays a parser EXTENSION instead of a build file
# (`orchestrator/runner.ts`'s `replayRecovery`). These files are code, not prose, so the checks below actually run
# them: the extension has to register through `scripts/parsers/registry.py`'s real mechanism, and the corpus test
# that ships with it has to pass from a tree that holds nothing but the replayed files.


def _recovery_files(wf_id: str) -> dict[str, Path]:
    """Every canned recovery file, keyed by the forward-slash path `replayRecovery` derives."""
    base = _canned(wf_id) / "parser-recovery"
    return {path.relative_to(base).as_posix(): path for path in sorted(base.rglob("*")) if path.is_file()}


def _stage_recovery(wf_id: str, root: Path) -> Path:
    """Copy the canned recovery files into `root` exactly as the mock runner would, minus the `parsed/` ones,
    which go to the workflow directory and are prose. Returns `root`."""
    for relative, path in _recovery_files(wf_id).items():
        if relative.startswith("parsed/"):
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, destination)
    return root


@pytest.mark.parametrize("wf_id", RECOVERY_WORKFLOWS)
def test_the_recovery_artifact_set_is_complete(wf_id):
    """What the mock runner replays for a workflow that parks at MANUAL: the intake plan and the objective (tier
    T3), plus the three things `.github/agents/parser-recovery.agent.md` requires -- a diagnosis, an extension, and
    a corpus fixture with a test.

    There is deliberately no build file, no `translation_notes.md`, no `review.json` and no `docs/migration.md`:
    `orchestrator/stages.ts` stops a T3 workflow after the analyzer, so neither the translator nor the documenter
    ever runs.
    """
    canned = _canned(wf_id)
    for relative in ("intake/plan.md", "objective.json", "objective.md"):
        assert (canned / relative).is_file(), f"{wf_id}: canned/{relative} is missing"
    for relative in ("build.sql", "build.py", "translation_notes.md", "review.json", "docs/migration.md"):
        assert not (canned / relative).exists(), (
            f"{wf_id}: canned/{relative} exists, but this workflow replays a parser recovery and parks at MANUAL: "
            f"there is nothing to translate, review or document")

    files = _recovery_files(wf_id)
    assert "parsed/parse_diagnosis.md" in files, f"{wf_id}: no parser-recovery/parsed/parse_diagnosis.md"
    assert files["parsed/parse_diagnosis.md"].read_text(encoding="utf-8").strip(), f"{wf_id}: parse_diagnosis.md is empty"

    extensions = [name for name in files if name.startswith("scripts/parsers/ext/") and name.endswith(".py")]
    assert extensions, f"{wf_id}: no parser-recovery/scripts/parsers/ext/*.py"
    fixtures = {name.split("/")[2] for name in files if name.startswith("tests/parser_corpus/")}
    assert fixtures, f"{wf_id}: no parser-recovery/tests/parser_corpus/<name>/ fixture directory"
    for fixture in sorted(fixtures):
        present = {name.split("/", 3)[3] for name in files if name.startswith(f"tests/parser_corpus/{fixture}/")}
        assert any(name.startswith("test_") and name.endswith(".py") for name in present), (
            f"{wf_id}: corpus fixture {fixture} has no test_*.py; an extension without a test is not finished "
            f"(scripts/parsers/ext/README.md)")
        assert any(name.endswith((".yxmd", ".yxwz", ".yxmc", ".xml")) for name in present), (
            f"{wf_id}: corpus fixture {fixture} has no workflow fragment")
        assert "README.md" in present, f"{wf_id}: corpus fixture {fixture} has no README.md saying what it guards"

    outside = [name for name in files if not name.startswith(_RECOVERY_ROOTS)]
    assert not outside, (
        f"{wf_id}: these canned recovery files are outside the roots `replayRecovery` and `orchestrator/policy.ts` "
        f"allow ({list(_RECOVERY_ROOTS)}): {outside}")


@pytest.mark.parametrize("wf_id", RECOVERY_WORKFLOWS)
def test_a_recovered_workflow_declares_tier_t3_and_names_what_blocks_it(wf_id, build_workflow):
    """A workflow that needs parser recovery is not automatically T3 -- the recovery is what lets the pipeline
    *reach* a verdict. This one is T3 for its own reasons, and the objective has to say which tools they are, by an
    id the parsed DAG really has."""
    objective = _objective(wf_id)
    assert objective["tier"] == "T3", (
        f"{wf_id}: replays a parser recovery and never produces a build file, so its tier must be T3, not "
        f"{objective['tier']!r}")
    blocking = [risk for risk in objective["parity_risks"] if risk["class"] == "UNKNOWN"]
    assert blocking, f"{wf_id}: tier T3 with no UNKNOWN parity risk says nothing about what blocks it"

    repo = build_workflow(wf_id)
    dag = io.read_json(repo.wf(wf_id, "parsed", "dag.json"))
    tool_ids = {str(node["tool_id"]) for node in dag["nodes"]}
    for risk in blocking:
        assert str(risk["tool_id"]) in tool_ids, (
            f"{wf_id}: objective.json names tool {risk['tool_id']!r}, which is not in the parsed DAG")
        assert str(risk["note"]).strip(), f"{wf_id}: the parity risk for tool {risk['tool_id']} gives no reason"


@pytest.mark.parametrize("wf_id", RECOVERY_WORKFLOWS)
def test_the_recovery_extension_registers_and_explains_the_unknown_tool(wf_id, tmp_path):
    """The extension really does register through `parsers.registry` and really does turn the workflow's own
    `INVARIANT_VIOLATION` into a clean parse -- run against the sample's own source, from a tree holding nothing but
    the replayed files.

    `registry` state is process-global, so it is reset in a `finally`, as every other test that loads an extension
    does.
    """
    import invariants
    import parse
    from parsers import registry

    source = parse.workflow_file(SAMPLES / wf_id / "source")
    xml_text = parse.decode_xml(source.read_bytes())
    try:
        before, _ = parse.parse_file(source)
        assert any("unknown tools without a behavior" in error for error in invariants.check(xml_text, before)), (
            f"{wf_id} parses cleanly without the extension, so the recovery artifacts guard nothing")

        ext_dir = _stage_recovery(wf_id, tmp_path) / "scripts" / "parsers" / "ext"
        after, _ = parse.parse_file(source, ext_dirs=[ext_dir])
        assert invariants.check(xml_text, after) == [], (
            f"{wf_id}: the canned extension does not get the workflow past its invariants")
    finally:
        registry.reset()

    explained = [node for node in after["nodes"] if node.get("type") == "unknown"]
    assert explained, f"{wf_id}: no unknown tool left for the extension to explain"
    for node in explained:
        assert str(node.get("behavior") or "").strip(), f"{wf_id}: tool {node['tool_id']} is still unknown with no behavior"
        assert 0.0 <= float(node["confidence"]) < 1.0, (
            f"{wf_id}: tool {node['tool_id']} claims confidence {node.get('confidence')!r}; an extension that has not "
            f"settled the tool's semantics may not claim certainty")
        assert node["raw_config"], (
            f"{wf_id}: tool {node['tool_id']} lost its raw_config, which is the only record of what the vendor tool "
            f"was configured to do")


@pytest.mark.parametrize("wf_id", RECOVERY_WORKFLOWS)
def test_the_recovery_corpus_test_passes_from_a_replayed_tree(wf_id, tmp_path):
    """The test that ships with the extension is the permanent regression gate, so it has to pass where the mock
    runner puts it -- `<root>/tests/parser_corpus/<name>/` beside `<root>/scripts/parsers/ext/`, with no conftest and
    no `pythonpath` to help it.

    Run in this process rather than as a subprocess: a replayed tree holds the extension and the fixture but not the
    parser, so the module under test has to find `parse` on the session's own path, which is exactly the fallback it
    is written for.
    """
    import importlib.util

    from parsers import registry

    root = _stage_recovery(wf_id, tmp_path)
    modules = sorted((root / "tests" / "parser_corpus").glob("*/test_*.py"))
    assert modules, f"{wf_id}: the replayed tree holds no corpus test to run"
    try:
        for index, path in enumerate(modules):
            spec = importlib.util.spec_from_file_location(f"replayed_corpus_{wf_id}_{index}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cases = sorted(name for name in dir(module) if name.startswith("test_"))
            assert cases, f"{wf_id}: {path.name} defines no test"
            for case in cases:
                getattr(module, case)()
    finally:
        registry.reset()
