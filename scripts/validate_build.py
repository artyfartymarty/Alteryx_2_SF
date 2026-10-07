"""Golden-data parity validation of a workflow's build file (objective-first design §4.8).

    python scripts/validate_build.py <wf_id> [--set NAME]... [--root .]
        [--backend duckdb|snowflake] [--connection NAME] [--sandbox-database DB] [--sandbox-timeout SECONDS]

Does the one build file produce the golden outputs? This is the deterministic core the translate loop runs as written (`orchestrator/stages.ts` calls it as a
script after the reviewer; the validator agent is retired) -- every parity verdict in the project flows through
`compare.py`, and nothing here invents a number or a verdict. `workflows/<wf_id>/build.sql` (or `build.py`,
whichever `objective.json`'s `language` names) is run against every golden set and each objective output is
judged against its golden file (one file per workflow: no segments, no work tables, no chain, no golden
intermediates).
Nothing here has ever run on a real Snowflake account or Alteryx: locally, "parity" means DuckDB (or the Snowpark
Local Testing Framework) agrees with the Alteryx simulator, which is itself an assumption
(`docs/reference/simulator-semantics.md`).

**One golden set.** A fresh in-memory `DuckDBBackend` (contract C3's local stand-in for Snowflake);
`load_golden.load_set` loads the set (each source as a view under `MIGDB.MIG_GOLDEN_<WF>_<SET>` by its logical
name, each `targets_before` file as `MIGDB.MIG_WORK.<LOGICAL>`); `{{src}}` is rendered to that view schema and
`{{tgt}}` to `MIGDB.MIG_WORK` (`load_golden.build_schemas`); every statement of the file is run in order (an
`ALTER SESSION` is recorded, not applied, on the double -- as it always was -- and sent as written on
`--backend snowflake`); then every objective output is compared with its golden file
(`golden/outputs/<set>/<tool_id>.csv`) by `compare.compare`, given the objective converted, per output, into the
contract shape it reads (`lib.objective.contract_view`: the output's `keys`, `row_relation`, `ordering`,
`tolerances`, `normalizations`; the `stream` in its messages is the output's logical name). `keys_not_unique`,
`nullability_contract`, the update-insert caveat and the `needs_human` rule are `compare.py`'s and come through
unchanged. A `build.py` runs `main(session, src, tgt)` the same way in the Snowpark Local Testing Framework,
inside the runtime sandbox (`lib/snowpark_sandbox.py`: a child process under an audit hook, a timeout, a signed
result), after the Snowpark static rules (`lib.snowpark_rules.check_build_py`) -- a build that breaks one is
refused before any of it runs, every set a FAIL naming the rules.

**Aggregating outputs into one report per golden set.** Each output is compared on its own and the reports are
combined (`lib.validation.combine_outputs`): the set's verdict is the worst output verdict (`FAIL` >
`PASS_WITH_ACCEPTED_DIFF` > `PASS`), `needs_human` is true if any output's report says so, and every diff
cluster is carried over with `"stream"` added, the output it came from. The report is the old segment report
without its `segment` key and with `"language"`. The top-level `validation.json` takes the worst verdict across
every processed set and the body of the first set with that verdict (`lib.validation.aggregate_sets`);
`sets` lists every set's own verdict.

**Idempotency.** An `append` output legitimately doubles if the same build runs twice against the same state, so
"run twice without resetting" is never the test. EVERY golden set's build is run twice, each time from scratch in
its own fresh backend (or sandbox child) with the same golden data -- but the re-run, SQL or Python, loads every
input and every `targets_before` table in reversed row order, so a build whose result depends on the order its rows
arrive in (a `LIMIT` without `ORDER BY`, a window without a total order, a row number taken from the input order)
is caught too, on whichever set shows it. It is idempotent when every output table holds the same rows -- as a
multiset, order does not matter -- both times. That catches `RANDOM()`, `UNIFORM(...)`, a clock and a dependence
on input row order without being fooled by `append`. Each set's report carries its own `idempotent` and
`idempotency_diff`; a set that is not idempotent FAILs, with the output tables that differ in its
`idempotency_diff` (a table missing on either run counts: idempotency is never claimed without both runs
compared). `validation.json`'s `idempotent` is the AND over the sets (`lib.validation.idempotency_of`), its
`idempotency_diff` the tables that diverged in any set. A set's `idempotent` is None only when its first run did not
complete, and then the top-level one is None unless another set already said False.

**Errors.** A compile or runtime error in the build (a `BackendError`, a Python exception from `main`, a refused
sandbox event, a read-back failure) is a *domain* failure -- the build is wrong, which is exactly what feeds the
fixer -- so that set's report is `{"verdict": "FAIL", "error": "<message>", ...}`, processing continues and the
exit code is 1. So is an objective output the build never created (the report names the table), and a build file
of the wrong language. A missing or unreadable prerequisite is a *usage* error, exit 2, with nothing written: no
objective (or one that is not tier T1/T2, has no outputs, or leaves an output with no logical name, write mode or
`tool_id`), no build file or both, no `intake/mappings.yaml`, an unknown golden set, a missing golden file. Every
prerequisite is checked, and every set fully processed in memory, before anything is written to disk.

**A report on disk always belongs to the most recent invocation.** Before anything else -- before even the
prerequisite checks -- `validate_build()` deletes the workflow's `validation.json` and every
`validation.<set>.json`, so a usage error or a crash leaves no report at all (the orchestrator reads
`validation.json`'s `verdict` and `needs_human` only if the file exists; no file is `script-error`) and a set that
is no longer requested loses its own leftover report.

**`--backend snowflake`.** Selected only by that flag, run by a human: each run gets a fresh sandbox in a
sandbox database the policy lists (`lib.snowflake_sandbox`), reached through a NAMED connection
(`lib.snowflake_conn`); the golden set is loaded there, a `build.sql` is run statement by statement and a
`build.py` is called in a Snowpark session built from the connection, and every output is judged with the same
`compare.py` into the same report. The two runs share that database, so the first run's outputs are
snapshotted before the second run's fresh sandbox replaces them. Never exercised against a real account
(`docs/reference/snowflake-backend.md`); the tests drive it through `tests/fake_snowflake.py`.

Exit codes: 0 when the verdict is `PASS` or `PASS_WITH_ACCEPTED_DIFF`, 1 for `FAIL`, 2 for a usage error or a
crash.
"""
from __future__ import annotations

import argparse
import sys
import time
import types
from collections import Counter
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Sequence

import compare
import load_golden
from lib import build_sql, objective as objective_lib, snowpark_rules, snowpark_tables, validation as v
from lib.backend import SANDBOX_DB, BackendError, DuckDBBackend
from lib.io import load_manifest, read_yaml
from lib.paths import Repo, add_root_arg
from lib.typed_csv import read_table
from lib.write_modes import objective_targets

SQL, PYTHON = "sql", "python"
BUILD_FILES = {SQL: "build.sql", PYTHON: "build.py"}


class RulesRefused(Exception):
    """A `build.py` that breaks the Snowpark rules `compile_check.py` applies: it is never imported, so none of
    its code runs. A domain failure -- the build is wrong -- which a report carries as its `error`, naming every
    rule."""

    def __init__(self, name: str, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__(f"{name} breaks the Snowpark rules `compile_check.py` applies, so it was not imported or "
                         f"run: {'; '.join(self.errors)}")


@dataclass(frozen=True)
class _Job:
    """Everything one validation needs, every prerequisite already checked."""
    repo: Repo
    wf_id: str
    objective: dict
    outputs: list[dict]                  # the objective's outputs, each with a logical name and a tool_id
    contracts: list[dict]                # `contract_view` of each output, in the same order
    path: Path
    text: str
    language: str
    sets: list[str]
    tolerances: dict
    accepted_classes: Sequence[str]
    approvals: list[dict]
    sandbox: object | None               # a `SnowflakeSandbox` for --backend snowflake, else None (the local double)
    database: str
    sandbox_timeout: float | None
    refused: RulesRefused | None         # a build.py that breaks a Snowpark rule; None for build.sql
    mismatch: str | None                 # the build file is not the language the objective names

    @property
    def tables(self) -> list[str]:
        """The table each output's `{{tgt}}.<LOGICAL>` lands in, in output order."""
        return [v.build_output_table(self.database, output["logical"]) for output in self.outputs]


# --- prerequisites ---------------------------------------------------------------------------------------------


def _prepare(repo: Repo, wf_id: str, golden_sets: Sequence[str] | None, backend: str, connection: str | None,
             sandbox_database: str | None, sandbox_timeout: float | None) -> _Job:
    """Every prerequisite of a validation, checked before any backend exists. `FileNotFoundError` / `ValueError`
    (a usage error, exit 2) for anything missing or malformed; the previous invocation's reports are already
    gone."""
    sandbox = v.snowflake_target(repo, backend, connection, sandbox_database)
    objective = objective_lib.load(repo, wf_id)
    if objective.get("tier") == "T3":
        raise ValueError(f"{wf_id}: objective.json says tier T3, so the workflow is not translated and has no build "
                         f"file to validate")
    language = objective.get("language")
    if language not in objective_lib.LANGUAGES:
        raise ValueError(f"{wf_id}: objective.json has language {language!r}; expected one of "
                         f"{', '.join(objective_lib.LANGUAGES)} before a build file can be validated against it")
    path = objective_lib.build_path(repo, wf_id)
    if path is None:
        raise FileNotFoundError(f"{wf_id} has no build file to validate: expected {repo.wf(wf_id, BUILD_FILES[SQL])} "
                                f"(or {BUILD_FILES[PYTHON]} when the objective's language is python); translate it "
                                f"first")
    built = PYTHON if path.suffix == ".py" else SQL
    text = path.read_text(encoding="utf-8-sig")        # a BOM (PowerShell's Out-File) is not part of the file

    if not objective.get("outputs"):
        raise ValueError(f"{wf_id}: objective.json has no outputs[] to validate; expected at least one")
    objective_targets(objective)                       # a ContractError (a ValueError) names the bad output
    outputs = [dict(output) for output in objective["outputs"]]
    for output in outputs:
        v.golden_output_path(repo, wf_id, "normal", output)          # its tool_id, before any set is looked at
    contracts = [objective_lib.contract_view(objective, output) for output in outputs]

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
    database = SANDBOX_DB if sandbox is None else sandbox.database
    # Every name spliced into SQL or a session, checked before any backend exists: a bad one is a usage error and
    # nothing is executed on either backend.
    load_golden.check_names(repo, wf_id, sets, database=database)
    for output in outputs:
        v.build_output_table(database, output["logical"])

    refused = None
    if built == PYTHON and language == PYTHON:
        errors = snowpark_rules.check_build_py(text, objective)          # a ContractError propagates: a usage error
        refused = RulesRefused(path.name, errors) if errors else None
    mismatch = None
    if built != language:
        mismatch = (f"{path.name} is a {built} build but objective.json says language {language!r}; write "
                    f"{BUILD_FILES[language]} and remove {path.name}")
    # An approval names a class and columns, never an output, and the objective has no segments: the ones that
    # apply to a workflow-level comparison carry no `segment`.
    approvals = [a for a in (manifest.get("accepted_diffs") or []) if a.get("segment") is None]
    return _Job(repo=repo, wf_id=wf_id, objective=objective, outputs=outputs, contracts=contracts, path=path,
                text=text, language=built, sets=sets, tolerances=settings.get("tolerances") or compare.DEFAULT_TOLERANCES,
                accepted_classes=settings.get("accepted_diff_classes") or (), approvals=approvals, sandbox=sandbox,
                database=database, sandbox_timeout=sandbox_timeout, refused=refused, mismatch=mismatch)


# --- judging one set's outputs -----------------------------------------------------------------------------------


def _judge(job: _Job, golden_set: str, backend, actual_fqns: Sequence[str | None]) -> dict:
    """The combined per-output report for one set: each golden output loaded into `backend` and compared, with
    `compare.compare`, with the table `actual_fqns[i]` names there. None -- or a table that is not there -- is an
    output the build never created: that output's own FAIL report, naming the table. Nothing about a check or a
    cluster is derived here."""
    tables = job.tables
    output_reports: list[tuple[dict, dict]] = []
    for index, output in enumerate(job.outputs):
        expected = v.expected_fqn(index)
        backend.load_table(expected, read_table(v.golden_output_path(job.repo, job.wf_id, golden_set, output)))
        actual = actual_fqns[index]
        view = job.contracts[index]
        if actual is not None and backend.table_exists(actual):
            report = compare.compare(backend, expected, actual, view, job.tolerances, output=view["output"],
                                     accepted_classes=job.accepted_classes, approvals=job.approvals,
                                     segment_dag=None, golden_set=golden_set)
        else:
            report = v.missing_table_report(tables[index], ran="the build file")
        output_reports.append((view["output"], report))            # tagged with its `stream`: the logical name
    return v.combine_outputs(golden_set, output_reports, language=job.language)


def _judge_tables(job: _Job, golden_set: str, actuals: dict[str, dict | None]) -> dict:
    """`_judge` for a Python run, whose outputs came back as typed tables (`{fqn: table | None}`): they are
    loaded, with the golden outputs, into a fresh local DuckDB, which is what compares them -- the same
    `compare.py`, whichever engine ran the build."""
    compare_backend = DuckDBBackend()
    try:
        actual_fqns: list[str | None] = []
        for index, fqn in enumerate(job.tables):
            table = actuals.get(fqn)
            if table is None:
                actual_fqns.append(None)
                continue
            local = f"{v.EXPECTED_SCHEMA}.ACTUAL_{index}"
            compare_backend.load_table(local, table)
            actual_fqns.append(local)
        return _judge(job, golden_set, compare_backend, actual_fqns)
    finally:
        compare_backend.close()


def _row_snapshot(actuals: dict[str, dict | None], fqns: Sequence[str]) -> dict:
    """Every table's rows as a multiset (`Counter`), None for one that is missing: a Python run's outputs, as
    `lib.validation.snapshot` is a SQL run's. Counter hashes rather than orders, so it never trips over
    comparing `None` with a value the way `sorted()` would."""
    return {fqn: (None if actuals.get(fqn) is None else Counter(map(tuple, actuals[fqn]["rows"])))
            for fqn in dict.fromkeys(fqns)}


def _stamp_idempotency(report: dict, idempotent: bool, diverging: list[str]) -> dict:
    """A failed idempotency check FAILS the set whatever `compare()` said about this run's own output."""
    report["idempotent"] = idempotent
    report["idempotency_diff"] = diverging
    if not idempotent:
        report["verdict"] = v.worst_verdict([report["verdict"], "FAIL"])
    return report


def _finish(report: dict, started: float) -> dict:
    report["runtime_ms"] = int(round((time.perf_counter() - started) * 1000))
    return report


# --- a build.sql, one golden set ---------------------------------------------------------------------------------


def _run_statements(backend, statements: Sequence[build_sql.Statement], values: dict[str, str], *,
                    apply_session: bool) -> None:
    """Runs the build's statements in order, `{{src}}`/`{{tgt}}` rendered. An `ALTER SESSION SET ...` is recorded,
    not applied, on the DuckDB double (its session is not Snowflake's; compile_check treats it the same) and sent
    as written on a real account. A statement DuckDB or Snowflake refuses, or a placeholder that cannot be
    rendered, is a `BackendError`: the build is wrong."""
    lock = getattr(backend, "lock_external_access", None)
    if callable(lock):
        # The build is agent-written SQL, and DuckDB can read and write the host's files: its connection loses
        # external access -- for good -- before the first statement runs (live hardening L4 fix round 1, P). The
        # golden data is loaded before this, and every later load inserts rows through Python, never a file.
        lock()
    for statement in statements:
        try:
            sql = build_sql.render(statement.text, values)
        except ValueError as exc:
            raise BackendError(f"line {statement.start_line}: {exc}") from exc
        if build_sql.session_settings(statement) is not None and not apply_session:
            continue
        try:
            backend.execute(sql)
        except BackendError as exc:
            raise BackendError(f"line {statement.start_line}: {exc}") from exc


def _load_and_run_sql(backend, job: _Job, golden_set: str, *, reverse: bool = False) -> None:
    """One complete run against a fresh backend: the golden set, then the build. A missing or malformed golden
    file raises `FileNotFoundError`/`ValueError` (a usage error); a `BackendError` from running the build
    propagates for the caller to turn into a domain FAIL. `reverse` loads every input in reversed row order: the
    idempotency re-run uses it, so a build whose result depends on the order rows arrive in (a `LIMIT` without
    `ORDER BY`, a window without a total order -- Snowflake promises no row order) gives a different answer."""
    load_golden.load_set(backend, job.repo, job.wf_id, golden_set, database=job.database, reverse=reverse)
    src, tgt = load_golden.build_schemas(job.wf_id, golden_set, database=job.database)
    _run_statements(backend, build_sql.split_statements(job.text), {"src": src, "tgt": tgt},
                    apply_session=job.sandbox is not None)


def _sql_backend_factory(job: _Job, golden_set: str) -> Callable[[], object]:
    """A fresh backend per run: a new in-memory `DuckDBBackend` locally, `SnowflakeSandbox.fresh` on a real
    account, where both runs share one sandbox database -- so the first run's outputs are snapshotted before the
    second run's fresh sandbox replaces them."""
    if job.sandbox is None:
        return DuckDBBackend
    return partial(job.sandbox.fresh, [load_golden.golden_view_schema(job.wf_id, golden_set)])


def _run_set_sql(job: _Job, golden_set: str) -> dict:
    started = time.perf_counter()
    tables = job.tables
    factory = _sql_backend_factory(job, golden_set)
    backend = factory()
    try:
        try:
            _load_and_run_sql(backend, job, golden_set)
        except BackendError as exc:
            return _finish(v.build_fail_report(golden_set, exc, language=SQL), started)
        report = _judge(job, golden_set, backend, tables)

        first = v.snapshot(backend, tables)                # before a second fresh sandbox can replace it
        other = factory()
        try:
            try:
                _load_and_run_sql(other, job, golden_set, reverse=True)
            except BackendError:
                # The second run itself could not complete: idempotency can never be verified as True
                # without both runs being compared, and here it did not even run -- every output counts
                # as diverging.
                diverging = list(tables)
            else:
                diverging = v.diverging_tables(first, v.snapshot(other, tables), tables)
        finally:
            other.close()
        _stamp_idempotency(report, not diverging, diverging)
        return _finish(report, started)
    finally:
        backend.close()


# --- a build.py, one golden set ------------------------------------------------------------------------------------


def _run_set_python_local(job: _Job, golden_set: str) -> dict:
    """The local double: the build runs in a sandboxed child process (`lib.snowpark_sandbox`), which loads the
    golden set, runs `main` under the audit hook and reads every output back; a refused host operation, a raise
    or a read-back failure comes back as a FAIL naming the reason."""
    from lib import snowpark_sandbox  # noqa: PLC0415  (lazy: the child is a separate process)
    started = time.perf_counter()
    fqns = job.tables

    def run(*, reverse: bool = False):
        return snowpark_sandbox.run_golden_build(
            source=job.text, display_path=job.path.name, root=str(job.repo.root), wf_id=job.wf_id,
            golden_set=golden_set, database=job.database, outputs=fqns, timeout=job.sandbox_timeout, reverse=reverse)

    result = run()
    if result.kind == "usage":
        # A missing or malformed golden file: a usage error, re-raised so the CLI exits 2 with nothing written.
        raise (FileNotFoundError if result.error_type == "FileNotFoundError" else ValueError)(result.error)
    if result.kind != "ok":
        error = ValueError(snowpark_sandbox.failure_reason(result, "the build"))
        return _finish(v.build_fail_report(golden_set, error, language=PYTHON), started)
    report = _judge_tables(job, golden_set, result.outputs)
    again = run(reverse=True)                      # every input in reversed row order, as the SQL re-run loads it
    if again.kind != "ok":
        diverging = list(fqns)
    else:
        diverging = v.diverging_tables(_row_snapshot(result.outputs, fqns), _row_snapshot(again.outputs, fqns), fqns)
    _stamp_idempotency(report, not diverging, diverging)
    return _finish(report, started)


def _load_build_module(job: _Job):
    """Imports the build as a throwaway module -- only after it passed the Snowpark rules (`_prepare`), compiled
    from the very text they read, never re-read from disk, so a file swapped after the check cannot run; and
    `compile()` + `exec` writes no `__pycache__/` beside it."""
    module = types.ModuleType(f"build_{abs(hash(str(job.path)))}")
    module.__file__ = str(job.path)
    exec(compile(job.text, str(job.path), "exec"), module.__dict__)  # noqa: S102 -- the rules gate ran first
    if not callable(getattr(module, "main", None)):
        raise ValueError(f"{job.path.name} has no main()")
    return module


def _snowflake_session(job: _Job, golden_set: str, *, reverse: bool = False):
    """One run's session on a real account: built from the named connection, the sandbox's schemas replaced -- empty
    -- in its database (what `SnowflakeSandbox.fresh` does through the connector), the golden set loaded there. A
    missing or malformed prerequisite stays a usage error; anything the session raises is re-raised with its text
    redacted."""
    from lib import snowflake_conn, snowflake_sandbox  # noqa: PLC0415  (lazy: never on the local path)
    schemas = [snowflake_sandbox.ident(schema)
               for schema in (*snowflake_sandbox.SANDBOX_SCHEMAS, load_golden.golden_view_schema(job.wf_id, golden_set))]
    database = job.sandbox.database
    session = snowflake_conn.snowpark_session(job.sandbox.connection_name)
    try:
        session.sql(f"USE DATABASE {database}").collect()
        for schema in schemas:
            session.sql(f"CREATE OR REPLACE SCHEMA {database}.{schema}").collect()
        for fqn, table in load_golden.session_tables(job.repo, job.wf_id, golden_set, database=database,
                                                      reverse=reverse).items():
            snowpark_tables.load_into_snowpark(session, fqn, table)
    except (FileNotFoundError, ValueError):
        session.close()
        raise
    except Exception as exc:  # noqa: BLE001 -- redacted, never chained
        session.close()
        raise snowflake_conn.scrubbed(exc) from None
    return session


def _redacted(exc: Exception, job: _Job) -> Exception:
    """What a report may carry of `exc`: itself locally, its redacted text on a real account."""
    if job.sandbox is None:
        return exc
    from lib import snowflake_conn  # noqa: PLC0415
    return snowflake_conn.scrubbed(exc)


def _run_in_session(job: _Job, golden_set: str, *, reverse: bool = False) -> tuple[dict[str, dict | None], Exception | None]:
    """One run on a real account: a fresh session with the golden set, `main` called, every output read back.
    `({fqn: table | None}, None)`, or `({}, the failure)` when the build raised or an output could not be read
    back (a cell its own column type cannot represent). The session is always closed."""
    session = _snowflake_session(job, golden_set, reverse=reverse)       # loading the golden set: a usage error propagates
    try:
        src, tgt = load_golden.build_schemas(job.wf_id, golden_set, database=job.database)
        try:
            _load_build_module(job).main(session, src, tgt)
        except Exception as exc:  # noqa: BLE001 -- any exception from the build's main() is a domain FAIL
            return {}, _redacted(RuntimeError(f"the build raised: {type(exc).__name__}: {exc}"), job)
        actuals: dict[str, dict | None] = {}
        for fqn in job.tables:
            try:
                actuals[fqn] = snowpark_tables.table_from_snowpark(session, fqn)
            except snowpark_tables.ReadBackError as exc:
                return {}, exc
        return actuals, None
    finally:
        session.close()


def _run_set_python_snowflake(job: _Job, golden_set: str) -> dict:
    """`--backend snowflake` (a human, a real account): the build runs in this process against a real Snowflake
    session, which the child-process sandbox cannot host -- it needs the network the sandbox forbids, and it runs
    in Snowflake, not on this host."""
    started = time.perf_counter()
    fqns = job.tables
    actuals, failure = _run_in_session(job, golden_set)
    if failure is not None:
        return _finish(v.build_fail_report(golden_set, failure, language=PYTHON), started)
    report = _judge_tables(job, golden_set, actuals)
    # The first run's outputs were read back before the second run's fresh sandbox replaces them.
    again, failure = _run_in_session(job, golden_set, reverse=True)
    diverging = list(fqns) if failure is not None else v.diverging_tables(
        _row_snapshot(actuals, fqns), _row_snapshot(again, fqns), fqns)
    _stamp_idempotency(report, not diverging, diverging)
    return _finish(report, started)


# --- the whole workflow ------------------------------------------------------------------------------------------


def _run_set(job: _Job, golden_set: str) -> dict:
    if job.language == SQL:
        return _run_set_sql(job, golden_set)
    if job.sandbox is None:
        return _run_set_python_local(job, golden_set)
    return _run_set_python_snowflake(job, golden_set)


def validate_build(repo: Repo, wf_id: str, golden_sets: Sequence[str] | None = None, *, backend: str = "duckdb",
                   connection: str | None = None, sandbox_database: str | None = None,
                   sandbox_timeout: float | None = None) -> dict:
    """Validates `workflows/<wf_id>/build.sql` (or `build.py`) against every named golden set (default:
    `manifest.golden_sets`), writes `validation.<set>.json` for each and `validation.json` for the workflow as a
    whole, and returns that top-level report.

    Before anything else -- before even the prerequisite checks -- this deletes the workflow's `validation.json`
    and `validation.<set>.json` files already on disk (no other file), so a report on disk always belongs to the
    most recent invocation: a missing
    prerequisite raises `FileNotFoundError`/`ValueError` (a usage error for the CLI) and leaves *no* report behind,
    not a stale one from an earlier run. Every prerequisite is checked, and every golden set fully processed in
    memory, before anything is written.

    `backend="snowflake"` runs each run in a fresh sandbox on a real account -- `connection` names the
    `connections.toml` entry, `sandbox_database` a database the policy lists -- with the same judging and the same
    report; both are checked before anything connects.
    """
    v.clear_stale_build_reports(repo, wf_id)
    job = _prepare(repo, wf_id, golden_sets, backend, connection, sandbox_database, sandbox_timeout)

    reports: dict[str, dict] = {}
    if job.mismatch is not None or job.refused is not None:
        # The build is wrong before any of it runs: a FAIL on every set naming why, never idempotent.
        error = ValueError(job.mismatch) if job.mismatch is not None else job.refused
        reports = {golden_set: v.build_fail_report(golden_set, error, language=job.language) for golden_set in job.sets}
        return v.write_build_reports(repo, wf_id, job.sets, reports, language=job.language)

    for golden_set in job.sets:
        reports[golden_set] = _run_set(job, golden_set)

    # Every set was re-run on its own; `write_build_reports` makes the top-level `idempotent` their AND.
    return v.write_build_reports(repo, wf_id, job.sets, reports, language=job.language)


# --- CLI ----------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id", help="workflow id, e.g. wf_0003")
    parser.add_argument("--set", dest="sets", action="append", default=None,
                        help="golden set to validate (repeatable); default: manifest.golden_sets")
    parser.add_argument("--sandbox-timeout", type=float, default=None,
                        help="a build.py: seconds one sandboxed run may take before it is killed "
                             "(default: MIG_SNOWPARK_SANDBOX_TIMEOUT or 600)")
    v.add_backend_args(parser)
    add_root_arg(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Not parser.error(): main() is called in-process by callers that expect an int back.
    try:
        report = validate_build(Repo(args.root), args.wf_id, args.sets, backend=args.backend,
                                connection=args.connection, sandbox_database=args.sandbox_database,
                                sandbox_timeout=args.sandbox_timeout)
    except (FileNotFoundError, ValueError) as exc:
        # exit 2: a missing prerequisite; nothing was written
        print(f"cannot validate {args.wf_id}: {v.error_text(args.backend, str(exc))}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 -- exit 2 is "anything unexpected"; never a bare traceback exit 1
        v.print_crash(args.backend)
        return 2

    print(f"{args.wf_id}: {report['verdict']} {report['sets']} (idempotent={report['idempotent']})")
    if report.get("error") and not report["verdict"].startswith("PASS"):
        print(f"  {report['golden_set']}: {str(report['error']).splitlines()[0]}", file=sys.stderr)
    return 0 if report["verdict"].startswith("PASS") else 1


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
