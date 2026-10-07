"""`lib/minimality.py`: the three lint flags of design §4.6 (`pass_through_cte`, `tool_by_tool`,
`single_use_chain`), computed from sqlglot's tree of each statement of a `build.sql`.

A flag never changes `compile_check.py`'s exit code; these tests pin only what is flagged, under which
kind, name and line, and what is deliberately left alone (a CTE that filters, groups, joins, orders, ...).
"""
from __future__ import annotations

import pytest

from lib import minimality


def objective(*, tools: int = 8, inputs=("1",), outputs=("9",)) -> dict:
    """A bare objective with `tools` non-input/output tools spread over two steps."""
    middle = [str(10 + n) for n in range(tools)]
    return {"inputs": [{"tool_id": tool} for tool in inputs],
            "outputs": [{"tool_id": tool} for tool in outputs],
            "steps": [{"id": "s1", "tools": [*inputs, *middle[: tools // 2]]},
                      {"id": "s2", "tools": [*middle[tools // 2:], *outputs]}],
            "no_effect": []}


def flags(text: str, **kwargs) -> list[dict]:
    return minimality.lint_text(text, objective(**kwargs))["flags"]


def kinds(text: str, **kwargs) -> list[tuple[str, str]]:
    return [(flag["kind"], flag["name"]) for flag in flags(text, **kwargs)]


# --- pass_through_cte -----------------------------------------------------------------------------

def test_a_cte_that_only_selects_bare_columns_of_one_relation_is_flagged_with_its_name_and_line():
    text = """-- step s1
CREATE OR REPLACE TABLE {{tgt}}.OUT AS
WITH t2 AS (
  SELECT ID, NOTE FROM {{src}}.ITEMS
)
SELECT ID, NOTE FROM t2;
"""
    found = flags(text)
    assert [(f["kind"], f["name"], f["line"]) for f in found] == [("pass_through_cte", "t2", 3)]
    assert "inline" in found[0]["note"]


def test_a_cte_that_renames_columns_is_a_pass_through():
    text = """CREATE OR REPLACE TABLE {{tgt}}.OUT AS
WITH renamed AS (SELECT ID AS ORDER_ID, STATUS AS ORDER_STATUS FROM {{src}}.ITEMS)
SELECT ORDER_ID, ORDER_STATUS FROM renamed;
"""
    assert kinds(text) == [("pass_through_cte", "renamed")]


@pytest.mark.parametrize("projection", ["*", "t.*", "ITEMS.*"])
def test_a_select_star_cte_from_one_relation_is_a_pass_through(projection):
    text = f"""CREATE OR REPLACE TABLE {{{{tgt}}}}.OUT AS
WITH everything AS (SELECT {projection} FROM {{{{src}}}}.ITEMS t)
SELECT ID FROM everything;
"""
    assert kinds(text) == [("pass_through_cte", "everything")]


def test_a_temporary_table_that_only_projects_columns_is_a_pass_through():
    text = """-- step s1
CREATE OR REPLACE TEMPORARY TABLE items AS
SELECT ID, NOTE FROM {{src}}.ITEMS;

CREATE OR REPLACE TABLE {{tgt}}.OUT AS SELECT ID, NOTE FROM items WHERE NOTE IS NOT NULL;
"""
    found = flags(text)
    assert [(f["kind"], f["name"], f["line"]) for f in found] == [("pass_through_cte", "items", 2)]


@pytest.mark.parametrize("body", [
    "SELECT ID, NOTE FROM {{src}}.ITEMS WHERE NOTE IS NOT NULL",                       # filters
    "SELECT ID, COUNT(*) AS N FROM {{src}}.ITEMS GROUP BY ID",                         # groups
    "SELECT ID, COUNT(*) AS N FROM {{src}}.ITEMS GROUP BY ID HAVING COUNT(*) > 1",     # having
    "SELECT ID, NOTE FROM {{src}}.ITEMS QUALIFY ROW_NUMBER() OVER (PARTITION BY ID ORDER BY NOTE) = 1",
    "SELECT a.ID, b.NOTE FROM {{src}}.ITEMS a JOIN {{src}}.OTHER b ON a.ID = b.ID",    # joins
    "SELECT DISTINCT ID, NOTE FROM {{src}}.ITEMS",                                     # distinct
    "SELECT ID, ROW_NUMBER() OVER (ORDER BY ID) AS RN FROM {{src}}.ITEMS",             # window
    "SELECT ID, NOTE FROM {{src}}.ITEMS ORDER BY ID",                                  # order by
    "SELECT ID, NOTE FROM {{src}}.ITEMS LIMIT 10",                                     # limit
    "SELECT ID, TRY_TO_DOUBLE(NOTE) AS NOTE_NUM FROM {{src}}.ITEMS",                   # computes
    "SELECT ID, NOTE FROM {{src}}.ITEMS UNION ALL SELECT ID, NOTE FROM {{src}}.OTHER", # two relations
])
def test_a_cte_that_does_real_work_is_not_flagged(body):
    text = f"""CREATE OR REPLACE TABLE {{{{tgt}}}}.OUT AS
WITH work AS ({body})
SELECT * FROM work;
"""
    assert flags(text) == []


def test_the_final_query_of_a_statement_is_not_a_cte_and_is_never_flagged():
    text = "CREATE OR REPLACE TABLE {{tgt}}.OUT AS SELECT ID, NOTE FROM {{src}}.ITEMS;\n"
    assert flags(text) == []


def test_a_quoted_cte_name_is_reported_unquoted():
    text = """CREATE OR REPLACE TABLE {{tgt}}.OUT AS
WITH "Items" AS (SELECT ID FROM {{src}}.ITEMS)
SELECT ID FROM "Items";
"""
    assert kinds(text) == [("pass_through_cte", "Items")]


# --- tool_by_tool ---------------------------------------------------------------------------------

#: Four CTEs that each do real work (so no pass_through flag muddies the count).
FOUR_CTES = """CREATE OR REPLACE TABLE {{tgt}}.OUT AS
WITH c1 AS (SELECT ID, TRY_TO_DOUBLE(NOTE) AS A FROM {{src}}.ITEMS WHERE ID > 0),
     c2 AS (SELECT ID, A, A * 2 AS B FROM c1 WHERE A IS NOT NULL),
     c3 AS (SELECT ID, B, B + 1 AS C FROM c2 WHERE B > 0),
     c4 AS (SELECT ID, C, C - 1 AS D FROM c3 WHERE C > 0)
SELECT ID, D FROM c4;
"""


def test_as_many_ctes_as_there_are_tools_is_the_shape_of_a_per_tool_translation():
    found = flags(FOUR_CTES, tools=4)
    assert [flag["kind"] for flag in found] == ["tool_by_tool"]
    assert found[0]["name"] == "c1" and found[0]["line"] == 2
    assert "4 CTEs" in found[0]["note"] and "4 tools" in found[0]["note"]


def test_fewer_ctes_than_tools_is_not_tool_by_tool():
    assert flags(FOUR_CTES, tools=9) == []


def test_fewer_than_four_ctes_is_never_tool_by_tool():
    three = FOUR_CTES.replace(",\n     c4 AS (SELECT ID, C, C - 1 AS D FROM c3 WHERE C > 0)", "").replace(
        "SELECT ID, D FROM c4", "SELECT ID, C FROM c3")
    assert flags(three, tools=2) == []


def test_temporary_tables_count_towards_tool_by_tool_with_the_ctes():
    text = """CREATE OR REPLACE TEMPORARY TABLE t1 AS SELECT ID, TRY_TO_DOUBLE(NOTE) AS A FROM {{src}}.ITEMS WHERE ID > 0;
CREATE OR REPLACE TEMPORARY TABLE t2 AS SELECT ID, A * 2 AS B FROM t1 WHERE A IS NOT NULL;
CREATE OR REPLACE TABLE {{tgt}}.OUT AS
WITH c3 AS (SELECT ID, B + 1 AS C FROM t2 WHERE B > 0),
     c4 AS (SELECT ID, C - 1 AS D FROM c3 WHERE C > 0)
SELECT ID, D FROM c4;
"""
    result = minimality.lint_text(text, objective(tools=4))
    assert result["ctes"] == 4
    assert [flag["kind"] for flag in result["flags"]] == ["tool_by_tool"]


# --- single_use_chain -----------------------------------------------------------------------------

#: Three CTEs in a row, each read only by the next, none filtering, grouping or joining (each computes,
#: so none is a pass-through).
CHAIN = """CREATE OR REPLACE TABLE {{tgt}}.OUT AS
WITH a AS (SELECT ID, TRY_TO_DOUBLE(NOTE) AS X FROM {{src}}.ITEMS),
     b AS (SELECT ID, X * 2 AS Y FROM a),
     c AS (SELECT ID, Y + 1 AS Z FROM b)
SELECT ID, Z FROM c;
"""


def test_three_ctes_in_a_row_each_read_only_by_the_next_are_a_single_use_chain():
    found = flags(CHAIN)
    assert [(f["kind"], f["name"], f["line"]) for f in found] == [("single_use_chain", "a", 2)]
    assert "a, b, c" in found[0]["note"]


def test_a_filtering_cte_breaks_the_chain():
    broken = CHAIN.replace("SELECT ID, X * 2 AS Y FROM a)", "SELECT ID, X * 2 AS Y FROM a WHERE X > 0)")
    assert flags(broken) == []


def test_a_grouping_or_joining_cte_breaks_the_chain():
    grouped = CHAIN.replace("SELECT ID, X * 2 AS Y FROM a)", "SELECT ID, SUM(X) AS Y FROM a GROUP BY ID)")
    joined = CHAIN.replace("SELECT ID, X * 2 AS Y FROM a)",
                           "SELECT a.ID, a.X * 2 AS Y FROM a JOIN {{src}}.OTHER o ON a.ID = o.ID)")
    assert flags(grouped) == [] and flags(joined) == []


def test_a_cte_read_by_two_others_is_not_single_use():
    shared = CHAIN.replace("SELECT ID, Z FROM c", "SELECT c.ID, c.Z FROM c JOIN a ON c.ID = a.ID")
    assert flags(shared) == []


def test_two_ctes_in_a_row_are_not_a_chain():
    two = """CREATE OR REPLACE TABLE {{tgt}}.OUT AS
WITH a AS (SELECT ID, TRY_TO_DOUBLE(NOTE) AS X FROM {{src}}.ITEMS),
     b AS (SELECT ID, X * 2 AS Y FROM a)
SELECT ID, Y FROM b;
"""
    assert flags(two) == []


def test_temporary_tables_chain_across_statements():
    text = """CREATE OR REPLACE TEMPORARY TABLE a AS SELECT ID, TRY_TO_DOUBLE(NOTE) AS X FROM {{src}}.ITEMS;
CREATE OR REPLACE TEMPORARY TABLE b AS SELECT ID, X * 2 AS Y FROM a;
CREATE OR REPLACE TEMPORARY TABLE c AS SELECT ID, Y + 1 AS Z FROM b;
CREATE OR REPLACE TABLE {{tgt}}.OUT AS SELECT ID, Z FROM c WHERE Z > 0;
"""
    assert [(f["kind"], f["name"], f["line"]) for f in flags(text)] == [("single_use_chain", "a", 1)]


# --- the report ----------------------------------------------------------------------------------

def test_the_report_counts_lines_statements_and_ctes():
    result = minimality.lint_text(CHAIN, objective())
    assert (result["lines"], result["statements"], result["ctes"]) == (5, 1, 3)


def test_a_session_statement_counts_as_a_statement_and_is_not_linted():
    text = "ALTER SESSION SET TIMEZONE = 'America/New_York';\n" + CHAIN
    result = minimality.lint_text(text, objective())
    assert result["statements"] == 2 and [f["kind"] for f in result["flags"]] == ["single_use_chain"]
    assert result["flags"][0]["line"] == 3


def test_a_statement_that_does_not_parse_is_skipped_not_a_crash():
    text = CHAIN + "\nSELEC 1;\n"
    assert [f["kind"] for f in minimality.lint_text(text, objective())["flags"]] == ["single_use_chain"]


def test_flags_are_plain_json_with_exactly_four_keys():
    flag = flags("CREATE OR REPLACE TABLE {{tgt}}.OUT AS\nWITH t AS (SELECT * FROM {{src}}.ITEMS)\nSELECT ID FROM t;\n")[0]
    assert sorted(flag) == ["kind", "line", "name", "note"]
    assert isinstance(flag["line"], int)
