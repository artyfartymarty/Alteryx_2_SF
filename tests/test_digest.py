"""`digest.py`: the deterministic, one-line-per-tool reading of a parsed workflow that the analyzer and
the translator work from (objective-first design §4.1), and `lib/tool_lines.py`'s per-type summaries.

A digest is never written by a model: the same `parsed/dag.json` gives byte-identical text. It lists
one section per output (the tools upstream of it in topological order), puts a tool that feeds
several outputs once in a leading "Shared" section, and carries on each line what decides the tool's
behaviour -- the expression, the keys, the write mode -- whole, because that text is the logic.

Nothing here has run on Snowflake or Alteryx: the dags are the seed samples' `.yxmd` files parsed
locally.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

import digest
import parse
from lib import tool_lines
from lib.io import read_json, write_json
from lib.paths import Repo
from lib.vocab import DATA_LESS_TYPES
from tests.helpers import prepare_intake_ready

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
WORKFLOWS = [f"wf_000{n}" for n in range(1, 8)]
_DAGS: dict[str, dict] = {}


def sample_dag(wf_id: str) -> dict:
    """The sample's `parsed/dag.json`, as `parse.py` builds it from the `.yxmd` (cached: parsing is the
    slow part and nothing here edits it)."""
    if wf_id not in _DAGS:
        source = next((SAMPLES / wf_id / "source").glob("*.yxmd"))
        _DAGS[wf_id], _ = parse.parse_file(source)
    return copy.deepcopy(_DAGS[wf_id])


def node_of(wf_id: str, tool_id: str) -> dict:
    return next(n for n in sample_dag(wf_id)["nodes"] if n["tool_id"] == tool_id)


def tool_lines_of(text: str) -> list[str]:
    """Every line that starts a tool (`T<id> `), nested macro tools included."""
    return [line for line in text.splitlines() if re.match(r"\s*T\d", line)]


# --- the golden digest of wf_0001 --------------------------------------------------------------------

WF_0001_DIGEST = """\
# wf_0001 — digest

8 tools · 2 outputs · from parsed/dag.json (deterministic; never edited by hand)

Each line: `T<id> <Type>  <what it does>  ← <where its rows come from>  (<anchor> → <where they go>)  — <annotation>`. `T3.T` is anchor T of tool 3. Expressions are kept whole.

## Shared by more than one output

T1 Input  read orders.yxdb (yxdb)  — Orders extract
T2 Select  CUSTOMER:String(10); STATUS → ORDER_STATUS; other fields kept  ← T1  — Shorten CUSTOMER, rename STATUS
T3 Filter  [REGION] != "WEST"  ← T2   (T → T4, F → T8)  — Exclude WEST

## Output T7 · sales_summary.yxdb

Shared tools used: T1, T2, T3
T4 Formula  AMOUNT:Double = ToNumber([AMOUNT_TXT]); NET:Double = Round([AMOUNT] * IIF([QTY] >= 10, 0.9, 1), 0.01); SIZE_BAND:V_String(10) = IF [AMOUNT] >= 1000 THEN "LARGE" ELSEIF [AMOUNT] >= 100 THEN "MEDIUM" ELSE "SMALL" ENDIF  ← T3.T  — AMOUNT, NET, SIZE_BAND
T5 Summarize  group by REGION, SIZE_BAND; Sum(NET) as TOTAL_NET; Count(ORDER_ID) as ORDERS; CountNonNull(AMOUNT) as PRICED_ORDERS; Max(ORDER_DATE) as LAST_ORDER  ← T4  — Totals by region and band
T6 Sort  TOTAL_NET desc, REGION asc  ← T5  — Biggest totals first
T7 Output  write sales_summary.yxdb (yxdb), mode overwrite  ← T6  — sales_summary.yxdb

## Output T8 · excluded_orders.csv

Shared tools used: T1, T2, T3
T8 Output  write excluded_orders.csv (csv), mode overwrite  ← T3.F  — excluded_orders.csv
"""


@pytest.fixture(scope="module")
def wf_0001_text() -> str:
    return digest.render(sample_dag("wf_0001"), "wf_0001")


def test_the_wf_0001_digest_is_exactly_this(wf_0001_text):
    assert wf_0001_text == WF_0001_DIGEST


@pytest.mark.parametrize("line", [
    'T3 Filter  [REGION] != "WEST"  ← T2   (T → T4, F → T8)  — Exclude WEST',
    "T2 Select  CUSTOMER:String(10); STATUS → ORDER_STATUS; other fields kept  ← T1  — Shorten CUSTOMER, rename STATUS",
    "T6 Sort  TOTAL_NET desc, REGION asc  ← T5  — Biggest totals first",
])
def test_a_tool_line_carries_id_type_config_inbound_outbound_and_annotation(wf_0001_text, line):
    assert line in wf_0001_text.splitlines()


def test_a_tool_that_feeds_several_outputs_is_listed_once_in_the_shared_section(wf_0001_text):
    lines = wf_0001_text.splitlines()
    for tool in ("T1", "T2", "T3"):
        assert sum(1 for line in lines if line.startswith(f"{tool} ")) == 1, f"{tool} must be listed exactly once"
    shared = wf_0001_text.split("## Shared by more than one output")[1].split("## Output")[0]
    assert [line.split()[0] for line in tool_lines_of(shared)] == ["T1", "T2", "T3"]
    for section in wf_0001_text.split("## Output")[1:]:
        assert "Shared tools used: T1, T2, T3" in section, "an output's section names the shared tools by id"
        assert not [line for line in tool_lines_of(section) if line.split()[0] in ("T1", "T2", "T3")]


def test_a_tool_that_feeds_one_output_is_in_that_outputs_section_in_topological_order(wf_0001_text):
    seven = wf_0001_text.split("## Output T7")[1].split("## Output T8")[0]
    assert [line.split()[0] for line in tool_lines_of(seven)] == ["T4", "T5", "T6", "T7"]


# --- determinism ---------------------------------------------------------------------------------------

def test_the_same_dag_gives_the_same_text_whatever_order_its_nodes_and_edges_are_in():
    dag = sample_dag("wf_0002")
    shuffled = copy.deepcopy(dag)
    shuffled["nodes"].reverse()
    shuffled["edges"].reverse()
    assert digest.render(dag, "wf_0002") == digest.render(dag, "wf_0002") == digest.render(shuffled, "wf_0002")


def test_the_written_file_is_utf8_with_lf_endings_and_identical_on_a_second_run(tmp_path):
    repo = prepare_intake_ready(tmp_path, "wf_0001")
    path = digest.write(repo, "wf_0001")
    first = path.read_bytes()
    assert path == repo.wf("wf_0001", "digest.md")
    assert b"\r" not in first and first.endswith(b"\n")
    assert first.decode("utf-8") == WF_0001_DIGEST
    assert digest.write(repo, "wf_0001").read_bytes() == first


# --- what a digest lists --------------------------------------------------------------------------------

@pytest.mark.parametrize("wf_id", WORKFLOWS)
def test_every_data_carrying_top_level_tool_is_listed_exactly_once(wf_id):
    dag = sample_dag(wf_id)
    text = digest.render(dag, wf_id)
    wanted = sorted((n["tool_id"] for n in dag["nodes"] if n["type"] not in DATA_LESS_TYPES), key=int)
    top_level = [re.match(r"T(\d+) ", line).group(1) for line in text.splitlines()
                 if re.match(r"T\d+ ", line)]
    assert sorted(top_level, key=int) == wanted
    assert f"{len(wanted)} tools" in text.splitlines()[2]


def test_containers_comments_and_browse_tools_are_not_listed_but_counted_in_the_header():
    dag = sample_dag("wf_0002")
    text = digest.render(dag, "wf_0002")
    listed = {re.match(r"T(\d+) ", line).group(1) for line in text.splitlines() if re.match(r"T\d+ ", line)}
    assert not listed & {"100", "200", "210", "300", "11"}
    assert "5 nodes that carry no data (containers, comments, browse tools) are not listed." in text


def test_a_macros_inner_tools_follow_it_indented_and_numbered_under_it():
    text = digest.render(sample_dag("wf_0004"), "wf_0004")
    lines = text.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("T2 Macro"))
    assert lines[at] == ("T2 Macro  clean_codes.yxmc; MinQty = 1 (default 0)  ← T1  — Clean SKU codes, drop empty stock")
    assert lines[at + 1:at + 6] == [
        "  T2/1 MacroInput  — Stock rows",
        '  T2/2 RegEx  replace on SKU: expression "^\\s*([A-Za-z]+)[- ]?(\\d+)\\s*$"; replacement "$1-$2"; '
        "case-insensitive; copy unmatched  ← T2/1  — Normalise to FAMILY-NUMBER",
        "  T2/3 Formula  SKU:V_String(30) = Uppercase([SKU])  ← T2/2  — Upper-case the code",
        "  T2/4 Filter  [QTY] >= [%Question.MinQty%]  ← T2/3   (T → T2/5, F → none)  — Drop stock below MinQty",
        "  T2/5 MacroOutput  ← T2/4.T  — Cleaned stock rows",
    ]
    assert lines[at + 6].startswith("T3 RegEx"), "the macro's inner tools end where the next top-level tool starts"


def test_a_workflow_without_an_output_tool_gets_a_section_for_each_terminal_stream():
    dag = sample_dag("wf_0001")
    dag["nodes"] = [n for n in dag["nodes"] if n["type"] != "output"]
    dag["edges"] = [e for e in dag["edges"] if e["dst"] not in ("7", "8")]
    text = digest.render(dag, "wf_0001")
    assert "## Terminal stream T6 (no Output tool)" in text
    assert "## Terminal stream T3" not in text, "T3 still feeds T4: only a tool nothing reads is a terminal stream"
    assert "1 output " in text.splitlines()[2] and "terminal" not in text.splitlines()[2]


def test_an_annotation_or_expression_with_a_line_break_stays_on_one_line():
    dag = sample_dag("wf_0001")
    next(n for n in dag["nodes"] if n["tool_id"] == "3")["annotation"] = "Exclude\nWEST\u2028now"
    next(n for n in dag["nodes"] if n["tool_id"] == "3")["config"]["expression"] = "[REGION]\n!= \"WEST\""
    text = digest.render(dag, "wf_0001")
    assert 'T3 Filter  [REGION]\\n!= "WEST"  ← T2   (T → T4, F → T8)  — Exclude\\nWEST\\u2028now' in text.splitlines()


def test_a_tool_inside_a_cycle_is_still_listed_rather_than_dropped():
    dag = sample_dag("wf_0001")
    dag["edges"].append({"src": "6", "src_anchor": "Output", "dst": "4", "dst_anchor": "Input", "dst_order": 2,
                         "wireless": False})
    text = digest.render(dag, "wf_0001")
    assert {line.split()[0] for line in tool_lines_of(text)} == {f"T{n}" for n in range(1, 9)}


def test_workflow_constants_are_listed_so_an_expression_that_reads_one_can_be_understood():
    text = digest.render(sample_dag("wf_0003"), "wf_0003")
    section = text.split("## Workflow constants")[1].split("## ")[0]
    assert [line for line in section.splitlines() if " = " in line] == [
        "User.PeriodEnd = 2026-08-31", "User.Region = EMEA"]
    assert "## Workflow constants" not in digest.render(sample_dag("wf_0001"), "wf_0001")


def test_a_join_names_which_side_each_input_arrives_on():
    text = digest.render(sample_dag("wf_0002"), "wf_0002")
    join = next(line for line in text.splitlines() if line.startswith("T5 Join"))
    assert join == ("T5 Join  on Left.CUST_ID = Right.CUST_ID; drop Right_CUST_ID  ← Left: T2, Right: T4   "
                    "(L → T7, J → T6, R → T8)  — Customers to orders on CUST_ID")


def test_a_union_lists_its_inputs_in_connection_order():
    text = digest.render(sample_dag("wf_0002"), "wf_0002")
    union = next(line for line in text.splitlines() if line.startswith("T9 Union"))
    assert union == "T9 Union  by name  ← T6, T7, T8  — Stack the three flags"


# --- per-type summaries (lib/tool_lines.config_summary) -----------------------------------------------

REAL_NODES = [
    # (workflow, tool id, what the line says about the tool)
    ("wf_0002", "3", 'read orders_export.csv (csv, delimiter ",", header row, codepage 28591)'),
    ("wf_0003", "1", "read db:prod_fin; query: SELECT ACCT, PERIOD, POSTED, AMOUNT, REGION, ENTRY_ID "
                     "FROM dbo.GL_LEDGER WHERE AMOUNT <> 0"),
    ("wf_0002", "4", "ORDER_ID:Int32; CUST_ID:Int32; AMOUNT:Double; ORDER_DATE:Date; other fields dropped"),
    ("wf_0002", "2", "fields NAME, CITY: replace null text with blank, trim whitespace, upper case"),
    ("wf_0003", "3", 'to_datetime POSTED with format "%d/%m/%Y" → POSTED_DT'),
    ("wf_0003", "4", "ACCT asc, POSTED_DT asc, ENTRY_ID desc"),
    ("wf_0003", "5", "keys ACCT, POSTED_DT"),
    ("wf_0003", "6", "new field RUN_BAL:Double = [Row-1:RUN_BAL] + [AMOUNT]; rows 1; group by ACCT; "
                     "missing rows → zero"),
    ("wf_0003", "7", "new field RecordID:Int32 starting at 1, position first"),
    ("wf_0003", "8", 'PERIOD_END_FLAG:V_String(1) = IIF(DateTimeFormat([POSTED_DT], "%Y-%m-%d") = '
                     '[User.PeriodEnd], "Y", "N")'),
    ("wf_0003", "9", "group by ACCT, PERIOD; Sum(AMOUNT) as TOTAL; Last(RUN_BAL) as CLOSING_BAL; "
                     "Count(ENTRY_ID) as ENTRIES; Max(PERIOD_END_FLAG) as HAS_PERIOD_END"),
    ("wf_0003", "10", "write db:prod_fin table dbo.GL_SUMMARY, mode update_insert, keys ACCT, PERIOD; "
                      "PreSQL: DELETE FROM dbo.GL_SUMMARY WHERE PERIOD < '2020-01'; "
                      "PostSQL: UPDATE dbo.GL_SUMMARY SET LOADED_FLAG = 'Y' WHERE LOADED_FLAG IS NULL"),
    ("wf_0002", "10", "write db:dw_sales table dbo.CUSTOMER_ORDER_FACT, mode append"),
    ("wf_0004", "3", 'parse on SKU: expression "^([A-Z]+)-(\\d+)$"; case-sensitive; '
                     "output fields FAMILY:V_String(20), ITEM_NO:V_String(10)"),
    ("wf_0004", "4", "group by SKU, FAMILY; header WAREHOUSE; data QTY; methods Sum"),
    ("wf_0004", "5", "key fields SKU; data fields EAST, NORTH, WEST"),
    ("wf_0005", "2", "plugin AcmeAnalytics.Dedupe.DedupeTool; raw XML: <Configuration> <KeyField>ACCT</KeyField> "
                     "<Keep>MaxDate</Keep> <DateField>UPDATED</DateField> </Configuration>"),
    ("wf_0005", "3", "command C:\\scripts\\notify.bat"),
    ("wf_0007", "5", "group by REGION, PERIOD; Sum(TARGET) as TARGET_TOTAL; Sum(ACTUAL) as ACTUAL_TOTAL; "
                     "Count(REGION) as LINES"),
]


@pytest.mark.parametrize("wf_id, tool_id, expected", REAL_NODES, ids=[f"{w}/T{t}" for w, t, _ in REAL_NODES])
def test_config_summary_of_a_real_tool(wf_id, tool_id, expected):
    assert tool_lines.config_summary(node_of(wf_id, tool_id)) == expected


def test_a_python_tools_script_is_kept_whole_with_its_line_breaks_escaped():
    summary = tool_lines.config_summary(node_of("wf_0006", "3"))
    assert summary.startswith('script: import pandas as pd\\ndf = Alteryx.read("#1")\\n')
    assert summary.endswith("Alteryx.write(schedule, 1)")
    assert "\n" not in summary


@pytest.mark.parametrize("node, expected", [
    ({"type": "sample", "config": {"mode": "first", "n": 10, "group_by": []}}, "first 10 rows"),
    ({"type": "sample", "config": {"mode": "last", "n": 3, "group_by": ["REGION"]}}, "last 3 rows; group by REGION"),
    ({"type": "sample", "config": {"mode": "skip", "n": 2, "group_by": []}}, "skip first 2 rows"),
    ({"type": "sample", "config": {"mode": "one_in_n", "n": 5, "group_by": []}}, "1 in every 5 rows"),
    ({"type": "union", "config": {"mode": "position"}}, "by position"),
    ({"type": "multi_row_formula", "config": {"field": "X", "update_existing": True, "type": None, "size": None,
                                              "num_rows": 2, "expression": "[Row-1:X]", "group_by": [],
                                              "unknown_rows": "nearest"}},
     "update field X = [Row-1:X]; rows 2; missing rows → nearest"),
    ({"type": "summarize", "config": {"fields": [
        {"field": "NAME", "action": "Concat", "rename": "NAMES", "separator": ","},
        {"field": "REGION", "action": "GroupBy", "rename": "AREA", "separator": None}]}},
     'group by REGION as AREA; Concat(NAME, sep ",") as NAMES'),
    ({"type": "formula", "config": {"formulas": [
        {"field": "PRICE", "expression": "[A]*2", "type": "FixedDecimal", "size": 19, "scale": 2}]}},
     "PRICE:FixedDecimal(19,2) = [A]*2"),
    ({"type": "regex", "config": {"field": "X", "expression": "a", "case_insensitive": True, "method": "match",
                                  "replace": None, "copy_unmatched": False, "output_fields": [],
                                  "match_field": "HIT"}},
     'match on X: expression "a"; case-insensitive; match into HIT'),
    ({"type": "input", "config": {"source": "\\\\fileserver\\crm\\orders.csv", "format": "csv", "query": None,
                                  "alias": None, "csv": {"delimiter": "|", "header": False, "codepage": None},
                                  "record_limit": 100}},
     'read orders.csv (csv, delimiter "|", no header row); first 100 records'),
    ({"type": "macro", "config": {"values": {}}, "macro_path": None, "unresolved": True},
     "macro (file not found)"),
    ({"type": "mystery_tool", "config": {"a": 1, "b": "x"}}, "a=1, b=x"),
    ({"type": "unknown", "plugin": "Acme.Tool", "config": {"key_field": "ACCT"}, "raw_config": "<Configuration/>"},
     "plugin Acme.Tool; key_field=ACCT"),
    ({"type": "browse", "config": {}}, ""),
])
def test_config_summary_of_a_synthetic_tool(node, expected):
    assert tool_lines.config_summary(node) == expected


# --- the command line -------------------------------------------------------------------------------

def test_the_cli_writes_the_digest_and_prints_its_path(tmp_path, capsys):
    repo = prepare_intake_ready(tmp_path, "wf_0001")
    assert digest.main(["wf_0001", "--root", str(tmp_path)]) == 0
    printed = capsys.readouterr().out.strip()
    assert Path(printed) == repo.wf("wf_0001", "digest.md")
    assert Path(printed).read_text(encoding="utf-8") == WF_0001_DIGEST


def test_the_cli_exits_2_when_there_is_no_parsed_dag(tmp_path, capsys):
    assert digest.main(["wf_0001", "--root", str(tmp_path)]) == 2
    assert "parsed/dag.json" in capsys.readouterr().err
    assert not (tmp_path / "workflows").exists(), "nothing is written on a usage error"


def test_the_cli_exits_2_for_a_workflow_id_that_is_not_one(tmp_path, capsys):
    assert digest.main(["../escape", "--root", str(tmp_path)]) == 2
    assert capsys.readouterr().err


def test_the_cli_exits_2_for_a_dag_it_cannot_read(tmp_path, capsys):
    repo = Repo(tmp_path)
    write_json(repo.wf("wf_0001", "parsed", "dag.json"), {"nodes": "not a list"})
    assert digest.main(["wf_0001", "--root", str(tmp_path)]) == 2
    assert capsys.readouterr().err


def test_the_cli_reads_the_dag_it_finds_under_root(tmp_path):
    repo = Repo(tmp_path)
    write_json(repo.wf("wf_0001", "parsed", "dag.json"), sample_dag("wf_0001"))
    assert digest.main(["wf_0001", "--root", str(tmp_path)]) == 0
    assert read_json(repo.wf("wf_0001", "parsed", "dag.json"))["nodes"], "the dag is read, never rewritten"
    assert repo.wf("wf_0001", "digest.md").read_text(encoding="utf-8") == WF_0001_DIGEST
