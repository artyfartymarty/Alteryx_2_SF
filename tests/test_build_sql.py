"""`lib/build_sql.py`: the one place that reads a build file as statements, placeholders and step regions
(objective-first design §4.4): the statement splitter, the comment/string masking, the placeholders and the
step regions every script that reads a build file shares.

Nothing here has run against a real Snowflake account: these are tests of text handling.
"""
from __future__ import annotations

import textwrap
import time

import pytest

from lib import build_sql
from lib.build_sql import (Region, Statement, masked_code, placeholders, render, session_settings,
                           split_statements, step_regions)


def sql(text: str) -> str:
    """A test file's text: dedented, first newline dropped, so line 1 is the first line written."""
    return textwrap.dedent(text).lstrip("\n")


# --- split_statements -------------------------------------------------------------------------

def test_two_statements_split_without_their_semicolons_and_with_exact_lines():
    text = sql("""
        CREATE TABLE a AS
        SELECT 1;

        SELECT 2;
    """)
    assert split_statements(text) == [
        Statement(text="CREATE TABLE a AS\nSELECT 1", start_line=1, end_line=2),
        Statement(text="SELECT 2", start_line=4, end_line=4),
    ]


def test_a_semicolon_inside_a_string_literal_does_not_split():
    statements = split_statements("SELECT 'a;b' AS X; SELECT 2;")
    assert [s.text for s in statements] == ["SELECT 'a;b' AS X", "SELECT 2"]


def test_a_doubled_quote_and_a_backslash_escape_stay_inside_their_string():
    statements = split_statements("SELECT 'it''s;here' AS A, 'x\\';y' AS B; SELECT 2;")
    assert [s.text for s in statements] == ["SELECT 'it''s;here' AS A, 'x\\';y' AS B", "SELECT 2"]


def test_a_semicolon_inside_a_double_quoted_identifier_does_not_split():
    statements = split_statements('SELECT 1 AS "A;B"; SELECT 2;')
    assert [s.text for s in statements] == ['SELECT 1 AS "A;B"', "SELECT 2"]


def test_a_semicolon_inside_a_line_comment_does_not_split_and_a_quote_in_one_does_not_open_a_string():
    text = sql("""
        -- it's a comment; with a semicolon
        SELECT 1; -- trailing; note
        SELECT 2;
    """)
    statements = split_statements(text)
    assert [s.text.splitlines()[-1] for s in statements] == ["SELECT 1", "SELECT 2"]
    assert [(s.start_line, s.end_line) for s in statements] == [(2, 2), (3, 3)]


def test_a_semicolon_inside_a_block_comment_does_not_split_even_across_lines():
    text = sql("""
        SELECT 1 /* one;
        two; */ + 1;
        SELECT 2;
    """)
    statements = split_statements(text)
    assert len(statements) == 2
    assert (statements[0].start_line, statements[0].end_line) == (1, 2)
    assert (statements[1].start_line, statements[1].end_line) == (3, 3)


def test_a_dollar_quoted_body_does_not_split_and_its_apostrophes_do_not_open_strings():
    text = sql("""
        SELECT $$ it's; two;
        three $$ AS X;
        SELECT 2;
    """)
    statements = split_statements(text)
    assert [s.text for s in statements] == ["SELECT $$ it's; two;\nthree $$ AS X", "SELECT 2"]
    assert (statements[0].start_line, statements[0].end_line) == (1, 2)
    assert statements[1].start_line == 3


def test_line_numbers_skip_leading_comments_and_blank_lines_and_the_text_keeps_the_comments():
    text = sql("""
        -- wf_0001 header

        -- step s1
        CREATE TABLE a AS
        SELECT 1
        FROM t
        ;

        -- step s2
        /* block
           comment */
        SELECT 2;
    """)
    first, second = split_statements(text)
    assert (first.start_line, first.end_line) == (4, 7)
    assert first.text.startswith("-- wf_0001 header") and "-- step s1" in first.text
    assert first.text.endswith("FROM t")
    assert (second.start_line, second.end_line) == (12, 12)
    assert second.text.startswith("-- step s2") and second.text.endswith("SELECT 2")


def test_a_trailing_statement_without_a_semicolon_is_returned_and_ends_on_its_last_code_line():
    text = sql("""
        SELECT 1;
        SELECT 2
        FROM t
        -- the end
    """)
    last = split_statements(text)[-1]
    assert last.text == "SELECT 2\nFROM t"
    assert (last.start_line, last.end_line) == (2, 3)


def test_empty_statements_and_comment_only_tails_are_not_statements():
    assert split_statements("") == []
    assert split_statements("  \n-- only a comment\n") == []
    assert [s.text for s in split_statements(";; SELECT 1;; ;\n-- done\n")] == ["SELECT 1"]


def test_windows_line_endings_count_lines_the_same_way():
    statements = split_statements("SELECT 1;\r\n\r\nSELECT 2\r\nFROM t;\r\n")
    assert [(s.start_line, s.end_line) for s in statements] == [(1, 1), (3, 4)]


def test_an_unterminated_string_runs_to_the_end_and_is_one_statement():
    statements = split_statements("SELECT 1; SELECT 'open; still open")
    assert [s.text for s in statements] == ["SELECT 1", "SELECT 'open; still open"]


# --- placeholders and render ------------------------------------------------------------------

def test_placeholders_lists_every_name_with_its_line_and_allows_spaces_inside_the_braces():
    text = sql("""
        -- {{src}} is the source
        SELECT * FROM {{ src }}.ORDERS
        JOIN {{tgt}}.X ON 1 = 1 AND '{{  tgt  }}' = 'y';
    """)
    assert placeholders(text) == [("src", 1), ("src", 2), ("tgt", 3), ("tgt", 3)]


def test_placeholders_reports_unknown_names_too():
    assert placeholders("SELECT 1 FROM {{ nope }}.A, {{SRC}}.B") == [("nope", 1), ("SRC", 1)]


def test_placeholders_in_a_text_without_any_is_empty():
    assert placeholders("SELECT {1, 2} FROM a;") == []


def test_render_substitutes_both_names_with_and_without_inner_spaces():
    text = "FROM {{src}}.A JOIN {{ tgt }}.B -- {{ src }}"
    assert render(text, {"src": "DB.SRC", "tgt": "DB.TGT"}) == "FROM DB.SRC.A JOIN DB.TGT.B -- DB.SRC"


def test_render_ignores_a_value_the_text_does_not_use():
    assert render("SELECT 1", {"src": "A", "tgt": "B"}) == "SELECT 1"
    assert render("FROM {{src}}.A", {"src": "X", "tgt": "Y", "other": "Z"}) == "FROM X.A"


def test_render_names_a_missing_value():
    with pytest.raises(ValueError, match=r"tgt.*line 2"):
        render("FROM {{src}}.A\nJOIN {{tgt}}.B", {"src": "X"})


def test_render_names_an_unknown_placeholder_and_its_line():
    with pytest.raises(ValueError, match=r"foo.*line 3"):
        render("SELECT 1;\n\nSELECT {{ foo }};", {"src": "X", "tgt": "Y"})


def test_an_unknown_name_is_refused_even_when_the_caller_supplied_a_value_for_it():
    with pytest.raises(ValueError, match="foo"):
        render("SELECT {{foo}}", {"foo": "bar"})


def test_render_refuses_a_brace_pair_that_never_closes_instead_of_leaving_it_behind():
    with pytest.raises(ValueError, match=r"line 2"):
        render("SELECT 1;\nSELECT {{src.A;", {"src": "X", "tgt": "Y"})


def test_rendered_text_never_holds_a_placeholder():
    rendered = render(sql("""
        -- {{src}} and {{tgt}}
        CREATE OR REPLACE TABLE {{ tgt }}.T AS SELECT * FROM {{ src }}.S;
    """), {"src": "S1", "tgt": "T1"})
    assert "{{" not in rendered and "}}" not in rendered


# --- step_regions -----------------------------------------------------------------------------

def test_a_single_step_region_runs_from_its_first_code_line_to_the_last_before_the_next_marker():
    text = sql("""
        -- header
        -- step s1
        CREATE TABLE a AS
        SELECT 1;

        -- step s2
        SELECT 2;
    """)
    assert step_regions(text) == [
        Region(steps=("s1",), marker_line=2, start_line=3, end_line=4),
        Region(steps=("s2",), marker_line=6, start_line=7, end_line=7),
    ]


def test_one_marker_can_name_several_steps():
    text = sql("""
        -- step s1, s3
        SELECT 1;
        -- step s2 ,s10
        SELECT 2;
        --step s4
        SELECT 4;
    """)
    assert [(r.steps, r.start_line, r.end_line) for r in step_regions(text)] == [
        (("s1", "s3"), 2, 2), (("s2", "s10"), 4, 4), (("s4",), 6, 6)]


def test_trailing_blank_and_comment_only_lines_are_trimmed_and_leading_ones_are_skipped():
    text = sql("""
        -- step s1

        -- a note on the statement below
        /* block
           note */
        SELECT 1
        FROM t;

        -- a note about nothing
        /* another */


        -- step s2
        SELECT 2;
    """)
    first, second = step_regions(text)
    assert (first.marker_line, first.start_line, first.end_line) == (1, 6, 7)
    assert (second.marker_line, second.start_line, second.end_line) == (13, 14, 14)


def test_the_last_region_runs_to_the_end_of_the_file_minus_trailing_comments():
    text = "-- step s1\nSELECT 1;\n-- done\n"
    assert step_regions(text) == [Region(steps=("s1",), marker_line=1, start_line=2, end_line=2)]


def test_code_before_the_first_marker_belongs_to_no_region():
    text = "ALTER SESSION SET TIMEZONE = 'UTC';\n-- step s1\nSELECT 1;\n"
    assert [r.steps for r in step_regions(text)] == [("s1",)]
    assert step_regions(text)[0].start_line == 3


def test_a_marker_followed_directly_by_another_marker_has_no_region():
    text = sql("""
        -- step s1
        -- step s2
        SELECT 1;
    """)
    assert step_regions(text) == [Region(steps=("s2",), marker_line=2, start_line=3, end_line=3)]


def test_the_same_step_may_have_two_regions_and_a_repeated_id_in_one_marker_is_listed_once():
    text = "-- step s1, s1\nSELECT 1;\n-- step s2\nSELECT 2;\n-- step s1\nSELECT 3;\n"
    assert [(r.steps, r.start_line) for r in step_regions(text)] == [
        (("s1",), 2), (("s2",), 4), (("s1",), 6)]


def test_a_marker_inside_a_string_literal_is_not_a_marker():
    text = sql("""
        -- step s1
        SELECT 'first
        -- step s9
        last' AS X;
    """)
    assert step_regions(text) == [Region(steps=("s1",), marker_line=1, start_line=2, end_line=4)]


def test_a_marker_inside_a_block_comment_or_a_dollar_body_or_after_code_is_not_a_marker():
    text = sql("""
        -- step s1
        /*
        -- step s8
        */
        SELECT $$
        -- step s7
        $$; -- step s6
        SELECT 2;
    """)
    assert step_regions(text) == [Region(steps=("s1",), marker_line=1, start_line=5, end_line=8)]


def test_an_indented_marker_counts():
    assert step_regions("    -- step s1\n    SELECT 1;\n")[0].steps == ("s1",)


@pytest.mark.parametrize("comment_text", [
    "-- step S1", "-- step s0", "-- step s01", "-- step 1", "-- step", "-- step s1,", "-- step s1 s2",
    "-- steps s1", "-- step s1: read orders", "-- Step s1", "# step s1", "-- step s1 -- note"])
def test_a_comment_that_is_not_exactly_a_marker_is_not_a_marker(comment_text):
    assert step_regions(f"{comment_text}\nSELECT 1;\n") == []


def test_python_markers_use_hash_comments():
    text = sql('''
        import snowflake.snowpark as sp

        # step s1
        def main(session, src, tgt):
            df = session.table(f"{src}.ORDERS")
            note = """
        # step s9
            """

            # step s2, s3
            df.write.save_as_table(f"{tgt}.OUT", mode="overwrite")
            return "OK"
    ''')
    assert step_regions(text, comment="#") == [
        Region(steps=("s1",), marker_line=3, start_line=4, end_line=8),      # the closing """ is code
        Region(steps=("s2", "s3"), marker_line=10, start_line=11, end_line=12),
    ]


def test_python_scanning_ignores_dash_dash_and_sql_scanning_ignores_hash():
    assert step_regions("-- step s1\nx = 1\n", comment="#") == []
    assert step_regions("# step s1\nSELECT 1;\n", comment="--") == []


def test_python_string_with_an_escaped_quote_does_not_hide_a_following_marker():
    text = "x = 'it\\'s'\n# step s1\ny = 2\n"
    assert step_regions(text, comment="#") == [Region(steps=("s1",), marker_line=2, start_line=3, end_line=3)]


def test_an_unknown_comment_style_is_refused_with_the_expected_values():
    with pytest.raises(ValueError, match=r"'--'.*'#'"):
        step_regions("SELECT 1;", comment="//")


def test_a_file_with_no_markers_has_no_regions():
    assert step_regions("SELECT 1;\n") == []
    assert step_regions("") == []


# --- session_settings -------------------------------------------------------------------------

def _statement(text: str) -> Statement:
    (statement,) = split_statements(text)
    return statement


def test_session_settings_reads_quoted_and_bare_values_with_upper_cased_names():
    statement = _statement("ALTER SESSION SET TIMEZONE = 'America/New_York', week_start = 1;")
    assert session_settings(statement) == {"TIMEZONE": "America/New_York", "WEEK_START": "1"}


def test_session_settings_ignores_leading_comments_case_and_line_breaks():
    statement = _statement("-- step s0\nalter session set\n  TIMEZONE = 'UTC',\n  TIMESTAMP_TYPE_MAPPING = TIMESTAMP_NTZ;")
    assert session_settings(statement) == {"TIMEZONE": "UTC", "TIMESTAMP_TYPE_MAPPING": "TIMESTAMP_NTZ"}


def test_session_settings_keeps_a_comma_inside_a_value_and_unescapes_doubled_quotes():
    statement = _statement("ALTER SESSION SET QUERY_TAG = 'a, b', X = 'it''s';")
    assert session_settings(statement) == {"QUERY_TAG": "a, b", "X": "it's"}


@pytest.mark.parametrize("text", [
    "SELECT 1", "ALTER SESSION UNSET TIMEZONE", "ALTER SESSION SET TIMEZONE", "ALTER SESSION SET = 'x'",
    "ALTER SESSION SET A = 'x',", "ALTER TABLE t SET A = 1", "CREATE TABLE t AS SELECT 1"])
def test_session_settings_is_none_for_anything_but_the_set_form(text):
    assert session_settings(Statement(text=text, start_line=1, end_line=1)) is None


# --- masked_code ------------------------------------------------------------------------------

def test_masked_code_blanks_comments_and_strings_but_keeps_offsets_and_quoted_identifiers():
    text = "SELECT 'a;b' AS \"Q\" -- note\nFROM /* c */ t"
    masked = masked_code(text)
    assert len(masked) == len(text)
    assert masked == ("SELECT " + " " * 5 + ' AS "Q" ' + " " * 7 + "\nFROM " + " " * 7 + " t")


def test_masked_code_can_keep_the_strings():
    assert masked_code("SELECT 'x' -- c", strings=False) == "SELECT 'x' " + " " * 4


# --- the design's example file, end to end ----------------------------------------------------

DESIGN_EXAMPLE = sql("""
    -- wf_0001 · Sales summary by region and size band
    -- Written from objective.md; checked against golden data. {{src}} = source schema, {{tgt}} = target schema.
    ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;

    -- step s1, s2
    CREATE OR REPLACE TEMPORARY TABLE orders AS
    SELECT LEFT(CUSTOMER, 10) AS CUSTOMER, STATUS AS ORDER_STATUS, ORDER_ID, REGION, AMOUNT_TXT, QTY, ORDER_DATE,
           TRY_TO_DOUBLE(AMOUNT_TXT) AS AMOUNT
    FROM {{src}}.ORDERS;

    -- step s3
    CREATE OR REPLACE TABLE {{tgt}}.SALES_SUMMARY AS
    SELECT REGION, SUM(AMOUNT) AS TOTAL_NET
    FROM orders
    WHERE REGION <> 'WEST'
    GROUP BY REGION, SIZE_BAND
    ORDER BY TOTAL_NET DESC, REGION;

    -- step s4
    CREATE OR REPLACE TABLE {{tgt}}.EXCLUDED_ORDERS AS
    SELECT CUSTOMER, ORDER_STATUS, ORDER_ID, REGION, AMOUNT_TXT, QTY, ORDER_DATE
    FROM orders
    WHERE NOT (REGION <> 'WEST') OR REGION IS NULL;
""")


def test_the_design_example_splits_into_its_four_statements_with_exact_lines():
    statements = split_statements(DESIGN_EXAMPLE)
    assert [(s.start_line, s.end_line) for s in statements] == [(3, 3), (6, 9), (12, 17), (20, 23)]
    assert [s.text.split("\n")[-1].strip() for s in statements] == [
        "ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1", "FROM {{src}}.ORDERS",
        "ORDER BY TOTAL_NET DESC, REGION", "WHERE NOT (REGION <> 'WEST') OR REGION IS NULL"]


def test_the_design_example_has_the_regions_its_markers_describe():
    assert step_regions(DESIGN_EXAMPLE) == [
        Region(steps=("s1", "s2"), marker_line=5, start_line=6, end_line=9),
        Region(steps=("s3",), marker_line=11, start_line=12, end_line=17),
        Region(steps=("s4",), marker_line=19, start_line=20, end_line=23),
    ]


def test_the_design_example_reads_its_session_settings_and_renders_every_placeholder():
    first = split_statements(DESIGN_EXAMPLE)[0]
    assert session_settings(first) == {"TIMEZONE": "America/New_York", "WEEK_START": "1"}
    assert placeholders(DESIGN_EXAMPLE) == [("src", 2), ("tgt", 2), ("src", 9), ("tgt", 12), ("tgt", 20)]
    rendered = render(DESIGN_EXAMPLE, {"src": "MIGDB.MIG_SRC_WF0001", "tgt": "MIGDB.MIG_WORK"})
    assert "{{" not in rendered and "FROM MIGDB.MIG_SRC_WF0001.ORDERS;" in rendered
    # Rendering changes no line count, so the regions found before are still where the code is.
    assert [(s.start_line, s.end_line) for s in split_statements(rendered)] == [(3, 3), (6, 9), (12, 17), (20, 23)]


def test_a_large_file_is_read_in_linear_time():
    """Not a benchmark: a generous bound that an accidentally quadratic scan (8,000 lines here) would break."""
    big = "\n".join(f"-- step s{n}\nSELECT {n} AS A, 'x;y' AS B FROM T{n};" for n in range(1, 4001))
    started = time.perf_counter()
    assert len(split_statements(big)) == 4000
    assert len(step_regions(big)) == 4000
    assert len(placeholders(big)) == 0
    assert time.perf_counter() - started < 5.0


# --- the placeholders --------------------------------------------------------------------------

def test_placeholder_names_are_the_two_the_design_names():
    assert build_sql.PLACEHOLDER_NAMES == ("src", "tgt")
