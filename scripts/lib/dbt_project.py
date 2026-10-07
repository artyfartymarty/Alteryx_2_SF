"""The ONE way this repository runs dbt (objective-first design §4.10).

A SQL workflow is delivered as a small dbt project, converted from its validated `build.sql` by
`scripts/export_dbt.py` and checked against the golden data by `scripts/validate_dbt.py`. This module is what
`validate_dbt.py` runs dbt through: a subprocess of the console script installed beside the running
interpreter (`sysconfig.get_path("scripts")`), with anonymous usage statistics and colours off and its
`target/` and `logs/` directories in a temporary directory, so a run never writes into the project and one
invocation never inherits another's process state.

The project's own files are the exporter's, not this module's: the exporter writes every one of them, so
there is nothing for a runner to refuse. (The closed "dbt surface" the agent-written projects of the earlier
pipeline were held to is gone with them.)

Facts this module encodes (spike 2026-09-22; dbt-core 1.12.5, dbt-duckdb 1.11.0, duckdb 1.5.5):
* dbt-duckdb names the attached catalog after the whole file stem while DuckDB drops a leading
  dot, so `.sandbox.<set>.duckdb` fails: sandboxes are `dbt_sandbox_<set>.duckdb`.
* `lib.backend.DuckDBBackend` stores `MIGDB.<SCHEMA>.<T>` as schema `MIGDB__<SCHEMA>`; a local
  run's `--vars` carry those flattened names (`local_vars`).
* dbt will not adopt a pre-existing upper-case target table for a lower-case model name
  ("approximate match"), so every output model carries `alias='<LOGICAL>'`.
Nothing here has run against Snowflake; dbt-snowflake is not installed in this environment.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sysconfig
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .backend import SANDBOX_DB, local_name
from .paths import Repo

DUCKDB_PATH_ENV = "MIG_DBT_DUCKDB_PATH"
WORK_SCHEMA = "MIG_WORK"
DBT_TIMEOUT_S = 600
FAILED_STATUSES = frozenset({"error", "fail", "runtime error"})
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class DbtUnavailable(RuntimeError):
    """No dbt console script beside this interpreter: a usage problem, never a domain verdict."""


@dataclass(frozen=True)
class DbtResult:
    code: int
    output: str                 # stdout+stderr: ANSI stripped, LF endings, temp/project paths redacted
    results: list[dict]         # run_results.json: {"name", "unique_id", "status", "message"} per node
    manifest: dict | None       # target/manifest.json when dbt wrote one (parse, run, build)

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def failed_models(self) -> list[str]:
        """The models dbt says failed to run (a failed test is `failed_tests`)."""
        return sorted(r["name"] for r in self.results if r["status"] in FAILED_STATUSES and not _is_test(r))

    @property
    def failed_tests(self) -> list[str]:
        return sorted(r["name"] for r in self.results if r["status"] in FAILED_STATUSES and _is_test(r))

    @property
    def warned_tests(self) -> list[dict]:
        """The tests that ran with `severity: warn` and found rows -- `{"name", "message"}` each, in dbt's order.
        A warning is not a failure: dbt exits 0 for it, and `FAILED_STATUSES` leaves it out."""
        return [{"name": r["name"], "message": r["message"]} for r in self.results if r["status"] == "warn" and _is_test(r)]

    @property
    def skipped_models(self) -> list[str]:
        return sorted(r["name"] for r in self.results if r["status"] == "skipped" and not _is_test(r))


def _is_test(result: dict) -> bool:
    return str(result.get("unique_id", "")).startswith("test.")


def _name_of(unique_id: str) -> str:
    """`model.<project>.<name>` -> name; `test.<project>.<name>.<hash>` -> name."""
    parts = str(unique_id).split(".")
    return parts[-2] if parts[0] == "test" and len(parts) > 2 else parts[-1]


def project_dir(repo: Repo, wf_id: str) -> Path:
    """`workflows/<wf_id>/dbt`, where `export_dbt.py` writes the project."""
    return repo.wf(wf_id, "dbt")


def project_name(wf_id: str) -> str:
    """The dbt project's name, and the name of the profile it looks for: `mig_wf_0001` for `wf_0001`."""
    return "mig_" + re.sub(r"[^a-z0-9_]", "_", wf_id.lower())


def local_vars(src_schema: str, tgt_schema: str = WORK_SCHEMA) -> dict[str, str]:
    """The `--vars` of a local run: the schema names `DuckDBBackend` stores `MIGDB.<SCHEMA>` under."""
    return {"src_schema": local_name(f"{SANDBOX_DB}.{src_schema}.X")[0],
            "tgt_schema": local_name(f"{SANDBOX_DB}.{tgt_schema}.X")[0]}


def sandbox_path(repo: Repo, wf_id: str, golden_set: str, suffix: str = "") -> Path:
    return repo.wf(wf_id, f"dbt_sandbox_{golden_set}{suffix}.duckdb")


def dbt_executable() -> Path:
    scripts = Path(sysconfig.get_path("scripts"))
    for name in ("dbt.exe", "dbt"):
        if (scripts / name).is_file():
            return scripts / name
    raise DbtUnavailable(f"no dbt console script in {scripts.name}/ beside this interpreter; "
                         f"install requirements.txt into it")


def tail(text: str, lines: int = 5) -> str:
    return " | ".join(line.strip() for line in text.strip().splitlines()[-lines:])


def bounded(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean(text: str, replacements: dict[str, str]) -> str:
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    for raw, placeholder in replacements.items():
        for form in {raw, raw.replace("\\", "/"), raw.replace("/", "\\")}:
            text = text.replace(form, placeholder)
    return text


def run_dbt(command: str, project: Path, *, vars: dict[str, str], duckdb_path: Path | None,
            target: str = "local", log_file: Path | None = None,
            extra_env: dict[str, str] | None = None, timeout: int = DBT_TIMEOUT_S,
            flags: Sequence[str] = ()) -> DbtResult:
    """The ONE way this repository runs dbt: `dbt <command>` on `project` (its own `profiles.yml`), the
    `--vars` as given, the DuckDB sandbox file named through `MIG_DBT_DUCKDB_PATH`. `log_file`, when given, gets
    the cleaned output (ANSI stripped, the temp and project paths redacted). `flags` are further command-line
    flags, each one argument (`["--full-refresh"]`)."""
    project = Path(project).resolve()
    executable = dbt_executable()
    with tempfile.TemporaryDirectory(prefix="dbt-") as tmp:
        target_path, log_path = Path(tmp, "target"), Path(tmp, "logs")
        args = [str(executable), command, "--project-dir", str(project), "--profiles-dir", str(project),
                "--target", target, "--target-path", str(target_path), "--log-path", str(log_path),
                "--vars", json.dumps(vars, sort_keys=True), "--no-use-colors", *flags]
        env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false", "DO_NOT_TRACK": "1",
               **(extra_env or {})}
        if duckdb_path is not None:
            env[DUCKDB_PATH_ENV] = str(Path(duckdb_path).resolve())
        completed = subprocess.run(args, cwd=project, env=env, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout)
        redact = {tmp: "<dbt-temp>", str(project): "<project>"}
        output = _clean((completed.stdout or "") + (completed.stderr or ""), redact)
        results = []
        run_results = target_path / "run_results.json"
        if run_results.is_file():
            for r in json.loads(run_results.read_text(encoding="utf-8")).get("results") or []:
                results.append({"name": _name_of(r["unique_id"]), "unique_id": r["unique_id"],
                                "status": str(r.get("status")), "message": _clean(r.get("message") or "", redact)})
        manifest_path = target_path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(output, encoding="utf-8", newline="\n")
    return DbtResult(completed.returncode, output, results, manifest)
