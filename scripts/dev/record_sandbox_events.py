"""Re-record the Snowpark sandbox's benign audit-event allow-list (live hardening L4, fix round 5).

    python scripts/dev/record_sandbox_events.py            # rewrite scripts/lib/sandbox_events.json
    python scripts/dev/record_sandbox_events.py --check     # exit 1 if the committed list is stale

The runtime audit hook (`lib.snowpark_sandbox._hook`) is DEFAULT-DENY: it refuses every event that is
not on `scripts/lib/sandbox_events.json`. That file is not guessed -- it is the union of every audit
event that fires, AFTER the hook is armed, while every benign Snowpark build runs in the sandbox:
the committed canned build (`samples/wf_0006/canned/build.py`, the carry-over body, fed that sample's
own `normal` golden input) and every cookbook Snowpark example fragment (`tests/cookbook_examples/snowpark/**`).
This script runs them all in the sandbox's recording mode (`run_module(..., record_events_to=...)`, which
the parent writes into the child's spec so `_arm` installs a logging hook instead of the enforcing one --
no environment variable can select it) and collects the names.

`tests/test_snowpark_sandbox.py` calls `record_events()` and fails if it produces an event the
committed file does not list -- so the list is maintained, not stale. Nothing here runs on Snowflake:
the double is the Snowpark Local Testing Framework, in a child process, exactly as validation uses it.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_ROOT = _SCRIPTS.parent
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_EVENTS_FILE = _SCRIPTS / "lib" / "sandbox_events.json"
_COOKBOOK = _ROOT / "tests" / "cookbook_examples" / "snowpark"
_CANNED = _ROOT / "samples" / "wf_0006" / "canned" / "build.py"
_CANNED_INPUT = _ROOT / "samples" / "wf_0006" / "golden_inputs" / "normal" / "1.csv"
_OWN = "TGT.T.COOKBOOK_OUT"
_ARGS = {"src": "SRC.S", "tgt": "TGT.T"}


def _run(record_path: Path, source: str, inputs: dict, outputs: list[str]) -> None:
    """Run one benign source in the sandbox with recording armed; its captured events land in
    `record_path` (passed in the child's spec file -- never an environment variable)."""
    from lib import snowpark_sandbox  # noqa: PLC0415
    result = snowpark_sandbox.run_module(source=source, display_path="build.py", run_id="record",
                                         outputs=outputs, inputs=inputs,
                                         record_events_to=str(record_path))
    if result.kind not in ("ok", "run"):   # a benign body should finish; "run" is a raise we tolerate
        raise RuntimeError(f"recording run was not benign: {result.kind}: {result.error}")


def record_events() -> set[str]:
    """The union of audit events every benign Snowpark build fires in the sandbox, after arming."""
    from lib import typed_csv  # noqa: PLC0415
    events: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="record-events-") as tmp:
        record_path = Path(tmp) / "events.json"

        def collect(source: str, inputs: dict, outputs: list[str]) -> None:
            if record_path.exists():
                record_path.unlink()
            _run(record_path, source, inputs, outputs)
            if record_path.exists():
                events.update(json.loads(record_path.read_text(encoding="utf-8")))

        # The committed canned build, fed the sample's own `normal` golden input as its one source --
        # exercised in tables mode so no golden pipeline is needed.
        collect(_CANNED.read_text(encoding="utf-8"),
                {"mode": "tables", "tables": [{"fqn": "SRC.S.SUBSCRIPTIONS", "table": typed_csv.read_table(_CANNED_INPUT)}],
                 "args": _ARGS},
                ["TGT.T.REVENUE_BY_PERIOD"])

        # Every cookbook Snowpark example fragment, wrapped into a full main() and driven in tables mode.
        for directory in sorted(p for p in _COOKBOOK.iterdir() if p.is_dir()):
            case = json.loads((directory / "case.json").read_text(encoding="utf-8"))
            example = (directory / "example.py").read_text(encoding="utf-8")
            tables = [{"fqn": f"MIG_COOKBOOK.IN_{tid}", "table": typed_csv.read_table(directory / name)}
                      for tid, name in case["inputs"].items()]
            for entry in case["compare"]:
                fn = entry["function"]
                wrapped = (example + "\n\n"
                           "def main(session, src, tgt):\n"
                           f'    {fn}(session).write.mode("overwrite").save_as_table("{_OWN}")\n'
                           '    return "OK"\n')
                collect(wrapped, {"mode": "tables", "tables": tables, "args": _ARGS}, [_OWN])
    return events


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Re-record the Snowpark sandbox benign audit-event list.")
    parser.add_argument("--check", action="store_true",
                        help="do not write; exit 1 if the committed list is missing a recorded event")
    args = parser.parse_args(argv)
    recorded = sorted(record_events())
    committed = sorted(json.loads(_EVENTS_FILE.read_text(encoding="utf-8"))) if _EVENTS_FILE.is_file() else []
    if args.check:
        missing = sorted(set(recorded) - set(committed))
        if missing:
            print(f"sandbox_events.json is stale: benign runs fired unlisted events {missing}", file=sys.stderr)
            return 1
        print(f"OK: {len(recorded)} recorded events, all on the committed allow-list")
        return 0
    _EVENTS_FILE.write_text(json.dumps(recorded, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {_EVENTS_FILE.relative_to(_ROOT)} with {len(recorded)} events: {recorded}")
    return 0


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
