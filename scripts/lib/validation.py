"""Validation-report shaping for `validate_build.py` (program spec §9; objective-first design §4.8).

The workflow-level build report is one report per golden set for the ONE build file of a workflow
(`combine_outputs`, `build_fail_report`), written to `workflows/<wf>/validation.json` and
`validation.<set>.json` (`write_build_reports`), the previous invocation's reports removed first
(`clear_stale_build_reports`). Everything about *shaping* a result is here, not in the script: how the several
`compare.py` reports for one golden set's outputs combine into one report (`combine_outputs`), how a declared
output table the build never created becomes its own kind of FAIL report (`missing_table_report`), how an
exception while running the build becomes a domain FAIL (`build_fail_report`), how several golden sets' own
reports aggregate into one top-level verdict (`aggregate_sets`; findings K1/K2/C3/C4/I5), and how a report on
disk always belongs to the most recent invocation.

Every validator's engine choice is shared here too (plan Task P2): `add_backend_args` gives each CLI
the same `--backend duckdb|snowflake`, `--connection`, `--sandbox-database`; `snowflake_target`
turns them into nothing (the DuckDB default) or a checked, not yet connected `SnowflakeSandbox`;
`snapshot` / `diverging_tables` compare two runs that share one sandbox database; `error_text` /
`print_crash` redact what a CLI prints on the Snowflake path.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Sequence

import compare
from .backend import ident
from .io import write_json
from .paths import Repo

#: Where a golden CSV / actual-table snapshot is loaded before compare() runs, one table per
#: output (compare.py's own convention is a single `MIG_COMPARE.EXPECTED`; a workflow can have
#: several outputs to compare in the same backend, so each gets its own numbered table).
EXPECTED_SCHEMA = "MIG_COMPARE"

#: `load_golden.WORK_SCHEMA`'s value, duplicated here (rather than imported): `load_golden.py`
#: imports `lib.backend` at its own top level, so importing `load_golden` back from here -- a `lib`
#: module -- would be a genuine import cycle the first time either module loads first.
_WORK_SCHEMA = "MIG_WORK"

VERDICT_SEVERITY = {"PASS": 0, "PASS_WITH_ACCEPTED_DIFF": 1, "FAIL": 2}


# --- which engine a validator runs on (plan Task P2) -----------------------------------------------
#
# `duckdb` (the default) is the local double every test and every agent uses. `snowflake` is a real
# account through a NAMED connection, run by a human: it is selected only by `--backend snowflake`,
# and nothing Snowflake is imported or constructed unless it is (`lib.snowflake_conn` and
# `lib.snowflake_sandbox` are imported lazily, here and in each validator).

BACKENDS = ("duckdb", "snowflake")


def add_backend_args(parser: argparse.ArgumentParser) -> None:
    """`--backend`, `--connection`, `--sandbox-database`: the same three options on every validator.
    There is deliberately no option for a password, token or key -- argparse refuses one."""
    parser.add_argument("--backend", choices=BACKENDS, default="duckdb",
                        help="duckdb: the local double (default). snowflake: a real account through a "
                             "named connection -- run by a human, never by an agent")
    parser.add_argument("--connection", default=None, metavar="NAME",
                        help="--backend snowflake: the connections.toml entry to use "
                             "(default: $MIG_SNOWFLAKE_CONNECTION)")
    parser.add_argument("--sandbox-database", default=None, metavar="DB",
                        help="--backend snowflake: the sandbox database; its MIG_GOLDEN, MIG_WORK and "
                             "MIG_COMPARE schemas are replaced, so it must be listed in "
                             "orchestrator.config.json policy.sandboxDatabases (default: $MIG_SANDBOX_DATABASE)")


def snowflake_target(repo: Repo, backend: str, connection: str | None, sandbox_database: str | None):
    """`None` for the DuckDB double; for `snowflake`, the `SnowflakeSandbox` the run uses -- every
    name checked (the policy's `sandboxDatabases` included) and nothing connected yet. A Snowflake
    option without `--backend snowflake`, or an unknown backend, is a `ValueError` (a usage error)."""
    if backend == "duckdb":
        if connection is not None or sandbox_database is not None:
            raise ValueError("--connection and --sandbox-database only apply with --backend snowflake")
        return None
    if backend != "snowflake":
        raise ValueError(f"unknown backend {backend!r}: expected one of {', '.join(BACKENDS)}")
    from . import snowflake_sandbox  # noqa: PLC0415  (lazy: the DuckDB path never imports it)
    return snowflake_sandbox.sandbox_for(repo.root, connection, sandbox_database)


def error_text(backend: str, text: str) -> str:
    """An error message as a CLI may print it: redacted on the Snowflake path, untouched locally."""
    if backend != "snowflake":
        return text
    from .snowflake_conn import redact  # noqa: PLC0415
    return redact(text)


def print_crash(backend: str) -> None:
    """The exit-2 traceback every validator prints for an unexpected exception -- redacted on the
    Snowflake path, whose exceptions may carry connector text."""
    if backend != "snowflake":
        traceback.print_exc()
        return
    sys.stderr.write(error_text(backend, traceback.format_exc()))


def worst_verdict(verdicts: Sequence[str]) -> str:
    if not verdicts:
        return "PASS"
    return max(verdicts, key=lambda verdict: VERDICT_SEVERITY[verdict])


def expected_fqn(index: int) -> str:
    """The `MIG_COMPARE.EXPECTED_<n>` table one output's golden data is loaded into
    before `compare.compare()` runs (a caller loads whatever it ran the same way, conventionally
    as the matching `MIG_COMPARE.ACTUAL_<n>`)."""
    return f"{EXPECTED_SCHEMA}.EXPECTED_{index}"


# --- one report per golden set, one file per workflow ----------------------------------------------


def missing_table_report(actual_fqn: str, *, ran: str = "the build file") -> dict:
    """The build never created one of its declared output tables (finding C4(a)). This is an *actual*-side failure -- a domain FAIL that feeds the fixer, exactly
    like a compile error -- not compare.py's own `ValueError("there is no table … to compare")`, which is
    reserved for a usage error on the *expected* side. Shaped like one of `compare.compare()`'s own
    per-output reports so `combine_outputs` can merge it the same way, plus an `"error"` naming the table."""
    return {
        "verdict": "FAIL", "error": f"output table {actual_fqn} does not exist after running "
                                    f"{ran}",
        "checks": {}, "diff_clusters": [], "normalizations_applied": [], "needs_human": False,
        "truncated": False,
    }


def ordered_rows(backend, table: str) -> list[tuple]:
    """Every row of `table`, ordered by every column so two runs' outputs can be compared for
    equality as row multisets: sorting by all columns puts duplicate rows next to each other, and
    since duplicates are indistinguishable, their relative order can never affect the comparison."""
    columns = backend.table_columns(table)
    if not columns:
        return []
    order = ", ".join(str(i) for i in range(1, len(columns) + 1))
    return backend.query(f"SELECT * FROM {table} ORDER BY {order}")[1]


def snapshot(backend, tables: Sequence[str]) -> dict[str, list[tuple] | None]:
    """Every table's `ordered_rows`, `None` for a table that does not exist. The idempotency check
    takes the first run's snapshot BEFORE the second run starts: on a real account both runs share
    one sandbox database, and the second run's fresh sandbox replaces the first run's schemas."""
    return {table: (ordered_rows(backend, table) if backend.table_exists(table) else None)
            for table in dict.fromkeys(tables)}


def diverging_tables(first: dict[str, list[tuple] | None], second: dict[str, list[tuple] | None],
                     tables: Sequence[str]) -> list[str]:
    """The tables (in `tables`' order) whose two snapshots differ; a table missing from either run
    counts as diverging -- idempotency is never verified without both runs compared."""
    return [table for table in tables
            if first.get(table) is None or second.get(table) is None or first[table] != second[table]]


def aggregate_sets(sets: Sequence[str], reports: dict[str, dict]) -> dict:
    """Combines every golden set's own report into the workflow-level result written to
    `validation.json` (finding K1's ruling): the verdict is the *worst* across every processed
    set; the body (every other key) is copied from the first set, in processing order, whose own
    verdict equals that worst verdict; `needs_human` is true if *any* set's own report says so,
    regardless of which set supplied the body; `sets` lists every processed set's own verdict. (The
    top-level `idempotent` and `idempotency_diff` are not the body's: `idempotency_of` combines every set's.)"""
    worst = worst_verdict([reports[golden_set]["verdict"] for golden_set in sets])
    chosen = next(reports[golden_set] for golden_set in sets
                 if reports[golden_set]["verdict"] == worst)
    result = dict(chosen)
    result["verdict"] = worst
    result["needs_human"] = any(reports[golden_set]["needs_human"] for golden_set in sets)
    result["sets"] = {golden_set: reports[golden_set]["verdict"] for golden_set in sets}
    return result


def idempotency_of(sets: Sequence[str], reports: dict[str, dict]) -> tuple[bool | None, list[str]]:
    """The run's idempotency from the golden sets' own reports (each set is re-run on its own, inputs reversed):
    False when any set says so, True only when every set was compared and is idempotent, else None (a set whose
    first run did not complete claims nothing); with the tables that diverged in any set, in the order found."""
    flags = [reports[golden_set].get("idempotent") for golden_set in sets]
    diverging = list(dict.fromkeys(table for golden_set in sets for table in reports[golden_set].get("idempotency_diff") or []))
    if any(flag is False for flag in flags):
        return False, diverging
    return (True if flags and all(flag is True for flag in flags) else None), diverging


def _write_set_reports(directory: Path, stem: str, sets: Sequence[str], reports: dict[str, dict],
                       extra: dict | None, aggregate=aggregate_sets) -> dict:
    """The tail every validator shares: every golden set's report carries its OWN `idempotent` /
    `idempotency_diff` (the set was re-run with its inputs reversed), and the top-level `idempotent` is the AND
    over the sets (`idempotency_of`). Aggregates the reports, merges `extra` into the *top-level* result only --
    never into a per-set report -- writes `<stem>.json` and `<stem>.<set>.json` for every set into `directory`,
    and returns the top-level result."""
    result = aggregate(sets, reports)
    result["idempotent"], result["idempotency_diff"] = idempotency_of(sets, reports)
    if extra:
        result.update(extra)

    write_json(directory / f"{stem}.json", result)
    for golden_set in sets:
        write_json(directory / f"{stem}.{golden_set}.json", reports[golden_set])
    return result


# --- the workflow-level build report (objective-first design §4.8) -----------------------------------------------


def build_output_table(database: str, logical: str) -> str:
    """The table a build file's `{{tgt}}.<LOGICAL>` is written to: `<database>.MIG_WORK.<LOGICAL>` --
    `MIGDB` for a local run (matching `load_golden`'s `WORK_SCHEMA`), the sandbox database on
    `--backend snowflake`. Spliced into SQL on both backends, so every component must be a plain identifier
    (`lib.backend.ident`): a `ValueError` naming the value otherwise."""
    return f"{ident(database, 'database')}.{_WORK_SCHEMA}.{ident(logical, 'output logical name')}"


def golden_output_path(repo: Repo, wf_id: str, golden_set: str, output: dict) -> Path:
    """`golden/outputs/<set>/<tool_id>.csv`: the golden file of one objective output, named by its Output
    tool. A `ValueError` when the output has no `tool_id`, and one from `Repo` for a hostile value."""
    tool_id = output.get("tool_id")
    if not tool_id:
        raise ValueError(f"{wf_id}: objective.json output {output.get('logical')!r} has no tool_id; expected the "
                         f"Output tool's id, which names its golden file golden/outputs/<set>/<tool_id>.csv")
    return repo.wf(wf_id, "golden", "outputs", golden_set, f"{tool_id}.csv")


def combine_outputs(golden_set: str, output_reports: list[tuple[dict, dict]], *, language: str) -> dict:
    """One report for a golden set from one `compare.compare()` report (or `missing_table_report`) per objective
    output, each paired with the contract-shaped output (`lib.objective.contract_view`) it judged. Nothing about
    a check or a cluster is re-derived: every count, verdict and classification is exactly what `compare.py`
    produced. Added are `language` (`sql` or `python`), `"stream"` on each cluster (the output's logical name,
    which `contract_view` sets as its stream) and a `"checks"` entry per output, keyed by stream and kind. When
    two or more outputs share the same stream:kind key, `:<tool_id>` is appended to EVERY occurrence of that
    colliding key so both checks survive (ruling R-B1); a key that never collides is left as it is. Any
    per-output `"error"` (a missing actual table) is carried up too, joined if there is more than one. The
    set's verdict is the worst output verdict, `needs_human` is true when any output says so."""
    verdict = worst_verdict([report["verdict"] for _, report in output_reports])
    clusters: list[dict] = []
    for output, report in output_reports:
        for cluster in report["diff_clusters"]:
            tagged = dict(cluster)
            tagged["stream"] = output.get("stream")
            clusters.append(tagged)
    clusters.sort(key=lambda cluster: (compare.CLASS_ORDER.index(cluster["class"]),
                                       cluster.get("stream") or "", cluster["columns"]))
    keys = [f"{output.get('stream')}:{output.get('kind')}" for output, _ in output_reports]
    repeated = {key for key in keys if keys.count(key) > 1}
    checks = {(f"{key}:{output.get('tool_id')}" if key in repeated else key): report["checks"]
              for key, (output, report) in zip(keys, output_reports)}
    normalizations = sorted({op for _, report in output_reports
                             for op in report["normalizations_applied"]})
    errors = [report["error"] for _, report in output_reports if report.get("error")]
    result = {
        "golden_set": golden_set,
        "language": language,
        "verdict": verdict,
        "checks": checks,
        "diff_clusters": clusters,
        "normalizations_applied": normalizations,
        "idempotent": None,             # filled in by the validator, once per golden set
        "idempotency_diff": [],         # filled in by the validator, once per golden set
        "runtime_ms": 0,                # filled in by the caller: wall time for the whole set
        "credits": None,                # a local run burns no Snowflake credits
        "needs_human": any(report["needs_human"] for _, report in output_reports),
        "truncated": any(report.get("truncated") for _, report in output_reports),
    }
    if errors:
        result["error"] = "; ".join(errors)
    return result


def build_fail_report(golden_set: str, error: Exception, *, language: str) -> dict:
    """Running the build raised (a SQL compile or runtime error, a Python exception from `main`, a refusal by the
    Snowpark rules): a domain FAIL that feeds the fixer (program spec §9), not a human escalation, so
    `needs_human` stays false. The first run itself never completed, so no second run was attempted either:
    `idempotent` stays `None` (finding K2's documented exception -- it must never become `True` without both
    runs being compared)."""
    return {
        "golden_set": golden_set, "language": language, "verdict": "FAIL",
        "error": str(error), "checks": {}, "diff_clusters": [], "normalizations_applied": [],
        "idempotent": None, "idempotency_diff": [], "runtime_ms": 0, "credits": None,
        "needs_human": False, "truncated": False,
    }


def clear_stale_build_reports(repo: Repo, wf_id: str, *, directory: Path | None = None) -> None:
    """Deletes the workflow's `validation.json` and every `validation.<set>.json` before anything else runs, so
    a report on disk always belongs to the most recent invocation: a usage error or a crash leaves no report
    at all (the orchestrator reads `validation.json`'s verdict only when the file exists), and a golden set no
    longer requested loses its own leftover report. Only those two exact shapes at the workflow root go --
    `dbt/validation.json` (the dbt export's) is another file, and `directory=workflows/<wf>/dbt` is how
    `validate_dbt.py` clears it."""
    wf_dir = repo.wf(wf_id) if directory is None else directory
    if not wf_dir.is_dir():
        return
    exact = wf_dir / "validation.json"
    if exact.is_file():
        exact.unlink()
    for path in wf_dir.glob("validation.*.json"):
        if path.is_file():
            path.unlink()


def write_build_reports(repo: Repo, wf_id: str, sets: Sequence[str], reports: dict[str, dict], *, language: str,
                        directory: Path | None = None, extra: dict | None = None) -> dict:
    """The tail of `validate_build()`: aggregates the sets' reports (`aggregate_sets`: the worst verdict across
    sets, the body of the first set with that verdict, `needs_human` if any set says so, `sets`; `idempotent` the
    AND over the sets' own, `idempotency_diff` the tables that diverged in any set), writes
    `workflows/<wf>/validation.json` and `validation.<set>.json` for every set, and returns the top-level
    report. `language` is on every report already; it is repeated on the top-level one so it holds even when
    `sets` is empty. `directory` names another place to write them (`validate_dbt.py` writes into the
    workflow's `dbt/`); the shapes are the same. `extra` is merged into the top-level report only
    (`validate_dbt.py` puts the dbt tests' advisories of every set there)."""
    return _write_set_reports(repo.wf(wf_id) if directory is None else directory, "validation", sets, reports,
                              {"language": language, **(extra or {})})
