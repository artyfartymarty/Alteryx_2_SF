"""Tests for `inject_outputs.py`: instrumenting a workflow's XML for golden capture, and importing
captured `.yxdb` files into typed CSV (plan task 11).

The first three tests are the brief's Step 1: the capture map shape for `wf_0003`, byte-identical
original XML, a clean re-parse, plus `import_captures` round-tripping a source table through
`write_yxdb`. The rest cover behaviour the brief describes in prose but does not test directly:
anchor names mapped back to their XML spelling (dag-contract §2), new ToolIDs starting at
max(existing) + 1001, the missing-capture-file error, and the CLI's two modes.

Objective-first (design D5): the golden data is the inputs and the final outputs only, so a capture
point is an input's out stream or the stream feeding a final Output tool; nothing between two tools
is captured, no segmentation is needed to instrument, and `golden/intermediates/` is never written.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

import inject_outputs
import parse
from lib import yxdb
from lib.io import load_manifest, read_json, write_json
from lib.paths import Repo

SAMPLES = Path(__file__).parents[1] / "samples"
WF3_SOURCE = SAMPLES / "wf_0003" / "source" / "gl_period_close.yxmd"


def _wf3():
    """The real sample's XML text and dag."""
    xml_text = parse.decode_xml(WF3_SOURCE.read_bytes())
    dag, _ = parse.parse_file(WF3_SOURCE)
    return xml_text, dag


def _strip_injected(xml_text: str, capture_map: list[dict]) -> str:
    """Removes exactly the `<Node>`/`<Connection>` blocks `inject` added, by the new tool ids."""
    new_ids = {row["tool_id"] for row in capture_map}
    text = xml_text
    for tid in new_ids:
        text = re.sub(rf'<Node ToolID="{tid}">.*?</Node>', "", text, flags=re.S)
    text = re.sub(
        r"<Connection>.*?</Connection>",
        lambda m: "" if any(f'Destination ToolID="{tid}"' in m.group(0) for tid in new_ids) else m.group(0),
        text, flags=re.S)
    return text


CAPTURE_DIR = r"C:\mig\capture\wf_0003"


# --- brief Step 1, verbatim ---

def test_capture_map_has_one_input_and_one_output_and_nothing_between_them():
    xml_text, dag = _wf3()
    _, capture_map = inject_outputs.inject(xml_text, dag, CAPTURE_DIR)

    assert len(capture_map) == 2
    by_kind = {row["kind"]: row for row in capture_map}
    assert set(by_kind) == {"input", "output"}

    assert by_kind["input"]["of_tool"] == "1"
    assert by_kind["input"]["stream"] == "1_Output"
    assert by_kind["input"]["file"] == str(CAPTURE_DIR) + "\\in_1.yxdb"

    assert by_kind["output"]["of_tool"] == "10"
    assert by_kind["output"]["stream"] == "9_Output"
    assert by_kind["output"]["file"] == str(CAPTURE_DIR) + "\\out_10.yxdb"

    # a row carries what the importer reads and nothing from the retired segmentation
    for row in capture_map:
        assert set(row) == {"tool_id", "kind", "of_tool", "stream", "file"}


def test_original_nodes_xml_is_byte_identical():
    xml_text, dag = _wf3()
    new_xml, capture_map = inject_outputs.inject(xml_text, dag, CAPTURE_DIR)

    assert new_xml != xml_text  # something was actually inserted
    assert _strip_injected(new_xml, capture_map) == xml_text


def test_instrumented_xml_reparses_cleanly_with_no_invariant_violations(tmp_path):
    xml_text, dag = _wf3()
    new_xml, _ = inject_outputs.inject(xml_text, dag, CAPTURE_DIR)

    out_path = tmp_path / "gl_period_close.instrumented.yxmd"
    out_path.write_text(new_xml, encoding="utf-8", newline="")

    new_dag, _ = parse.parse_file(out_path)
    import invariants
    assert invariants.check(new_xml, new_dag) == []
    # the 2 new Output Data tools plus the original 12
    assert len(new_dag["nodes"]) == len(dag["nodes"]) + 2


def test_import_captures_round_trips_write_yxdb_source_rows(tmp_path):
    repo = Repo(tmp_path)
    xml_text, dag = _wf3()
    _, capture_map = inject_outputs.inject(xml_text, dag, CAPTURE_DIR)
    write_json(repo.wf("wf_0003", "golden", "capture_map.json"), capture_map)

    capture_dir = tmp_path / "captures"
    fields = [
        {"name": "ACCT", "type": "V_String", "size": 20, "scale": None},
        {"name": "PERIOD", "type": "V_String", "size": 7, "scale": None},
        {"name": "POSTED", "type": "V_String", "size": 10, "scale": None},
        {"name": "AMOUNT", "type": "FixedDecimal", "size": 19, "scale": 2},
        {"name": "REGION", "type": "V_String", "size": 10, "scale": None},
        {"name": "ENTRY_ID", "type": "Int64", "size": 8, "scale": None},
    ]
    rows = [
        ["4000", "2026-08", "03/08/2026", Decimal("100.00"), "EMEA", 1],
        ["5000", "2026-08", "31/08/2026", Decimal("75.25"), "EMEA", 5],
    ]
    by_kind = {row["kind"]: row for row in capture_map}
    yxdb.write_yxdb(capture_dir / "in_1.yxdb", fields, rows)
    # the other capture point still needs a file on disk, or import_captures must fail loudly
    yxdb.write_yxdb(capture_dir / "out_10.yxdb", fields, [])

    written = inject_outputs.import_captures(repo, "wf_0003", "normal", capture_dir)

    input_csv = repo.wf("wf_0003", "golden", "inputs", "normal", "1.csv")
    assert input_csv in written
    from lib import typed_csv
    table = typed_csv.read_table(input_csv)
    assert table["fields"] == fields
    assert table["rows"] == rows
    assert input_csv.with_suffix(".schema.json").exists()

    assert repo.wf("wf_0003", "golden", "outputs", "normal", "10.csv").exists()
    assert not repo.wf("wf_0003", "golden", "intermediates").exists()
    assert by_kind["input"]["of_tool"] == "1"  # sanity: file naming lines up with the row we relied on


# --- additional coverage: prose-described behaviour the brief's own tests don't exercise ---

def test_new_tool_ids_start_after_existing_max_plus_1001():
    xml_text, dag = _wf3()
    _, capture_map = inject_outputs.inject(xml_text, dag, CAPTURE_DIR)

    max_existing = max(int(n["tool_id"]) for n in dag["nodes"])  # 200 (the "Publish" container)
    assert max_existing == 200
    new_ids = sorted(int(row["tool_id"]) for row in capture_map)
    assert new_ids == [1201, 1202]


def test_anchor_names_are_mapped_back_to_their_xml_spelling():
    """dag-contract §2: T/F, L/J/R and U/D are canonical short names; the XML `Connection`
    attribute on a new capture wire must use the tool's real anchor name (True/False, Left/Join/
    Right, Unique/Duplicates), not the canonical one. A final Output tool taps whichever anchor feeds it."""
    dag = {"nodes": [
        {"tool_id": "2", "type": "filter", "out_anchors": ["T", "F"]},
        {"tool_id": "3", "type": "join", "out_anchors": ["L", "J", "R"]},
        {"tool_id": "4", "type": "unique", "out_anchors": ["U", "D"]},
        *({"tool_id": str(90 + i), "type": "output", "out_anchors": []} for i in range(7)),
    ], "edges": [
        {"src": src, "src_anchor": anchor, "dst": str(90 + i), "dst_anchor": "Input"}
        for i, (src, anchor) in enumerate([("2", "T"), ("2", "F"), ("3", "L"), ("3", "J"), ("3", "R"),
                                           ("4", "U"), ("4", "D")])]}
    xml_text = "<AlteryxDocument><Nodes></Nodes><Connections></Connections></AlteryxDocument>"

    new_xml, capture_map = inject_outputs.inject(xml_text, dag, r"C:\cap")

    expected = {"2_T": "True", "2_F": "False", "3_L": "Left", "3_J": "Join", "3_R": "Right",
                "4_U": "Unique", "4_D": "Duplicates"}
    assert {row["stream"] for row in capture_map} == set(expected)
    for row in capture_map:
        src = row["stream"].rsplit("_", 1)[0]
        pattern = rf'<Origin ToolID="{src}" Connection="([^"]+)"/><Destination ToolID="{row["tool_id"]}"'
        match = re.search(pattern, new_xml)
        assert match, f"no connection found for {row}"
        assert match.group(1) == expected[row["stream"]]


def test_import_captures_reports_the_missing_file_by_name(tmp_path):
    repo = Repo(tmp_path)
    write_json(repo.wf("wf_0003", "golden", "capture_map.json"), [
        {"tool_id": "1201", "kind": "input", "of_tool": "1", "stream": "1_Output",
         "file": r"C:\mig\capture\wf_0003\in_1.yxdb"},
    ])
    with pytest.raises(FileNotFoundError, match="in_1.yxdb"):
        inject_outputs.import_captures(repo, "wf_0003", "normal", tmp_path / "empty_captures")


# --- Fix round 3 (task 9 re-review): a capture_map.json row cannot path-traverse ---------------
#
# `_golden_csv_path` passes a captured row's own "of_tool" straight into `repo.wf(...)`'s `*parts`.
# `lib.paths.Repo.wf` refuses a part containing a `..` component (fix round 3, rule A) -- this proves
# that refusal reaches all the way through `import_captures`, rather than writing a golden CSV through a
# traversed path. The CLI-level companion test is with the other `--import-set` CLI tests below.

def test_import_captures_rejects_a_traversal_of_tool(tmp_path):
    repo = Repo(tmp_path)
    write_json(repo.wf("wf_0003", "golden", "capture_map.json"), [
        {"tool_id": "1201", "kind": "output", "of_tool": "../..", "stream": "9_Output",
         "file": r"C:\mig\capture\wf_0003\out_10.yxdb"},
    ])
    capture_dir = tmp_path / "captures"
    fields = [{"name": "ACCT", "type": "V_String", "size": 20, "scale": None}]
    yxdb.write_yxdb(capture_dir / "out_10.yxdb", fields, [["4000"]])

    with pytest.raises(ValueError):
        inject_outputs.import_captures(repo, "wf_0003", "normal", capture_dir)

    # Pass 1 (resolve + read) runs entirely before Pass 2 (write), so a row that fails to resolve
    # a path leaves nothing written at all -- let alone outside golden/.
    assert not list(repo.wf("wf_0003", "golden").rglob("*.csv"))


def test_import_captures_names_an_old_intermediate_row_and_how_to_regenerate_the_map(tmp_path):
    """A `capture_map.json` written before the objective-first rewrite has `intermediate` rows. Nothing reads
    those captures any more, so the import says so by name rather than writing a `golden/intermediates/`."""
    repo = Repo(tmp_path)
    write_json(repo.wf("wf_0003", "golden", "capture_map.json"), [
        {"tool_id": "1201", "kind": "intermediate", "of_tool": "3", "stream": "3_Output", "segment": "seg_01",
         "file": r"C:\mig\capture\wf_0003\mid_seg_01_3_Output.yxdb"},
    ])
    capture_dir = tmp_path / "captures"
    fields = [{"name": "ACCT", "type": "V_String", "size": 20, "scale": None}]
    yxdb.write_yxdb(capture_dir / "mid_seg_01_3_Output.yxdb", fields, [["4000"]])

    with pytest.raises(ValueError, match=r"'intermediate'.*inject_outputs\.py wf_0003 --capture-dir"):
        inject_outputs.import_captures(repo, "wf_0003", "normal", capture_dir)
    assert not list(repo.wf("wf_0003", "golden").rglob("*.csv"))


# --- CLI: both modes, through parse.run so the artifacts are the real thing ---

def _prepared_repo(tmp_path) -> Repo:
    repo = Repo(tmp_path)
    import shutil
    source_dir = repo.wf("wf_0003", "source")
    source_dir.mkdir(parents=True)
    shutil.copy(WF3_SOURCE, source_dir / WF3_SOURCE.name)
    parse.run(repo, "wf_0003", check=True)
    return repo


def test_cli_instruments_and_prints_the_alteryx_command(tmp_path, capsys):
    repo = _prepared_repo(tmp_path)

    rc = inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    instrumented = repo.wf("wf_0003", "source", "gl_period_close.instrumented.yxmd")
    assert instrumented.exists()
    assert f'"{inject_outputs.ALTERYX_ENGINE_CMD}"' in out
    assert f'"{instrumented.resolve()}"' in out

    capture_map = read_json(repo.wf("wf_0003", "golden", "capture_map.json"))
    assert len(capture_map) == 2


def test_cli_import_set_writes_golden_csvs(tmp_path, capsys):
    repo = _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])
    capsys.readouterr()

    capture_dir = tmp_path / "captures"
    fields = [{"name": "ACCT", "type": "V_String", "size": 20, "scale": None}]
    for filename in ("in_1.yxdb", "out_10.yxdb"):
        yxdb.write_yxdb(capture_dir / filename, fields, [["4000"]])

    rc = inject_outputs.main(["wf_0003", "--capture-dir", str(capture_dir),
                              "--import-set", "normal", "--root", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "1.csv" in out
    assert repo.wf("wf_0003", "golden", "inputs", "normal", "1.csv").exists()
    assert repo.wf("wf_0003", "golden", "outputs", "normal", "10.csv").exists()
    assert not repo.wf("wf_0003", "golden", "intermediates").exists()


def test_cli_rerun_targets_the_original_file_not_its_own_instrumented_output(tmp_path, capsys):
    """`*.instrumented.yxmd` sorts alphabetically before the plain name, so a naive "first .yxmd
    in source/" pick would re-instrument the script's own prior output on a second run."""
    repo = _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])
    capsys.readouterr()
    first_capture_map = read_json(repo.wf("wf_0003", "golden", "capture_map.json"))

    rc = inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])

    assert rc == 0
    second_capture_map = read_json(repo.wf("wf_0003", "golden", "capture_map.json"))
    assert second_capture_map == first_capture_map  # same original source -> same capture map
    instrumented_xml = repo.wf("wf_0003", "source", "gl_period_close.instrumented.yxmd").read_text(encoding="utf-8")
    assert instrumented_xml.count("golden capture:") == 2  # not doubled by capturing its own captures


def test_cli_import_set_exits_1_and_names_the_file_when_a_capture_is_missing(tmp_path, capsys):
    repo = _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])
    capsys.readouterr()

    empty_capture_dir = tmp_path / "empty_but_present"
    empty_capture_dir.mkdir()  # exists (so this is a domain failure, not --capture-dir usage error)
    rc = inject_outputs.main(["wf_0003", "--capture-dir", str(empty_capture_dir),
                              "--import-set", "normal", "--root", str(tmp_path)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "in_1.yxdb" in err


def test_cli_import_set_with_a_traversal_of_tool_exits_1_and_writes_nothing(tmp_path, capsys):
    """Fix round 3 (task 9 re-review): a hand-edited (or otherwise malformed) capture_map.json
    row whose "of_tool" is a traversal must fail the same clean, one-line, exit-1 way a missing or
    corrupt capture file already does -- not crash with a traceback (exit 2) or write a golden
    CSV through the traversed path."""
    repo = _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])
    capsys.readouterr()
    capture_map = read_json(repo.wf("wf_0003", "golden", "capture_map.json"))
    for row in capture_map:
        if row["kind"] == "output":
            row["of_tool"] = "../.."
    write_json(repo.wf("wf_0003", "golden", "capture_map.json"), capture_map)

    capture_dir = tmp_path / "captures"
    fields = [{"name": "ACCT", "type": "V_String", "size": 20, "scale": None}]
    for filename in ("in_1.yxdb", "out_10.yxdb"):
        yxdb.write_yxdb(capture_dir / filename, fields, [["4000"]])

    rc = inject_outputs.main(["wf_0003", "--capture-dir", str(capture_dir),
                              "--import-set", "normal", "--root", str(tmp_path)])

    assert rc == 1  # a domain failure this script checks for, not an unhandled crash
    assert "Traceback" not in capsys.readouterr().err
    assert not list(repo.wf("wf_0003", "golden").rglob("*.csv"))


# --- Fix round 1: CLI exit codes (implementer-rules.md "CLI EXIT CODES") ---

def _wf3_source_only(tmp_path) -> Repo:
    """A repo with the workflow copied into source/ but `parse.py` never run."""
    import shutil
    repo = Repo(tmp_path)
    source_dir = repo.wf("wf_0003", "source")
    source_dir.mkdir(parents=True)
    shutil.copy(WF3_SOURCE, source_dir / WF3_SOURCE.name)
    return repo


def test_main_on_never_parsed_workflow_exits_2_and_writes_nothing(tmp_path):
    repo = _wf3_source_only(tmp_path)  # source/ exists; parsed/dag.json does not

    with pytest.raises(SystemExit) as exc:
        inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])

    assert exc.value.code == 2
    assert not repo.wf("wf_0003", "source", "gl_period_close.instrumented.yxmd").exists()
    assert not repo.wf("wf_0003", "golden", "capture_map.json").exists()


def test_main_on_a_parsed_workflow_needs_no_segmentation(tmp_path):
    """Instrumenting reads the source and `parsed/dag.json` only; `segments/` is not a prerequisite."""
    repo = _wf3_source_only(tmp_path)
    parse.run(repo, "wf_0003", check=True)
    assert not repo.wf("wf_0003", "segments").exists()

    assert inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)]) == 0

    assert repo.wf("wf_0003", "source", "gl_period_close.instrumented.yxmd").exists()
    assert len(read_json(repo.wf("wf_0003", "golden", "capture_map.json"))) == 2


def test_main_with_no_arguments_exits_2():
    with pytest.raises(SystemExit) as exc:
        inject_outputs.main([])
    assert exc.value.code == 2


def test_main_import_set_with_no_capture_map_exits_2(tmp_path):
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()

    with pytest.raises(SystemExit) as exc:
        inject_outputs.main(["wf_0003", "--capture-dir", str(capture_dir),
                             "--import-set", "normal", "--root", str(tmp_path)])
    assert exc.value.code == 2


def test_main_import_set_with_capture_dir_not_a_directory_exits_2(tmp_path):
    _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])

    with pytest.raises(SystemExit) as exc:
        inject_outputs.main(["wf_0003", "--capture-dir", str(tmp_path / "does_not_exist"),
                             "--import-set", "normal", "--root", str(tmp_path)])
    assert exc.value.code == 2


def test_main_import_set_with_missing_listed_capture_exits_1_and_writes_nothing(tmp_path, capsys):
    repo = _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])
    capsys.readouterr()

    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    fields = [{"name": "ACCT", "type": "V_String", "size": 20, "scale": None}]
    yxdb.write_yxdb(capture_dir / "in_1.yxdb", fields, [["4000"]])
    # out_10.yxdb intentionally left missing

    rc = inject_outputs.main(["wf_0003", "--capture-dir", str(capture_dir),
                              "--import-set", "normal", "--root", str(tmp_path)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "out_10.yxdb" in err
    assert not repo.wf("wf_0003", "golden", "inputs", "normal", "1.csv").exists()
    assert not repo.wf("wf_0003", "golden", "outputs", "normal", "10.csv").exists()


def test_main_import_set_with_corrupt_yxdb_exits_1_and_writes_nothing(tmp_path, capsys):
    repo = _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])
    capsys.readouterr()

    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    fields = [{"name": "ACCT", "type": "V_String", "size": 20, "scale": None}]
    yxdb.write_yxdb(capture_dir / "in_1.yxdb", fields, [["4000"]])
    (capture_dir / "out_10.yxdb").write_bytes(b"not a yxdb file" * 40)

    rc = inject_outputs.main(["wf_0003", "--capture-dir", str(capture_dir),
                              "--import-set", "normal", "--root", str(tmp_path)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "out_10.yxdb" in err
    assert not repo.wf("wf_0003", "golden", "inputs", "normal", "1.csv").exists()


def test_main_instrument_unexpected_exception_exits_2(tmp_path, monkeypatch):
    repo = _prepared_repo(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(inject_outputs, "inject", boom)

    rc = inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])

    assert rc == 2
    assert not repo.wf("wf_0003", "source", "gl_period_close.instrumented.yxmd").exists()


# --- Task P4 fix round 1, B2: an import records its golden set -------------------------------------
#
# The orchestrator's golden stage (`stageGolden` in orchestrator/stages.ts) looks only at
# `manifest.golden_sets`; with `golden.producer: "alteryx"` an import that did not record its set left
# the workflow BLOCKED however many captures were imported. The end-to-end half (a real import, then
# `--from-stage golden` reaching DONE) is orchestrator/test/integration.test.ts.

def _write_captures(capture_dir: Path) -> None:
    fields = [{"name": "ACCT", "type": "V_String", "size": 20, "scale": None}]
    for filename in ("in_1.yxdb", "out_10.yxdb"):
        yxdb.write_yxdb(capture_dir / filename, fields, [["4000"]])


def test_an_import_records_its_set_in_manifest_golden_sets_once_and_in_order(tmp_path, capsys):
    repo = _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])
    capture_dir = tmp_path / "captures"
    _write_captures(capture_dir)

    def run(golden_set: str) -> int:
        return inject_outputs.main(["wf_0003", "--capture-dir", str(capture_dir),
                                    "--import-set", golden_set, "--root", str(tmp_path)])

    assert load_manifest(repo, "wf_0003").get("golden_sets") in (None, [])
    assert run("normal") == 0
    assert load_manifest(repo, "wf_0003")["golden_sets"] == ["normal"]
    assert run("edge") == 0
    assert load_manifest(repo, "wf_0003")["golden_sets"] == ["normal", "edge"]
    assert run("normal") == 0                       # a re-import replaces the CSVs, never duplicates the set
    assert load_manifest(repo, "wf_0003")["golden_sets"] == ["normal", "edge"]
    capsys.readouterr()


def test_a_failed_import_records_no_golden_set(tmp_path, capsys):
    repo = _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])
    empty_capture_dir = tmp_path / "empty_but_present"
    empty_capture_dir.mkdir()

    rc = inject_outputs.main(["wf_0003", "--capture-dir", str(empty_capture_dir),
                              "--import-set", "normal", "--root", str(tmp_path)])

    assert rc == 1
    assert load_manifest(repo, "wf_0003").get("golden_sets") in (None, [])
    capsys.readouterr()


def test_an_import_whose_set_cannot_be_recorded_exits_2_and_says_so(tmp_path, capsys):
    """Every CSV is written before the manifest is touched; a manifest that cannot be read is a crash
    (exit 2) whose message says the set was imported but not recorded, never a silent success."""
    repo = _prepared_repo(tmp_path)
    inject_outputs.main(["wf_0003", "--capture-dir", CAPTURE_DIR, "--root", str(tmp_path)])
    capture_dir = tmp_path / "captures"
    _write_captures(capture_dir)
    repo.wf("wf_0003", "manifest.json").write_text("{ not json", encoding="utf-8")
    capsys.readouterr()

    rc = inject_outputs.main(["wf_0003", "--capture-dir", str(capture_dir),
                              "--import-set", "normal", "--root", str(tmp_path)])

    assert rc == 2
    assert "NOT recorded" in capsys.readouterr().err
    assert repo.wf("wf_0003", "golden", "inputs", "normal", "1.csv").exists()
