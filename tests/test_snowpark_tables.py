"""Typed tables in and out of a local Snowpark session (`lib/snowpark_tables.py`): what `validate_build.py` and the
runtime sandbox load golden data with and read a `build.py`'s outputs back through.

A table crosses as a `typed_csv` table, and `table_from_snowpark` reads the REAL schema from Snowpark's own
`StructType` -- never a declaration -- so the type drift a parity test exists to catch is not coerced away.
Nothing here has run on Snowflake: the Snowpark side is the Local Testing Framework.
"""
from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from lib import snowpark_tables as st

FQN = "MIG_WORK.ROUND_TRIP"

FIELDS = [{"name": "N", "type": "Int64", "size": None, "scale": None},
          {"name": "D", "type": "FixedDecimal", "size": 19, "scale": 2},
          {"name": "F", "type": "Double", "size": None, "scale": None},
          {"name": "S", "type": "V_String", "size": 20, "scale": None},
          {"name": "B", "type": "Bool", "size": None, "scale": None},
          {"name": "DT", "type": "Date", "size": None, "scale": None},
          {"name": "TS", "type": "DateTime", "size": None, "scale": None}]

#: every column carries a NULL somewhere
ROWS = [[1, Decimal("10.25"), 1.5, "a", True, "2026-01-31", "2026-01-31 12:34:56"],
        [None, Decimal("3.10"), 2.25, "b", False, "2026-02-28", "2026-02-28 00:00:01"],
        [3, None, -0.5, "c", True, "2026-03-31", "2026-03-31 23:59:59"],
        [4, Decimal("7.00"), None, "d", False, "2026-04-30", "2026-04-30 08:00:00"],
        [5, Decimal("1.01"), 9.75, None, True, "2026-05-31", "2026-05-31 09:30:00"],
        [6, Decimal("2.02"), 0.125, "f", None, "2026-06-30", "2026-06-30 10:15:00"],
        [7, Decimal("3.03"), 4.5, "g", True, None, "2026-07-31 11:45:00"],
        [8, Decimal("4.04"), 5.5, "h", False, "2026-08-31", None]]


@pytest.fixture
def session():
    session = st.new_local_session()
    try:
        yield session
    finally:
        session.close()


def test_a_typed_table_round_trips_through_a_session_keeping_types_and_values(session):
    st.load_into_snowpark(session, FQN, {"fields": FIELDS, "rows": ROWS})
    back = st.table_from_snowpark(session, FQN)
    assert back is not None
    assert [(f["name"], f["type"], f["size"], f["scale"]) for f in back["fields"]] == [
        ("N", "Int64", None, None), ("D", "FixedDecimal", 19, 2), ("F", "Double", None, None),
        ("S", "V_String", None, None), ("B", "Bool", None, None), ("DT", "Date", None, None),
        ("TS", "DateTime", None, None)]
    assert back["rows"] == ROWS                    # a NULL stayed a NULL, never a zero or "nan"; dates are ISO text


def test_an_empty_table_keeps_its_columns(session):
    st.load_into_snowpark(session, FQN, {"fields": FIELDS, "rows": []})
    back = st.table_from_snowpark(session, FQN)
    assert back is not None and back["rows"] == [] and [f["name"] for f in back["fields"]] == [f["name"] for f in FIELDS]


def test_the_real_schema_wins_over_any_declaration(session):
    from snowflake.snowpark.types import StringType, StructField, StructType

    # A column every objective would call a number, written by the build as a string.
    session.create_dataframe([["1"], ["2"]], schema=StructType([StructField("AMOUNT", StringType())])) \
        .write.mode("overwrite").save_as_table("MIG_WORK.BUILD_OUT")
    table = st.table_from_snowpark(session, "MIG_WORK.BUILD_OUT")
    assert table["fields"][0]["type"] == "V_String"
    assert table["rows"] == [["1"], ["2"]]


def test_a_missing_table_reads_back_as_none(session):
    assert st.table_from_snowpark(session, "MIG_WORK.NOWHERE") is None


def test_no_helper_takes_an_objective_or_a_contract():
    for helper in (st.table_from_snowpark, st.load_into_snowpark):
        assert not {"contract", "objective"} & set(inspect.signature(helper).parameters), helper.__name__


def test_read_back_error_is_a_value_error_naming_where_it_failed():
    assert issubclass(st.HandoffError, ValueError) and issubclass(st.ReadBackError, st.HandoffError)
    error = st.ReadBackError("MIG_WORK.T", "AMOUNT", 3, "a cell its column type cannot hold")
    assert (error.table, error.column, error.row_index) == ("MIG_WORK.T", "AMOUNT", 3)
    assert "MIG_WORK.T.AMOUNT (row 3)" in str(error)
