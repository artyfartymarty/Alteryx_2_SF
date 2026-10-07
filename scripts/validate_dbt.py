"""Does a workflow's dbt project produce the golden outputs? (objective-first design D9, §4.10)

    python scripts/validate_dbt.py <wf_id> [--set NAME]... [--root .]

`export_dbt.py` converts the validated `build.sql` to the dbt project `workflows/<wf_id>/dbt/`; this runs that
project against the SAME golden data `validate_build.py` used and judges every objective output with the same
`compare.py` (given the objective converted, per output, into the contract shape it reads:
`lib.objective.contract_view`). The report is `validate_build.py`'s, written to `workflows/<wf_id>/dbt/validation.json`
and `validation.<set>.json`: the golden set's verdict is the worst output verdict, `needs_human` if any output says
so, every diff cluster keyed by the output it came from (`lib.validation.combine_outputs`); the top-level file takes
the worst verdict across sets and the body of the first set with it, its `idempotent` the AND over the sets. Nothing here has run on a real Snowflake
account or in a real Alteryx: locally "parity" means dbt-duckdb on DuckDB agrees with the Alteryx simulator.

**One golden set.** A fresh on-disk DuckDB sandbox (`workflows/<wf>/dbt_sandbox_<set>.duckdb`, git-ignored: dbt
needs a file, and dbt-duckdb names the attached catalog after its stem, so the name has no leading dot) is
loaded exactly as `validate_build.py` loads one -- `load_golden.load_set`: each source a view under
`MIGDB.MIG_GOLDEN_<WF>_<SET>` by its logical name, each `targets_before` file a table under `MIGDB.MIG_WORK` --
and `dbt build` runs the project on it through `lib.dbt_project.run_dbt`, the one place this repository invokes
dbt: the models, then the tests of `models/schema.yml` (`not_null`, `unique`, each `severity: warn`). `--vars` are the flattened local
schema names (`dbt_project.local_vars`), so `{{ source() }}` reads the golden view schema and every output
lands in `MIGDB.MIG_WORK`, where it is read back and compared. The dbt log is kept at
`dbt/logs/validate_<set>.log` (git-ignored).

**The files stay Snowflake SQL.** The models of `dbt/` are the build's own text; dbt-duckdb hands them to DuckDB
as written, which has no `IFF` or `TRY_TO_NUMBER`. `validate_build.py` runs the same SQL through
`DuckDBBackend`, which translates it with sqlglot; so this runs a COPY of the project in a temporary directory in
which each model's body and hooks are translated the same way (`DuckDBBackend.translate`, the `{{ ref() }}`,
`{{ source() }}` and `{{ this }}` of the model kept as they are), and everything else -- configs, tests, profile,
sources -- is the delivered file. To be sure it is the delivered project that is judged, the files on disk must be
exactly what `export_dbt.py` writes from `build.sql` now (a file edited by hand, or left over from an older build,
is a FAIL naming it, and dbt never starts).

**Advisories, not failures.** The generated `not_null` and `unique` tests are `severity: warn`, exactly as
`compare.py` treats a golden NULL in a NOT NULL column and a repeating key (`nullability_contract`,
`keys_not_unique`: advisories, never a failure): dbt exits 0 for a warning, the set is judged by the golden
comparison alone, and every test that warned is named in the report's `advisories` -- one
`{"kind": "dbt_test_warned", "test", "golden_set", "message"}` each, on the set's report (only when there is one) and,
over all sets, on `validation.json`. The CLI prints them as `advisory:` lines. A test that FAILs (dbt status `fail` or
`error`) is still a FAIL of the set, naming it.

**Idempotency.** EVERY golden set's project is run a second time in a fresh sandbox loaded from the same files with
every input and `targets_before` table in REVERSED row order, so a model that depends on the order its input
arrives in (a `LIMIT` without `ORDER BY`, a window without a total order: Snowflake promises no row order) or on
`RANDOM()` gives other rows, whichever set shows it; it is idempotent when every output table holds the same rows
both times, as a multiset. Each set's report carries its own `idempotent` / `idempotency_diff`; `validation.json`'s
`idempotent` is the AND over the sets (`lib.validation.idempotency_of`). The second sandbox is deleted once compared.

**A dbt failure is a domain FAIL, not a crash.** `dbt build` exiting non-zero (a model that does not run, a test
that fails) FAILs that set with an `error` naming the failed models and tests from dbt's own `run_results.json`
and the first message; so does a project that is not the export, or a build with no dbt form, and an output
table dbt never made. A missing prerequisite is a *usage* error, exit 2, with nothing written: no objective (or
one that is T3, has no outputs, or leaves an output with no logical name, write mode or `tool_id`), no
`intake/mappings.yaml`, no `dbt/` project, no golden sets, a missing golden file, no dbt console script beside this
interpreter. Every stale `dbt/validation*.json` is deleted before anything else runs, so a usage error or a crash
leaves no report, never a stale one.

**Skipped** (exit 0, a line saying why, nothing written) exactly where `export_dbt.py` skips: `program.dbt: false`
in `mappings/global.yaml`, or a Python workflow. There is no `--backend snowflake`: dbt-snowflake is not installed
here, and nothing in this repository connects to Snowflake; a person runs the command `deploy.py --dbt` prints.

Exit codes: 0 for `PASS` or `PASS_WITH_ACCEPTED_DIFF` (or a skip), 1 for `FAIL` (the first problem is on stderr,
one line, which is what the orchestrator records as `dbt: <line>`), 2 for a usage error or a crash.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

import compare
import export_dbt
import load_golden
from lib import dbt_project, objective as objective_lib, validation as v
from lib.backend import SANDBOX_DB, BackendError, DuckDBBackend
from lib.dbt_project import DbtResult, DbtUnavailable
from lib.io import load_manifest, read_yaml
from lib.paths import Repo, add_root_arg
from lib.typed_csv import read_table
from lib.write_modes import ContractError, objective_targets

LANGUAGE = "sql"
_JINJA = re.compile(r"\{\{.*?\}\}", re.DOTALL)
#: What may sit inside `dbt/` without the project being "not the export": dbt's own scratch (git-ignored), a DuckDB
#: file a person's own local run left, and the reports this script writes.
_NOT_THE_PROJECT = ("logs/", "target/", "dbt_packages/")


class DbtRunFailed(Exception):
    """`dbt build` exited non-zero: a domain FAIL for the set (a model that does not run, a test that fails)."""


class ProjectIsNotTheExport(Exception):
    """The files of `dbt/` are not what `export_dbt.py` writes from `build.sql` now (or the build has no dbt form):
    a domain FAIL, and dbt never starts."""


class Skipped(Exception):
    """There is no dbt project to validate (`export_dbt.skip_reason`): exit 0, nothing written."""


# --- the project as dbt-duckdb can run it -------------------------------------------------------------------------


def translate(translator: DuckDBBackend, sql: str) -> str:
    """`sql` -- a model body or a hook, Snowflake SQL with `{{ ref() }}` / `{{ source() }}` / `{{ this }}` in it --
    as DuckDB SQL, by the same translation `validate_build.py` runs every statement through. Each Jinja
    expression is hidden behind an identifier while sqlglot reads the SQL and put back after, so a table named
    through dbt stays named through dbt. `BackendError` when it cannot be expressed in DuckDB."""
    hidden: list[str] = []

    def hide(match: re.Match) -> str:
        hidden.append(match.group(0))
        return f"jinja__{len(hidden) - 1}__"

    translated = translator.translate(_JINJA.sub(hide, sql))
    return re.sub(r"jinja__(\d+)__", lambda match: hidden[int(match.group(1))], translated)


def duckdb_model(translator: DuckDBBackend, model: export_dbt.Model) -> export_dbt.Model:
    """`model` with its body and hooks translated for DuckDB (`translate`); its config is unchanged."""
    return replace(model, body=translate(translator, model.body),
                   pre_hooks=[translate(translator, hook) for hook in model.pre_hooks],
                   post_hooks=[translate(translator, hook) for hook in model.post_hooks])


def write_duckdb_project(project: Path, models: Sequence[export_dbt.Model], into: Path) -> None:
    """The project at `into`: every file of `project` that is not a model as it is, every model translated.
    `BackendError` naming the model when one cannot be expressed in DuckDB."""
    (into / "models").mkdir(parents=True)
    for name in ("dbt_project.yml", "profiles.yml", "models/sources.yml", "models/schema.yml"):
        shutil.copyfile(project / name, into / name)
    translator = DuckDBBackend()
    try:
        for model in models:
            try:
                translated = duckdb_model(translator, model)
            except BackendError as exc:
                raise BackendError(f"models/{model.name}.sql cannot be expressed in DuckDB: {str(exc).splitlines()[0]}") from exc
            (into / "models" / f"{model.name}.sql").write_text(export_dbt.model_sql(translated), encoding="utf-8",
                                                               newline="\n")
    finally:
        translator.close()


def _differences(project: Path, expected: dict[str, str]) -> str | None:
    """The first way the files of `project` differ from `expected` (path -> text), or None."""
    present = {path.relative_to(project).as_posix(): path for path in sorted(project.rglob("*")) if path.is_file()}
    present = {name: path for name, path in present.items()
               if not name.startswith(_NOT_THE_PROJECT) and not name.endswith((".duckdb", ".duckdb.wal"))
               and not re.fullmatch(r"validation(\.[^/]+)?\.json", name)}
    for name, text in expected.items():
        if name not in present:
            return f"dbt/{name} is missing"
        if present[name].read_text(encoding="utf-8") != text:
            return f"dbt/{name} differs from the file export_dbt.py writes"
    for name in present:
        if name not in expected:
            return f"dbt/{name} is not a file export_dbt.py writes"
    return None


# --- prerequisites -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Job:
    """Everything one validation needs, every prerequisite already checked."""
    repo: Repo
    wf_id: str
    project: Path
    outputs: list[dict]                  # the objective's outputs
    contracts: list[dict]                # `contract_view` of each, in the same order
    models: list[export_dbt.Model]
    sets: list[str]
    tolerances: dict
    accepted_classes: Sequence[str]
    approvals: list[dict]
    problem: str | None                  # the project is not the export (or the build has no dbt form): every set FAILs


def _prepare(repo: Repo, wf_id: str, golden_sets: Sequence[str] | None) -> _Job:
    """Every prerequisite of a validation, checked before any sandbox exists. `Skipped` where the export is;
    `FileNotFoundError` / `ValueError` / `ContractError` / `UsageError` / `DbtUnavailable` (usage errors, exit 2)
    for anything missing or malformed."""
    reason = export_dbt.skip_reason(repo, wf_id)
    if reason is not None:
        raise Skipped(reason)
    objective = objective_lib.load(repo, wf_id)
    if objective.get("tier") == "T3":
        raise ValueError(f"{wf_id}: objective.json says tier T3, so the workflow is not translated and has no dbt project")
    if not objective.get("outputs"):
        raise ValueError(f"{wf_id}: objective.json has no outputs[] to validate; expected at least one")
    objective_targets(objective)                       # a ContractError names the bad output
    outputs = [dict(output) for output in objective["outputs"]]
    for output in outputs:
        v.golden_output_path(repo, wf_id, "normal", output)           # its tool_id, before any set is looked at
    contracts = [objective_lib.contract_view(objective, output) for output in outputs]

    project = dbt_project.project_dir(repo, wf_id)
    if not (project / "dbt_project.yml").is_file():
        raise FileNotFoundError(f"{wf_id} has no dbt project to validate: {project / 'dbt_project.yml'}; run "
                                f"python scripts/export_dbt.py {wf_id} first")
    if not repo.wf(wf_id, "intake", "mappings.yaml").is_file():
        raise FileNotFoundError(f"{wf_id} has no intake/mappings.yaml to load golden data against: "
                                f"{repo.wf(wf_id, 'intake', 'mappings.yaml')}; run intake first: "
                                f"python scripts/intake_prompt.py {wf_id}")
    manifest = load_manifest(repo, wf_id)
    known = list(manifest.get("golden_sets") or [])
    sets = list(dict.fromkeys(golden_sets)) if golden_sets is not None else known
    if not sets:
        raise ValueError(f"{wf_id} has no golden sets to validate against (manifest.json's golden_sets is empty and "
                         f"none were given with --set); run dev/build_samples.py or inject_outputs.py first")
    for golden_set in sets:
        if not repo.wf(wf_id, "golden", "inputs", golden_set).is_dir():
            raise ValueError(f"{wf_id} has no golden set {golden_set!r}: {repo.wf(wf_id, 'golden', 'inputs', golden_set)} "
                             f"is not a directory; expected one of {', '.join(known) or '(none recorded)'}")
        for output in outputs:
            golden = v.golden_output_path(repo, wf_id, golden_set, output)
            if not golden.is_file():
                raise FileNotFoundError(f"golden output of tool {output['tool_id']} ({output['logical']}) of "
                                        f"{wf_id}/{golden_set} is missing: {golden}")
    settings = (read_yaml(repo.global_mappings) if repo.global_mappings.is_file() else None) or {}
    load_golden.check_names(repo, wf_id, sets)
    for output in outputs:
        v.build_output_table(SANDBOX_DB, output["logical"])
    dbt_project.dbt_executable()                       # DbtUnavailable before anything runs

    # What is judged is the delivered project: the models come from the conversion of build.sql, and the files on
    # disk have to be exactly that conversion. An export error is a usage error; a build with no dbt form is not.
    models: list[export_dbt.Model] = []
    problem: str | None = None
    try:
        exported = export_dbt.export(repo, wf_id, write=False)
        models = exported.models
        mismatch = _differences(project, exported.files)
        if mismatch is not None:
            problem = f"the dbt project on disk is not the export of build.sql: {mismatch}; run python scripts/export_dbt.py {wf_id}"
    except export_dbt.NoDbtForm as exc:
        problem = f"{exc}; run python scripts/export_dbt.py {wf_id} once the build has a dbt form"
    approvals = [a for a in (manifest.get("accepted_diffs") or []) if a.get("segment") is None]
    return _Job(repo=repo, wf_id=wf_id, project=project, outputs=outputs, contracts=contracts, models=models, sets=sets,
                tolerances=settings.get("tolerances") or compare.DEFAULT_TOLERANCES,
                accepted_classes=settings.get("accepted_diff_classes") or (), approvals=approvals, problem=problem)


def clear_stale_reports(repo: Repo, wf_id: str) -> None:
    """Deletes `dbt/validation.json` and every `dbt/validation.<set>.json` before anything else runs, so a report
    on disk always belongs to the most recent invocation (`lib.validation.clear_stale_build_reports`, pointed at
    the project's directory)."""
    v.clear_stale_build_reports(repo, wf_id, directory=dbt_project.project_dir(repo, wf_id))


# --- one golden set ----------------------------------------------------------------------------------------------


def _run_error(result: DbtResult, golden_set: str) -> str:
    named = []
    if result.failed_models:
        named.append(f"failed models: {', '.join(result.failed_models)}")
    if result.failed_tests:
        named.append(f"failed tests: {', '.join(result.failed_tests)}")
    if result.skipped_models:
        named.append(f"skipped: {', '.join(result.skipped_models)}")
    detail = next((r["message"] for r in result.results
                   if r["status"] in dbt_project.FAILED_STATUSES and r["message"]), None) or dbt_project.tail(result.output)
    return (f"dbt build exited {result.code} on golden set {golden_set}: "
            f"{'; '.join(named) or 'no model or test named by dbt'}. {dbt_project.bounded(' '.join(detail.split()))}")


def _build(job: _Job, project: Path, golden_set: str, sandbox: Path, log: str, *, reverse: bool = False) -> DbtResult:
    """A fresh sandbox at `sandbox` loaded as `load_golden.load_set` loads one (any stale file and its `.wal` first;
    `reverse` for the idempotency re-run), then `dbt build` on `project` there."""
    for stale in (sandbox, sandbox.with_name(sandbox.name + ".wal")):
        if stale.exists():
            stale.unlink()
    backend = DuckDBBackend(str(sandbox))
    try:
        load_golden.load_set(backend, job.repo, job.wf_id, golden_set, reverse=reverse)
    finally:
        backend.close()
    return dbt_project.run_dbt("build", project, vars=dbt_project.local_vars(load_golden.golden_view_schema(job.wf_id, golden_set)),
                               duckdb_path=sandbox, log_file=job.project / "logs" / f"{log}.log")


def _tables(job: _Job) -> list[str]:
    """The table each output's model is read back from: `MIGDB.MIG_WORK.<LOGICAL>`."""
    return [v.build_output_table(SANDBOX_DB, output["logical"]) for output in job.outputs]


def _judge(job: _Job, golden_set: str, backend: DuckDBBackend) -> dict:
    """The combined per-output report for one set: each golden output loaded into `backend` and compared, with
    `compare.compare`, with the table dbt made. A table that is not there is that output's own FAIL, naming it."""
    output_reports: list[tuple[dict, dict]] = []
    for index, (output, table) in enumerate(zip(job.outputs, _tables(job))):
        expected = v.expected_fqn(index)
        backend.load_table(expected, read_table(v.golden_output_path(job.repo, job.wf_id, golden_set, output)))
        view = job.contracts[index]
        if backend.table_exists(table):
            report = compare.compare(backend, expected, table, view, job.tolerances, output=view["output"],
                                     accepted_classes=job.accepted_classes, approvals=job.approvals,
                                     segment_dag=None, golden_set=golden_set)
        else:
            report = v.missing_table_report(table, ran="the dbt project")
        output_reports.append((view["output"], report))
    return v.combine_outputs(golden_set, output_reports, language=LANGUAGE)


def _snapshot(job: _Job, sandbox: Path) -> dict:
    backend = DuckDBBackend(str(sandbox))
    try:
        return v.snapshot(backend, _tables(job))
    finally:
        backend.close()


def _advisories(result: DbtResult, golden_set: str) -> list[dict]:
    """The generated tests that warned on this set: advisories about the objective's contract, never a failure."""
    return [{"kind": "dbt_test_warned", "test": warned["name"], "golden_set": golden_set,
             "message": " ".join(warned["message"].split())} for warned in result.warned_tests]


def _run_set(job: _Job, project: Path, golden_set: str) -> dict:
    started = time.perf_counter()
    sandbox = dbt_project.sandbox_path(job.repo, job.wf_id, golden_set)
    result = _build(job, project, golden_set, sandbox, f"validate_{golden_set}")
    if result.code != 0:
        report = v.build_fail_report(golden_set, DbtRunFailed(_run_error(result, golden_set)), language=LANGUAGE)
        return _finish(report, started)
    backend = DuckDBBackend(str(sandbox))
    try:
        report = _judge(job, golden_set, backend)
    finally:
        backend.close()
    advisories = _advisories(result, golden_set)
    if advisories:
        report["advisories"] = advisories
    first = _snapshot(job, sandbox)
    rerun = dbt_project.sandbox_path(job.repo, job.wf_id, golden_set, "_rerun")
    again = _build(job, project, golden_set, rerun, f"validate_{golden_set}_rerun", reverse=True)
    tables = _tables(job)
    # A second run that did not complete counts every output as diverging: idempotency is never claimed
    # without both runs compared.
    diverging = tables if again.code != 0 else v.diverging_tables(first, _snapshot(job, rerun), tables)
    for path in (rerun, rerun.with_name(rerun.name + ".wal")):
        if path.exists():
            path.unlink()
    report["idempotent"], report["idempotency_diff"] = not diverging, diverging
    if diverging:
        report["verdict"] = v.worst_verdict([report["verdict"], "FAIL"])
    return _finish(report, started)


def _finish(report: dict, started: float) -> dict:
    report["runtime_ms"] = int(round((time.perf_counter() - started) * 1000))
    return report


# --- the whole workflow ------------------------------------------------------------------------------------------


def validate_dbt(repo: Repo, wf_id: str, golden_sets: Sequence[str] | None = None) -> dict:
    """Validates `workflows/<wf_id>/dbt/` against every named golden set (default: `manifest.golden_sets`), writes
    `dbt/validation.<set>.json` for each and `dbt/validation.json` for the project as a whole, and returns that
    top-level report. `Skipped` where there is no project to validate. Before anything else -- before even the
    prerequisite checks -- every `dbt/validation*.json` already on disk is deleted, so a report on disk always
    belongs to the most recent invocation; a missing prerequisite raises (a usage error for the CLI) and leaves
    no report. Every set is fully processed before anything is written."""
    repo.wf(wf_id)
    clear_stale_reports(repo, wf_id)
    job = _prepare(repo, wf_id, golden_sets)

    reports: dict[str, dict] = {}
    if job.problem is not None:
        error = ProjectIsNotTheExport(job.problem)
        reports = {golden_set: v.build_fail_report(golden_set, error, language=LANGUAGE) for golden_set in job.sets}
        return _write(job, reports)
    for stale in repo.wf(wf_id).glob("dbt_sandbox_*.duckdb*"):
        stale.unlink()
    with tempfile.TemporaryDirectory(prefix="dbt-project-") as tmp:
        project = Path(tmp) / "project"
        try:
            write_duckdb_project(job.project, job.models, project)
        except BackendError as exc:                     # the build file passed the same translation: unlikely, never a crash
            reports = {golden_set: v.build_fail_report(golden_set, exc, language=LANGUAGE) for golden_set in job.sets}
            return _write(job, reports)
        for golden_set in job.sets:
            reports[golden_set] = _run_set(job, project, golden_set)
    # Every set was re-run on its own; `write_build_reports` makes the top-level `idempotent` their AND.
    return _write(job, reports)


def _write(job: _Job, reports: dict[str, dict]) -> dict:
    """`lib.validation.write_build_reports`, into `dbt/` instead of the workflow's directory. The top-level report
    carries the advisories of every set (the body it is drawn from is one set's)."""
    advisories = [advisory for golden_set in job.sets for advisory in reports[golden_set].get("advisories") or []]
    return v.write_build_reports(job.repo, job.wf_id, job.sets, reports, language=LANGUAGE, directory=job.project,
                                 extra={"advisories": advisories} if advisories else None)


# --- CLI ---------------------------------------------------------------------------------------------------------


def first_problem(report: dict) -> str | None:
    """One line on what is wrong with a FAILing report -- the dbt error, the failed idempotency, or the first diff
    cluster -- as the orchestrator records it (`dbt: <line>`); None for a passing one."""
    if report["verdict"].startswith("PASS"):
        return None
    if report.get("error"):
        return f"{report['golden_set']}: {str(report['error']).splitlines()[0]}"
    if report.get("idempotent") is False:
        return f"{report['golden_set']}: not idempotent: {', '.join(report['idempotency_diff'])} hold other rows on a second run"
    cluster = (report.get("diff_clusters") or [None])[0]
    if cluster:
        columns = ", ".join(cluster.get("columns") or []) or "rows"
        return (f"{report['golden_set']}: {cluster.get('stream')}: {cluster['class']} in {columns} "
                f"({cluster.get('count')} rows) differs from the golden output")
    return f"{report['golden_set']}: {report['verdict']}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id", help="workflow id, e.g. wf_0001")
    parser.add_argument("--set", dest="sets", action="append", default=None,
                        help="golden set to validate (repeatable); default: manifest.golden_sets")
    add_root_arg(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Not parser.error(): main() is called in-process by callers that expect an int back.
    try:
        report = validate_dbt(Repo(args.root), args.wf_id, args.sets)
    except Skipped as exc:
        print(f"{args.wf_id}: dbt validation skipped: {exc}")
        return 0
    except (FileNotFoundError, ValueError, ContractError, export_dbt.UsageError, DbtUnavailable) as exc:
        print(f"cannot validate {args.wf_id}: {exc}", file=sys.stderr)       # exit 2: nothing was written
        return 2
    except Exception:  # noqa: BLE001 -- exit 2 is "anything unexpected"; never a bare traceback exit 1
        traceback.print_exc()
        return 2

    print(f"{args.wf_id}: {report['verdict']} {report['sets']} (idempotent={report['idempotent']})")
    for advisory in report.get("advisories") or []:
        print(f"  advisory: dbt test {advisory['test']} warned on golden set {advisory['golden_set']}: {advisory['message']}")
    problem = first_problem(report)
    if problem:
        print(f"  {problem}", file=sys.stderr)
    return 0 if report["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
