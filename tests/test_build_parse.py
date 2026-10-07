"""`lib/build_parse.py` (a build file's statements as sqlglot trees, placeholders understood) and
`lib/build_sql.step_markers` (every comment that is, or tries to be, a step marker): the two pieces
`compile_check.py` and `lib/minimality.py` share.
"""
from __future__ import annotations

import pytest

from lib import build_parse, build_sql
from lib.write_modes import ContractError, objective_targets


def parsed(text: str):
    return [(statement, build_parse.parse(statement)) for statement in build_sql.split_statements(text)]


# --- sentinel placeholders -------------------------------------------------------------------------------

def test_a_placeholder_is_parsed_as_a_two_part_sentinel_so_a_hand_written_schema_is_told_apart():
    text = "SELECT 1 FROM {{ src }}.ORDERS a JOIN MYDB.RAW.ORDERS b ON 1 = 1 JOIN {{tgt}}.OUT c ON 1 = 1"
    ((statement, tree),) = parsed(text)
    refs = build_parse.table_refs(statement, tree)
    assert [(ref.kind, ref.shown) for ref in refs] == [("src", "{{src}}.ORDERS"), ("other", "MYDB.RAW.ORDERS"),
                                                      ("tgt", "{{tgt}}.OUT")]


def test_cte_names_unqualified_tables_and_writes_are_classified():
    text = "INSERT INTO {{tgt}}.OUT WITH c AS (SELECT * FROM {{src}}.A) SELECT * FROM c JOIN tmp ON 1 = 1"
    ((statement, tree),) = parsed(text)
    refs = {(ref.name, ref.kind, ref.write) for ref in build_parse.table_refs(statement, tree)}
    assert refs == {("OUT", "tgt", True), ("A", "src", False), ("c", "cte", False), ("tmp", "plain", False)}


def test_a_table_function_is_not_a_named_table():
    ((statement, tree),) = parsed("SELECT * FROM TABLE(FLATTEN(INPUT => PARSE_JSON('[1]')))")
    assert build_parse.table_refs(statement, tree) == []


def test_unknown_placeholders_are_left_alone_by_the_sentinel_rendering():
    assert build_parse.sentinel("{{ other }} {{src}}") == "{{ other }} SRC__PH.SRC__PH"


def test_is_temporary_create():
    assert build_parse.is_temporary_create(parsed("CREATE OR REPLACE TEMPORARY TABLE t AS SELECT 1")[0][1])
    assert build_parse.is_temporary_create(parsed("CREATE TEMP TABLE t AS SELECT 1")[0][1])
    assert not build_parse.is_temporary_create(parsed("CREATE OR REPLACE TABLE t AS SELECT 1")[0][1])
    assert not build_parse.is_temporary_create(parsed("SELECT 1")[0][1])


# --- lines ------------------------------------------------------------------------------------------------

def test_first_line_is_the_line_of_the_comments_in_front_of_the_statement():
    text = "-- a\n-- b\nSELECT 1;\n\n-- c\nSELECT 2;\n"
    first, second = build_sql.split_statements(text)
    assert (first.start_line, build_parse.first_line(first)) == (3, 1)
    assert (second.start_line, build_parse.first_line(second)) == (6, 5)


def test_line_of_a_node_is_its_line_in_the_file():
    text = "-- header\nSELECT 1;\n\nCREATE OR REPLACE TEMPORARY TABLE t AS\nSELECT a,\n       b\nFROM {{src}}.X\nWHERE a > 0;\n"
    _, (statement, tree) = parsed(text)
    (write,) = build_parse.write_tables(tree)
    assert build_parse.line_of(statement, write.this) == 4
    src = next(ref for ref in build_parse.table_refs(statement, tree) if ref.kind == "src")
    assert src.line == 7


def test_the_line_of_an_unplaced_node_is_the_statements_own():
    ((statement, _),) = parsed("\n\nSELECT 1;")
    assert build_parse.line_of(statement, None) == 3


# --- step_markers ----------------------------------------------------------------------------------------------

def markers(text: str, comment: str = "--"):
    return [(m.line, m.steps) for m in build_sql.step_markers(text, comment)]


def test_a_well_formed_marker_has_its_steps():
    assert markers("-- step s1\nSELECT 1;\n  -- step s2, s10 \nSELECT 2;") == [(1, ("s1",)), (3, ("s2", "s10"))]


def test_a_python_marker_is_a_hash_comment():
    assert markers("x = 1\n# step s1\n    # step s2, s3\n-- step s4\n", "#") == [(2, ("s1",)), (3, ("s2", "s3"))]


@pytest.mark.parametrize("comment", ["-- Step s1: clean", "-- step s1: clean", "--step s1.", "-- STEP s1", "-- step s1 s2",
                                     "-- step s01", "-- step  s1,", "-- step s1, s0"])
def test_a_comment_that_tries_to_be_a_marker_has_no_steps(comment):
    assert markers(f"{comment}\nSELECT 1;") == [(1, ())]


def test_a_marker_after_code_on_its_line_is_not_a_marker():
    assert markers("SELECT 1; -- step s1\n") == [(1, ())]


def test_ordinary_comments_and_text_in_strings_are_not_markers():
    text = "-- step by step\n-- the steps are s1 and s2\nSELECT '-- step s1' AS x, \"-- step s2\" AS y;\n/* -- step s3 */\n"
    assert markers(text) == []


def test_markers_agree_with_the_regions_step_regions_returns():
    text = "-- step s1\nSELECT 1;\n-- step s2, s3\nSELECT 2;\n"
    assert [m.steps for m in build_sql.step_markers(text)] == [r.steps for r in build_sql.step_regions(text)]


# --- write_modes.objective_targets ---------------------------------------------------------------------------------

def output(**fields) -> dict:
    return {"tool_id": "7", "logical": "OUT", "write_mode": "overwrite", "keys": [], "pre_sql": None, "post_sql": None, **fields}


def test_objective_targets_normalises_the_write_mode_keys_and_pre_post():
    objective = {"outputs": [output(write_mode="merge", keys=["A", "B"], pre_sql="DELETE FROM x", post_sql="  "),
                             output(logical="TWO", write_mode="truncate_append", tool_id="8")]}
    first, second = objective_targets(objective)
    assert (first.logical, first.mode, first.spelling, first.keys, first.tool_id) == ("OUT", "update_insert", "merge", ("A", "B"), "7")
    assert (first.pre_sql, first.post_sql) == (True, False)
    assert (second.mode, second.pre_sql, second.post_sql) == ("truncate_append", False, False)


@pytest.mark.parametrize("fields, words", [
    ({"logical": None}, "logical"),
    ({"write_mode": None}, "write_mode"),
    ({"write_mode": "update_only"}, "update_only"),
    ({"write_mode": "update_insert", "keys": []}, "keys"),
])
def test_an_output_that_cannot_be_written_is_a_contract_error_naming_the_field(fields, words):
    with pytest.raises(ContractError, match=words):
        objective_targets({"outputs": [output(**fields)]})


# --- IDENTIFIER(...) tables (A3 review fix round 1) -----------------------------------------------------------------

def test_a_table_named_through_identifier_is_returned_with_kind_identifier_and_its_line():
    text = "SELECT 1\nFROM x\nJOIN IDENTIFIER('MYDB.RAW.ORDERS') y ON 1 = 1\nJOIN IDENTIFIER(:V) z ON 1 = 1"
    ((statement, tree),) = parsed(text)
    refs = [ref for ref in build_parse.table_refs(statement, tree) if ref.kind == "identifier"]
    assert [(ref.shown, ref.write, ref.line) for ref in refs] == [("IDENTIFIER('MYDB.RAW.ORDERS')", False, 3),
                                                                  ("IDENTIFIER(:V)", False, 4)]


def test_a_write_through_identifier_is_a_write():
    ((statement, tree),) = parsed("INSERT INTO IDENTIFIER(:T) (A) SELECT 1")
    (ref,) = [ref for ref in build_parse.table_refs(statement, tree) if ref.kind == "identifier"]
    assert ref.write is True
