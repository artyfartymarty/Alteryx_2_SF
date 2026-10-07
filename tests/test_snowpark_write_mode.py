"""The Snowpark twin of `compile_check.py`'s `write_mode` check: `lib.snowpark_rules`' `rule:write_mode`.

A `build.py` writes a final target with ONE call, and the objective's write mode picks it:
overwrite -> `.write.mode("overwrite").save_as_table(f"{tgt}.<LOGICAL>")`; append -> `.mode("append")`;
truncate_append -> `.mode("truncate")` (snowflake-snowpark-python 1.55's own "truncate the table, then append"
mode); update_insert (intake's merge) -> `session.table(<target>).merge(<source>, <target>["K"] == <source>["K"]
& …, [when_matched().update(…), when_not_matched().insert(…)])` on exactly the objective's keys. A
`.update(…)`/`.delete(…)` of the target is the Output tool's PreSQL (before the write) or PostSQL (after it) and
needs the tool to have one. Checked on the AST; nothing here runs a Snowpark session, and nothing has run on
Snowflake.
"""
from __future__ import annotations

import re

import pytest

from lib import snowpark_rules as rules
from lib.write_modes import ContractError

TARGET = 'f"{tgt}.REVENUE"'

HEAD = '''from snowflake.snowpark.functions import col, lit, when_matched, when_not_matched
from snowflake.snowpark.types import StructType, StructField, StringType


def main(session, src: str, tgt: str) -> str:
    pdf = session.table(f"{src}.SUBSCRIPTIONS").to_pandas()
    out = session.create_dataframe(pdf, schema=StructType([StructField("CUSTOMER", StringType(20))]))
'''
TAIL = '    return "OK"\n'

WRITES = {
    "overwrite": f'    out.write.mode("overwrite").save_as_table({TARGET})\n',
    "append": f'    out.write.mode("append").save_as_table({TARGET})\n',
    "truncate": f'    out.write.mode("truncate").save_as_table({TARGET})\n',
    "no_mode": f'    out.write.save_as_table({TARGET})\n',
    "camel_overwrite": f'    out.write.mode("overwrite").saveAsTable({TARGET})\n',
    "merge": (f'    target = session.table({TARGET})\n'
              '    target.merge(out, (target["CUSTOMER"] == out["CUSTOMER"]),\n'
              '                 [when_matched().update({"CUSTOMER": out["CUSTOMER"]}),\n'
              '                  when_not_matched().insert({"CUSTOMER": out["CUSTOMER"]})])\n'),
    "merge_chained": (f'    session.table({TARGET}).merge(out, session.table({TARGET})["CUSTOMER"] == out["CUSTOMER"],\n'
                      '        [when_matched().update({"CUSTOMER": out["CUSTOMER"]}),\n'
                      '         when_not_matched().insert({"CUSTOMER": out["CUSTOMER"]})])\n'),
    "update": (f'    fix = session.table({TARGET})\n'
               '    fix.update({"CUSTOMER": lit("X")}, col("CUSTOMER").is_null())\n'),
    "delete": (f'    old = session.table({TARGET})\n'
               '    old.delete(col("CUSTOMER").is_null())\n'),
}

REQUIRED = {
    "overwrite": '.write.mode("overwrite").save_as_table(f"{tgt}.REVENUE")',
    "append": '.write.mode("append").save_as_table(f"{tgt}.REVENUE")',
    "truncate_append": '.write.mode("truncate").save_as_table(f"{tgt}.REVENUE")',
    "update_insert": ('session.table(f"{tgt}.REVENUE").merge(<source>, <target>["CUSTOMER"] == '
                      '<source>["CUSTOMER"], [when_matched().update({…}), when_not_matched().insert({…})])'),
}


def module(*writes: str) -> str:
    return HEAD + "".join(WRITES.get(write, write) for write in writes) + TAIL


def objective(write_mode: str | None = "overwrite", keys: list[str] | None = None,
              pre_sql: str | None = None, post_sql: str | None = None) -> dict:
    """An objective with one input and one output, REVENUE, written in `write_mode`."""
    return {"workflow": "wf_0006",
            "inputs": [{"tool_id": "1", "logical": "SUBSCRIPTIONS", "columns": [], "keys": []}],
            "outputs": [{"tool_id": "5", "logical": "REVENUE", "write_mode": write_mode,
                         "keys": ["CUSTOMER"] if keys is None else keys, "pre_sql": pre_sql, "post_sql": post_sql,
                         "columns": [{"name": "CUSTOMER", "type": "VARCHAR(20)", "nullable": True}]}]}


def write_mode_errors(source: str, doc: dict) -> list[str]:
    """The `rule:write_mode` errors, each without its `line N:` (which these cases do not pin)."""
    return [re.sub(r"^(rule:write_mode): line \d+: ", r"\1: ", error)
            for error in rules.check_build_py(source, doc) if error.startswith("rule:write_mode")]


@pytest.mark.parametrize("mode, write", [
    ("overwrite", "overwrite"), ("overwrite", "camel_overwrite"), ("append", "append"),
    ("truncate_append", "truncate"), ("update_insert", "merge"), ("merge", "merge"),
    ("update_insert", "merge_chained"),
])
def test_each_write_mode_in_its_own_form_passes(mode, write):
    assert rules.check_build_py(module(write), objective(mode)) == []


@pytest.mark.parametrize("mode, write, found", [
    ("overwrite", "append", '.mode("append").save_as_table'),
    ("overwrite", "no_mode", ".save_as_table with no .mode(…)"),
    ("overwrite", "merge", ".merge"),
    ("append", "overwrite", '.mode("overwrite").save_as_table'),
    ("append", "truncate", '.mode("truncate").save_as_table'),
    ("truncate_append", "append", '.mode("append").save_as_table'),
    ("update_insert", "overwrite", '.mode("overwrite").save_as_table'),
    ("update_insert", "append", '.mode("append").save_as_table'),
])
def test_a_write_in_another_modes_form_is_refused(mode, write, found):
    assert write_mode_errors(module(write), objective(mode)) == [
        f"rule:write_mode: REVENUE is {mode}: write it with {REQUIRED[mode]}, not {found}"]


def test_a_refused_write_names_the_line_of_the_call():
    errors = rules.check_build_py(module("append"), objective("overwrite"))
    line = next(number for number, text in enumerate(module("append").splitlines(), 1) if '.mode("append")' in text)
    assert [e for e in errors if e.startswith("rule:write_mode")][0].startswith(f"rule:write_mode: line {line}: REVENUE is overwrite")


def test_a_target_the_module_never_writes_is_refused():
    assert write_mode_errors(module(), objective("overwrite")) == [
        f"rule:write_mode: REVENUE is overwrite: write it with {REQUIRED['overwrite']}; the module never writes "
        f'f"{{tgt}}.REVENUE"']


def test_a_target_written_twice_is_refused():
    assert write_mode_errors(module("overwrite", "overwrite"), objective("overwrite")) == [
        f"rule:write_mode: REVENUE is overwrite: write it with {REQUIRED['overwrite']} once, not "
        '.mode("overwrite").save_as_table + .mode("overwrite").save_as_table']


@pytest.mark.parametrize("join, keys", [
    ('(target["CUSTOMER"] == out["CUSTOMER"]) & (target["PERIOD"] == out["PERIOD"])', ["CUSTOMER"]),
    ('target["CUSTOMER"] == out["CUSTOMER"]', ["CUSTOMER", "PERIOD"]),
    ('target["CUSTOMER"] == target["CUSTOMER"]', ["CUSTOMER"]),
    ('(target["CUSTOMER"] == out["CUSTOMER"]) | target["CUSTOMER"].is_null()', ["CUSTOMER"]),
    ('target["CUSTOMER"].equal_null(out["CUSTOMER"])', ["CUSTOMER"]),
])
def test_a_merge_not_on_exactly_the_objectives_keys_is_refused(join, keys):
    source = module(WRITES["merge"].replace('(target["CUSTOMER"] == out["CUSTOMER"])', join))
    errors = write_mode_errors(source, objective("update_insert", keys=keys))
    assert len(errors) == 1 and errors[0].startswith(
        "rule:write_mode: REVENUE is update_insert: the merge into f\"{tgt}.REVENUE\" must join on "
        f"exactly the objective's keys {', '.join(keys)}"), errors


def test_a_merge_on_col_references_and_two_keys_in_any_order_passes():
    source = module(WRITES["merge"].replace(
        '(target["CUSTOMER"] == out["CUSTOMER"])',
        '(out.col("PERIOD") == target.col("PERIOD")) & (target["CUSTOMER"] == out["CUSTOMER"])'))
    assert rules.check_build_py(source, objective("update_insert", keys=["CUSTOMER", "PERIOD"])) == []


def test_a_merge_without_both_clauses_is_refused():
    source = module(WRITES["merge"].replace('[when_matched().update({"CUSTOMER": out["CUSTOMER"]}),\n', "[\n"))
    assert write_mode_errors(source, objective("update_insert")) == [
        'rule:write_mode: REVENUE is update_insert: the merge into f"{tgt}.REVENUE" needs both '
        "when_matched().update(…) and when_not_matched().insert(…) (Update; Insert if new)"]


def test_presql_and_postsql_are_allowed_only_when_the_output_tool_has_them():
    source = module("delete", "merge", "update")
    assert write_mode_errors(source, objective("update_insert", pre_sql="DELETE …", post_sql="UPDATE …")) == []
    errors = write_mode_errors(source, objective("update_insert"))
    assert errors == [
        f"rule:write_mode: REVENUE is update_insert: write it with {REQUIRED['update_insert']} alone, not "
        ".delete + .merge + .update (a call before the write is allowed only as tool 5's PreSQL, and it has none; "
        "a call after the write is allowed only as tool 5's PostSQL, and it has none)"]


@pytest.mark.parametrize("write_mode", [None, "update_only"])
def test_an_output_without_a_known_write_mode_is_a_contract_error_not_a_rule_error(write_mode):
    """Nothing in the module can fix an objective that does not say how an output is written: a usage error."""
    with pytest.raises(ContractError, match="REVENUE"):
        rules.check_build_py(module("overwrite"), objective(write_mode))


# --- fix round 1 (I2): the merge's keys compare case-insensitively on both sides ------------------

@pytest.mark.parametrize("join, key", [
    ('target["\\"Customer ID\\""] == out["\\"customer id\\""]', "Customer ID"),
    ('target["acctid"] == out["ACCTID"]', "AcctId"),
    ('target["\\"AcctId\\""] == out.col("ACCTID")', "AcctId"),
])
def test_a_merge_key_matches_the_objective_in_any_case(join, key):
    source = module(WRITES["merge"].replace('(target["CUSTOMER"] == out["CUSTOMER"])', join))
    assert write_mode_errors(source, objective("update_insert", keys=[key])) == []
