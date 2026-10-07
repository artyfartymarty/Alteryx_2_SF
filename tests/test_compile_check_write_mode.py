"""`compile_check.py`'s `write_mode` and `write_mode:pre_post` checks (Task L4, moved from the procedure form
to the build file): every output is written with exactly the statement form its write mode requires.

The live probe of wf_0001 (a real model through the real SDK) wrote an `overwrite` target as `TRUNCATE TABLE`
+ `INSERT INTO ...`. It compiled, and then validation failed every golden set with a catalog error that named
no rule. The forms (`cookbook/output.md`, "Config fields that change the pattern"):

- overwrite -> `CREATE OR REPLACE TABLE {{tgt}}.X AS ...`, nothing else on it;
- append -> `INSERT INTO {{tgt}}.X ...`, nothing else on it;
- truncate_append -> a `TRUNCATE`/`DELETE FROM` of it (no WHERE) before one `INSERT INTO` it;
- update_insert (intake's `merge`) -> one `MERGE INTO {{tgt}}.X` on the objective's keys, with `WHEN MATCHED
  THEN UPDATE` and `WHEN NOT MATCHED THEN INSERT`;
- the Output tool's PreSQL/PostSQL (the objective's `pre_sql` / `post_sql`) are separate statements
  before/after the write, and only an output that has one may have them, and one that has must.

Nothing here has run on Snowflake: the forms are checked in the text, and the run is the local DuckDB double.
"""
from __future__ import annotations

import re

import pytest

from tests.build_fixtures import check, dbt_off, named, objective

HEAD = """-- step s1
CREATE OR REPLACE TEMPORARY TABLE kept AS
SELECT ID, NOTE FROM {{src}}.ITEMS WHERE NOTE IS NOT NULL;

-- step s2
"""
QUERY = "SELECT ID, NOTE FROM kept"
T = "{{tgt}}.ITEMS_OUT"

#: One statement per form, each on the one target this workflow has.
FORMS = {
    "ctas": f"CREATE OR REPLACE TABLE {T} AS\n{QUERY};",
    "transient": f"CREATE OR REPLACE TRANSIENT TABLE {T} AS\n{QUERY};",
    "insert": f"INSERT INTO {T} (ID, NOTE)\n{QUERY};",
    "with_insert": f"WITH shaped AS ({QUERY})\nINSERT INTO {T} (ID, NOTE) SELECT ID, NOTE FROM shaped;",
    "truncate": f"TRUNCATE TABLE {T};",
    "delete": f"DELETE FROM {T};",
    "delete_where": f"DELETE FROM {T} WHERE ID < 0;",
    "update": f"UPDATE {T} SET NOTE = 'x' WHERE NOTE IS NULL;",
    "merge": (f"MERGE INTO {T} AS T\n"
              f"USING ({QUERY}) AS S\n"
              "ON T.ID = S.ID\n"
              "WHEN MATCHED THEN UPDATE SET NOTE = S.NOTE\n"
              "WHEN NOT MATCHED THEN INSERT (ID, NOTE) VALUES (S.ID, S.NOTE);"),
}


def sql(*forms: str) -> str:
    return HEAD + "\n".join(FORMS.get(form, form) for form in forms) + "\n"


def plain(error: str) -> str:
    """The error without its line numbers: the tests below say what is wrong, and
    `test_the_lines_are_the_lines_of_the_statements_concerned` says where."""
    return re.sub(r" \(line \d+\)", "", re.sub(r"^(\S+): line \d+: ", r"\1: ", error))


def errors_of(tmp_path, build: str, name: str = "write_mode", **kwargs) -> list[str]:
    return [plain(error) for error in named(check(tmp_path, build, objective(**kwargs)), name)]


def one(tmp_path, build: str, **kwargs) -> str:
    errors = errors_of(tmp_path, build, **kwargs)
    assert len(errors) == 1, errors
    return errors[0]


def compiles(tmp_path, build: str, **kwargs) -> dict:
    return check(tmp_path, build, objective(**kwargs))


# --- overwrite ---------------------------------------------------------------------------------------

def test_overwrite_written_as_create_or_replace_table_as_compiles(tmp_path):
    report = compiles(tmp_path, sql("ctas"), write_mode="overwrite")
    assert report["ok"] is True, report["errors"]


def test_the_live_shape_truncate_plus_insert_for_an_overwrite_target_is_refused_with_the_rule(tmp_path):
    error = one(tmp_path, sql("truncate", "with_insert"), write_mode="overwrite")
    assert error.startswith("write_mode: ITEMS_OUT is overwrite: write it with CREATE OR REPLACE TABLE "
                            "{{tgt}}.ITEMS_OUT AS ...; found TRUNCATE + INSERT"), error


@pytest.mark.parametrize("forms, found", [
    (("insert",), "INSERT"),
    (("delete", "insert"), "DELETE + INSERT"),
    (("merge",), "MERGE"),
    (("transient",), "CREATE OR REPLACE TRANSIENT TABLE"),
])
def test_overwrite_in_any_other_form_is_refused(tmp_path, forms, found):
    error = one(tmp_path, sql(*forms), write_mode="overwrite")
    assert "ITEMS_OUT is overwrite: write it with CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS ...; found " in error
    assert error.endswith(found), error


@pytest.mark.parametrize("statement", [
    f"CREATE OR REPLACE TABLE {T} COPY GRANTS AS {QUERY};",
    f"CREATE OR REPLACE TABLE {T} (ID NUMBER(38,0), NOTE VARCHAR(20));",
    f"CREATE TABLE {T} AS {QUERY};",
])
def test_a_create_that_is_not_create_or_replace_table_as_says_so(tmp_path, statement):
    error = one(tmp_path, sql(statement), write_mode="overwrite")
    assert "found CREATE" in error and error.startswith("write_mode:"), error


def test_overwrite_with_a_second_write_after_it_is_refused_as_a_postsql_it_does_not_have(tmp_path):
    errors = errors_of(tmp_path, sql("ctas", "insert"), "write_mode:pre_post", write_mode="overwrite")
    assert len(errors) == 1 and "INSERT on {{tgt}}.ITEMS_OUT comes after its write" in errors[0], errors
    assert "tool 7 has no PostSQL" in errors[0] and errors[0].startswith("write_mode:pre_post:")


def test_overwrite_written_twice_is_refused(tmp_path):
    error = one(tmp_path, sql("ctas", "ctas"), write_mode="overwrite")
    assert "write it with CREATE OR REPLACE TABLE {{tgt}}.ITEMS_OUT AS ... once; found" in error
    assert error.startswith("write_mode:")


def test_a_target_the_file_never_writes_is_refused(tmp_path):
    build = sql(f"CREATE OR REPLACE TABLE {{{{tgt}}}}.OTHER AS {QUERY};")
    error = one(tmp_path, build, write_mode="overwrite")
    assert error == ("write_mode: ITEMS_OUT is overwrite: write it with CREATE OR REPLACE TABLE "
                     "{{tgt}}.ITEMS_OUT AS ...; the file never writes {{tgt}}.ITEMS_OUT")


# --- append ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("form", ["insert", "with_insert"])
def test_append_written_as_insert_into_compiles(tmp_path, form):
    if form == "with_insert":
        dbt_off(tmp_path)          # a WITH before the INSERT is valid SQL; only the dbt export has no form for it
    report = compiles(tmp_path, sql(form), write_mode="append")
    assert report["ok"] is True, report["errors"]


@pytest.mark.parametrize("forms, found", [
    (("ctas",), "CREATE OR REPLACE TABLE ... AS"),
    (("merge",), "MERGE"),
])
def test_append_in_any_other_form_is_refused(tmp_path, forms, found):
    error = one(tmp_path, sql(*forms), write_mode="append")
    assert "ITEMS_OUT is append: write it with INSERT INTO {{tgt}}.ITEMS_OUT (<columns>) SELECT ..." in error
    assert error.endswith(f"found {found}"), error


@pytest.mark.parametrize("forms", [("truncate", "insert"), ("delete", "insert")])
def test_a_clear_before_an_append_is_a_presql_the_output_does_not_have(tmp_path, forms):
    errors = errors_of(tmp_path, sql(*forms), "write_mode:pre_post", write_mode="append")
    assert len(errors) == 1 and "comes before its write, but tool 7 has no PreSQL" in errors[0], errors


# --- truncate_append ------------------------------------------------------------------------------------

@pytest.mark.parametrize("forms", [("truncate", "insert"), ("delete", "with_insert")])
def test_truncate_append_written_as_a_clear_then_insert_compiles(tmp_path, forms):
    if "with_insert" in forms:
        dbt_off(tmp_path)          # a WITH before the INSERT is valid SQL; only the dbt export has no form for it
    report = compiles(tmp_path, sql(*forms), write_mode="truncate_append")
    assert report["ok"] is True, report["errors"]


@pytest.mark.parametrize("forms, found", [
    (("insert",), "INSERT"),
    (("ctas",), "CREATE OR REPLACE TABLE ... AS"),
    (("insert", "truncate"), "INSERT + TRUNCATE"),
    (("delete_where", "insert"), "DELETE ... WHERE + INSERT"),
    (("merge",), "MERGE"),
])
def test_truncate_append_in_any_other_form_is_refused(tmp_path, forms, found):
    error = one(tmp_path, sql(*forms), write_mode="truncate_append")
    assert error.startswith("write_mode: ") and "ITEMS_OUT is truncate_append: write it with TRUNCATE TABLE " \
        "{{tgt}}.ITEMS_OUT (or DELETE FROM it, with no WHERE) followed by INSERT INTO {{tgt}}.ITEMS_OUT (<columns>) " \
        "SELECT ..." in error
    assert error.endswith(found), error


def test_nothing_may_come_between_the_clear_and_the_insert(tmp_path):
    error = one(tmp_path, sql("truncate", "update", "insert"), write_mode="truncate_append")
    assert "nothing may come between the TRUNCATE/DELETE and the INSERT" in error


# --- update_insert / merge --------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["update_insert", "merge"])
def test_update_insert_written_as_a_merge_on_the_keys_compiles(tmp_path, mode):
    report = compiles(tmp_path, sql("merge"), write_mode=mode)
    assert report["ok"] is True, report["errors"]


def test_a_merge_on_two_keys_in_either_order_and_quoted_compiles(tmp_path):
    build = sql(FORMS["merge"].replace("ON T.ID = S.ID", 'ON (S."NOTE" = T."NOTE") AND T.ID = S.ID'))
    report = compiles(tmp_path, build, write_mode="update_insert", keys=["NOTE", "ID"])
    assert report["ok"] is True, report["errors"]


@pytest.mark.parametrize("forms, found", [
    (("insert",), "INSERT"),
    (("update", "insert"), "UPDATE + INSERT"),
    (("ctas",), "CREATE OR REPLACE TABLE ... AS"),
])
def test_update_insert_in_any_other_form_is_refused(tmp_path, forms, found):
    error = one(tmp_path, sql(*forms), write_mode="update_insert")
    assert ("ITEMS_OUT is update_insert: write it with MERGE INTO {{tgt}}.ITEMS_OUT ... ON the objective's keys ID "
            "... WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...; found ") in error
    assert error.endswith(found), error


@pytest.mark.parametrize("on_clause, found", [
    ("ON T.ID = S.ID AND T.NOTE = S.NOTE", "T.ID = S.ID AND T.NOTE = S.NOTE"),   # a non-key narrows the match
    ("ON T.NOTE = S.NOTE", "T.NOTE = S.NOTE"),                                   # the wrong key
    ("ON EQUAL_NULL(T.ID, S.ID)", "EQUAL_NULL(T.ID, S.ID)"),                     # NULL keys would match
    ("ON T.ID = T.ID", "T.ID = T.ID"),                                           # a tautology
    ("ON T.ID = S.ID OR T.ID IS NULL", "T.ID = S.ID OR T.ID IS NULL"),
])
def test_a_merge_that_is_not_on_exactly_the_objectives_keys_is_refused(tmp_path, on_clause, found):
    error = one(tmp_path, sql(FORMS["merge"].replace("ON T.ID = S.ID", on_clause)), write_mode="update_insert")
    assert error == ("write_mode: ITEMS_OUT is update_insert: the MERGE INTO {{tgt}}.ITEMS_OUT must match "
                     "on exactly the objective's keys ID -- ON <target>.<KEY> = <source>.<KEY> for each key, joined "
                     f"by AND, nothing else -- not ON {found}")


def test_a_merge_without_both_clauses_is_refused(tmp_path):
    build = sql(FORMS["merge"].replace("WHEN MATCHED THEN UPDATE SET NOTE = S.NOTE\n", ""))
    error = one(tmp_path, build, write_mode="update_insert")
    assert error == ("write_mode: ITEMS_OUT is update_insert: the MERGE INTO {{tgt}}.ITEMS_OUT needs both "
                     "WHEN MATCHED THEN UPDATE and WHEN NOT MATCHED THEN INSERT (Update; Insert if new)")


@pytest.mark.parametrize("on_clause", [
    'ON T."ID" = S."ID"', 'ON "T"."ID" = "S"."ID"', "ON (T.ID = S.ID)", "ON T . ID = S . ID", "ON t.id = s.ID",
])
def test_a_merge_on_a_quoted_or_spaced_or_mixed_case_key_is_accepted(tmp_path, on_clause):
    build = sql(FORMS["merge"].replace("ON T.ID = S.ID", on_clause))
    assert errors_of(tmp_path, build, write_mode="update_insert") == []


def test_the_message_shows_a_key_that_needs_quoting_quoted(tmp_path):
    build = sql(FORMS["merge"].replace("ON T.ID = S.ID", "ON T.NOTE = S.NOTE"))
    error = one(tmp_path, build, write_mode="update_insert", keys=["Account Id"])
    assert '"Account Id"' in error


# --- PreSQL / PostSQL -------------------------------------------------------------------------------------------

PRE = "DELETE FROM {{tgt}}.ITEMS_OUT WHERE ID < 0;"
POST = "UPDATE {{tgt}}.ITEMS_OUT SET NOTE = 'x' WHERE NOTE IS NULL;"


def test_presql_and_postsql_statements_are_separate_and_allowed_when_the_output_has_them(tmp_path):
    report = compiles(tmp_path, sql(PRE, "merge", POST), write_mode="update_insert",
                      pre_sql="DELETE FROM dbo.ITEMS_OUT WHERE ID < 0",
                      post_sql="UPDATE dbo.ITEMS_OUT SET NOTE = 'x' WHERE NOTE IS NULL")
    assert report["ok"] is True, report["errors"]


def test_a_statement_before_the_write_is_refused_when_the_output_has_no_presql(tmp_path):
    errors = errors_of(tmp_path, sql(PRE, "merge", POST), "write_mode:pre_post", write_mode="update_insert",
                       post_sql="UPDATE dbo.ITEMS_OUT SET NOTE = 'x'")
    assert len(errors) == 1, errors
    assert errors[0].startswith("write_mode:pre_post: DELETE ... WHERE on {{tgt}}.ITEMS_OUT comes before "
                                "its write, but tool 7 has no PreSQL"), errors


def test_a_statement_after_the_write_is_refused_when_the_output_has_no_postsql(tmp_path):
    errors = errors_of(tmp_path, sql(PRE, "merge", POST), "write_mode:pre_post", write_mode="update_insert",
                       pre_sql="DELETE FROM dbo.ITEMS_OUT WHERE ID < 0")
    assert len(errors) == 1, errors
    assert "UPDATE on {{tgt}}.ITEMS_OUT comes after its write, but tool 7 has no PostSQL" in errors[0], errors


@pytest.mark.parametrize("forms, missing", [
    (("merge", POST), "tool 7 has a PreSQL: expected a statement on {{tgt}}.ITEMS_OUT before its write, and there is none"),
    ((PRE, "merge"), "tool 7 has a PostSQL: expected a statement on {{tgt}}.ITEMS_OUT after its write, and there is none"),
])
def test_a_presql_or_postsql_the_output_has_is_required_not_only_allowed(tmp_path, forms, missing):
    """A translation that drops the Output tool's PreSQL/PostSQL used to compile and lose it silently."""
    errors = errors_of(tmp_path, sql(*forms), "write_mode:pre_post", write_mode="update_insert",
                       pre_sql="DELETE FROM dbo.ITEMS_OUT WHERE ID < 0",
                       post_sql="UPDATE dbo.ITEMS_OUT SET NOTE = 'x' WHERE NOTE IS NULL")
    assert len(errors) == 1 and missing in errors[0], errors


def test_an_overwrite_target_with_a_postsql_update_compiles(tmp_path):
    report = compiles(tmp_path, sql("ctas", POST), write_mode="overwrite", post_sql="UPDATE dbo.ITEMS_OUT SET NOTE = 'x'")
    assert report["ok"] is True, report["errors"]


# --- odd spellings -------------------------------------------------------------------------------------------------

def test_a_delete_with_only_an_alias_is_a_whole_table_clear(tmp_path):
    report = compiles(tmp_path, sql(f"DELETE FROM {T} AS t;", "insert"), write_mode="truncate_append")
    assert named(report, "write_mode") == [], report["errors"]


def test_a_create_like_is_labelled_as_such(tmp_path):
    error = one(tmp_path, sql(f"CREATE OR REPLACE TABLE {T} LIKE {{{{tgt}}}}.ITEMS_OUT;"), write_mode="overwrite")
    assert "found CREATE OR REPLACE TABLE ... LIKE" in error


def test_a_write_in_a_comment_or_a_string_is_not_a_write(tmp_path):
    build = sql("ctas", "-- INSERT INTO {{tgt}}.ITEMS_OUT (ID) SELECT 1;\nSELECT 1 AS NOTE_X FROM kept LIMIT 0;")
    assert named(check(tmp_path, build, objective(write_mode="overwrite")), "write_mode") == []


def line_of(build: str, fragment: str) -> int:
    return next(number for number, text in enumerate(build.splitlines(), 1) if fragment in text)


def test_the_lines_are_the_lines_of_the_statements_concerned(tmp_path):
    build = sql("truncate", "update", "insert")
    errors = named(check(tmp_path, build, objective(write_mode="truncate_append")), "write_mode")
    assert errors[0].startswith(f"write_mode: line {line_of(build, 'UPDATE')}:"), errors
    build = sql(PRE, "merge", POST)
    errors = named(check(tmp_path / "b", build, objective(write_mode="update_insert", post_sql="x")), "write_mode:pre_post")
    assert errors[0].startswith(f"write_mode:pre_post: line {line_of(build, 'DELETE')}:"), errors
    build = sql(FORMS["merge"].replace("ON T.ID = S.ID", "ON T.NOTE = S.NOTE"))
    errors = named(check(tmp_path / "c", build, objective(write_mode="update_insert")), "write_mode")
    assert errors[0].startswith(f"write_mode: line {line_of(build, 'MERGE INTO')}:"), errors


# --- the columns an INSERT / MERGE names are held to the objective's (A3 review fix round 1) ---------------------------
#
# The run creates an append / truncate_append / update_insert target with the objective's columns beforehand, so it
# cannot see which columns the write names; the statement's own column list is compared instead.

OTHER = "CREATE OR REPLACE TEMPORARY TABLE spare AS SELECT 1 AS X;"


def columns_of(tmp_path, build: str, **kwargs) -> list[str]:
    return errors_of(tmp_path, build, "outputs:columns", **kwargs)


def line_of(build: str, fragment: str) -> int:
    return next(number for number, text in enumerate(build.splitlines(), 1) if fragment in text)


@pytest.mark.parametrize("mode, clear", [("append", ()), ("truncate_append", ("truncate",))])
@pytest.mark.parametrize("listed, problem", [
    ("(ID)", "missing NOTE"),
    ("(ID, NOTE, EXTRA)", "extra EXTRA"),
    ("(NOTE, ID)", "out of order"),
    ("(ID, ID, NOTE)", "repeated ID"),
    ("(ID, NOTE, ID, EXTRA)", "extra EXTRA"),
])
def test_an_insert_whose_column_list_is_not_the_objectives_is_named(tmp_path, mode, clear, listed, problem):
    build = sql(*clear, f"INSERT INTO {T} {listed}\n{QUERY};")
    errors = columns_of(tmp_path, build, write_mode=mode)
    assert len(errors) == 1, errors
    assert errors[0].startswith("outputs:columns: INSERT INTO {{tgt}}.ITEMS_OUT lists ") and problem in errors[0], errors
    assert "but the objective lists [ID, NOTE]" in errors[0] and "same names in the same order" in errors[0]


@pytest.mark.parametrize("mode, clear", [("append", ()), ("truncate_append", ("delete",))])
def test_an_insert_with_no_column_list_is_refused_and_the_columns_named(tmp_path, mode, clear):
    build = sql(*clear, f"INSERT INTO {T}\n{QUERY};")
    assert columns_of(tmp_path, build, write_mode=mode) == [
        "outputs:columns: INSERT INTO {{tgt}}.ITEMS_OUT has no column list; name the objective's columns [ID, NOTE], "
        "in that order"]


@pytest.mark.parametrize("mode, forms", [("append", ("insert",)), ("truncate_append", ("truncate", "insert")),
                                         ("update_insert", ("merge",)), ("overwrite", ("ctas",))])
def test_the_objectives_columns_in_its_order_pass_in_any_case(tmp_path, mode, forms):
    build = sql(*forms).replace("(ID, NOTE)", "(id, Note)")
    assert columns_of(tmp_path, build, write_mode=mode) == []


@pytest.mark.parametrize("listed, values, problem", [
    ("(ID)", "(S.ID)", "missing NOTE"),
    ("(ID, NOTE, EXTRA)", "(S.ID, S.NOTE, 1)", "extra EXTRA"),
    ("(NOTE, ID)", "(S.NOTE, S.ID)", "out of order"),
])
def test_a_merges_insert_column_list_is_held_to_the_objectives_too(tmp_path, listed, values, problem):
    build = sql(FORMS["merge"].replace("INSERT (ID, NOTE) VALUES (S.ID, S.NOTE)", f"INSERT {listed} VALUES {values}"))
    (error,) = columns_of(tmp_path, build, write_mode="update_insert")
    assert error.startswith("outputs:columns: the WHEN NOT MATCHED THEN INSERT of MERGE INTO {{tgt}}.ITEMS_OUT lists ")
    assert problem in error and "but the objective lists [ID, NOTE]" in error


def test_a_merges_insert_with_no_column_list_is_refused_and_the_columns_named(tmp_path):
    build = sql(FORMS["merge"].replace("INSERT (ID, NOTE) VALUES (S.ID, S.NOTE)", "INSERT VALUES (S.ID, S.NOTE)"))
    assert columns_of(tmp_path, build, write_mode="update_insert") == [
        "outputs:columns: the WHEN NOT MATCHED THEN INSERT of MERGE INTO {{tgt}}.ITEMS_OUT has no column list; name the "
        "objective's columns [ID, NOTE], in that order"]


def test_the_column_list_error_names_the_line_of_the_list(tmp_path):
    build = sql(f"INSERT INTO {T}\n  (ID)\n{QUERY};")
    (error,) = named(check(tmp_path, build, objective(write_mode="append")), "outputs:columns")
    assert error.startswith(f"outputs:columns: line {line_of(build, '(ID)')}:"), error


# --- PreSQL / PostSQL, and the clear of a truncate_append, are IMMEDIATELY next to the write -------------------------------

def test_a_presql_with_another_statement_between_it_and_the_write_is_refused(tmp_path):
    errors = errors_of(tmp_path, sql(PRE, OTHER, "merge", POST), "write_mode:pre_post", write_mode="update_insert",
                       pre_sql="DELETE FROM dbo.ITEMS_OUT WHERE ID < 0", post_sql="UPDATE dbo.ITEMS_OUT SET NOTE = 'x'")
    assert len(errors) == 1, errors
    assert ("DELETE ... WHERE on {{tgt}}.ITEMS_OUT is tool 7's PreSQL and must come immediately before its write, "
            "but another statement sits between them") in errors[0], errors


def test_a_postsql_with_another_statement_between_the_write_and_it_is_refused(tmp_path):
    errors = errors_of(tmp_path, sql(PRE, "merge", OTHER, POST), "write_mode:pre_post", write_mode="update_insert",
                       pre_sql="DELETE FROM dbo.ITEMS_OUT WHERE ID < 0", post_sql="UPDATE dbo.ITEMS_OUT SET NOTE = 'x'")
    assert len(errors) == 1, errors
    assert ("UPDATE on {{tgt}}.ITEMS_OUT is tool 7's PostSQL and must come immediately after its write, "
            "but another statement sits between them") in errors[0], errors


def test_the_statement_named_between_is_the_one_in_the_way(tmp_path):
    build = sql(PRE, OTHER, "merge", POST)
    (error,) = named(check(tmp_path, build, objective(write_mode="update_insert", pre_sql="x", post_sql="y")),
                     "write_mode:pre_post")
    assert f"another statement (line {line_of(build, 'TEMPORARY TABLE spare')})" in error
    assert error.startswith(f"write_mode:pre_post: line {line_of(build, 'DELETE')}:")


def test_presql_and_postsql_next_to_the_write_pass_even_with_two_of_each(tmp_path):
    pre2 = "DELETE FROM {{tgt}}.ITEMS_OUT WHERE NOTE = 'old';"
    post2 = "UPDATE {{tgt}}.ITEMS_OUT SET NOTE = 'y' WHERE NOTE = 'x';"
    report = compiles(tmp_path, sql(PRE, pre2, "merge", POST, post2), write_mode="update_insert",
                      pre_sql="DELETE FROM dbo.ITEMS_OUT; DELETE FROM dbo.ITEMS_OUT", post_sql="UPDATE a; UPDATE b")
    assert report["ok"] is True, report["errors"]


def test_a_gap_between_two_presql_statements_is_refused_too(tmp_path):
    pre2 = "DELETE FROM {{tgt}}.ITEMS_OUT WHERE NOTE = 'old';"
    errors = errors_of(tmp_path, sql(PRE, OTHER, pre2, "merge"), "write_mode:pre_post", write_mode="update_insert",
                       pre_sql="x")
    assert len(errors) == 1 and "PreSQL and must come immediately before its write" in errors[0], errors


def test_a_truncate_with_another_statement_before_its_insert_is_refused(tmp_path):
    build = sql("truncate", OTHER, "insert")
    (error,) = [e for e in check(tmp_path, build, objective(write_mode="truncate_append"))["errors"]
                if e.startswith("write_mode:")]
    assert ("the TRUNCATE must come immediately before the INSERT, but another statement (line "
            f"{line_of(build, 'TEMPORARY TABLE spare')}) sits between them") in error, error


def test_a_delete_clear_next_to_its_insert_passes_and_one_apart_is_refused(tmp_path):
    assert errors_of(tmp_path / "a", sql("delete", "insert"), write_mode="truncate_append") == []
    assert errors_of(tmp_path / "b", sql("delete", OTHER, "insert"), write_mode="truncate_append")
