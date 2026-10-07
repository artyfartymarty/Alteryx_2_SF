"""Typed tables in and out of a Snowpark session: how `validate_build.py` and the runtime sandbox
(`lib/snowpark_sandbox.py`) load golden data into a local session and read a `build.py`'s outputs back.

A table crosses as a `typed_csv` table (`{"fields", "rows"}`, contract C1): `Decimal` kept, dates and times
as the ISO text `typed_csv` holds, booleans/ints/floats/strings as they are. `table_from_snowpark` reads the
REAL schema from Snowpark's own `StructType` (`types_map.snowpark_to_alteryx`) and never takes an objective:
coercing a table to what an objective declares would hide exactly the type drift a parity test exists to
catch (phase-1 ruling 16).

`new_local_session` builds the Local Testing Framework session every local run uses -- a real (if in-memory,
unaccounted) Snowpark session, not a double; the framework keeps its tables in that session's own catalog,
invisible to any other session in the process, so a fresh session plays exactly the role a fresh
`DuckDBBackend()` plays for the SQL path.

Nothing here has run against a real Snowflake account: the Snowpark side is the Local Testing Framework, and
its pandas round trip has quirks `_coerce` undoes (see `table_from_snowpark`).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from .types_map import alteryx_to_snowpark, snowpark_to_alteryx


def new_local_session():
    """A brand-new Snowpark Local Testing Framework session (lazy import: the module stays importable
    without Snowpark installed, same as `types_map.py`)."""
    from snowflake.snowpark import Session  # noqa: PLC0415
    return Session.builder.configs({"local_testing": True}).create()


class HandoffError(ValueError):
    """A table cannot cross the engine seam as a typed table: a column whose type has no Alteryx
    counterpart, a cell its own column type cannot represent, or a table that is not there. A
    domain failure of whatever produced the table -- never a usage error about a prerequisite."""


class ReadBackError(HandoffError):
    """A cell in the actual table (or a whole column's own Snowpark type) cannot be represented as
    a `typed_csv` value (task-4 fix round 1, finding I3): a domain failure -- the procedure's own
    output is broken in a way `compare.py` was never meant to see -- not a usage error about a
    missing prerequisite. Carries enough to name exactly where."""

    def __init__(self, table: str, column: str, row_index: int | None, reason: str):
        where = f"row {row_index}" if row_index is not None else "its column type"
        super().__init__(f"{table}.{column} ({where}): cannot read back ({reason})")
        self.table = table
        self.column = column
        self.row_index = row_index
        self.reason = reason


# --- typed table -> Snowpark -> typed table ---------------------------------------------------------


def load_into_snowpark(session, fqn: str, table: dict) -> None:
    from snowflake.snowpark.types import StructField, StructType  # noqa: PLC0415  (lazy)
    schema = StructType([StructField(f["name"], alteryx_to_snowpark(f), True) for f in table["fields"]])
    rows = [list(_to_snowpark(value, field) for value, field in zip(row, table["fields"]))
            for row in table["rows"]]
    session.create_dataframe(rows, schema=schema).write.mode("overwrite").save_as_table(fqn)


def _to_snowpark(value, field):
    """typed_csv values -> what create_dataframe accepts: ISO strings become date/datetime objects."""
    if value is None:
        return None
    if field["type"] == "Date":
        return dt.date.fromisoformat(value)
    if field["type"] == "DateTime":
        return dt.datetime.fromisoformat(value)
    return value


def _coerce(value, alteryx_type: str):
    """A `session.table(...).to_pandas()` cell -> a `typed_csv` value, per the REAL column's
    Alteryx type (from `types_map.snowpark_to_alteryx`, never the contract's declared type -- see
    the module docstring): NaN/NaT/None -> None; a NULL-upcasted float back to `int` for an Int64
    column; a `Decimal` kept as-is; `Timestamp`/`date`/`time` -> the same ISO text `typed_csv`
    reads and writes; a numpy scalar -> the plain Python type underneath it. Raises `ValueError`/
    `TypeError`/`decimal.InvalidOperation`/`OverflowError` for a value its own column type cannot
    represent -- `table_from_snowpark` turns that into a `ReadBackError` naming exactly where."""
    if value is None:
        return None
    import pandas as pd  # noqa: PLC0415  (lazy: see module docstring; already loaded by to_pandas())
    if pd.isna(value):
        return None
    if alteryx_type == "Int64":
        return int(value)
    if alteryx_type == "FixedDecimal":
        return value if isinstance(value, Decimal) else Decimal(str(value))
    if alteryx_type == "Double":
        return float(value)
    if alteryx_type == "Bool":
        return bool(value)
    if alteryx_type == "Date":
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if alteryx_type == "DateTime":
        return value.isoformat(sep=" ") if hasattr(value, "isoformat") else str(value)
    if alteryx_type == "Time":
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if hasattr(value, "item"):  # a numpy scalar (e.g. numpy.str_) with no more specific handling above
        return value.item()
    return value


def table_from_snowpark(session, fqn: str) -> dict | None:
    """The actual table's real schema AND rows, both read from Snowpark itself -- never from the
    contract (task-4 fix round 1, findings C1/C2): comparing against the contract's declared
    columns only ever confirms what the contract already expected, so the schema `compare.py`
    checks has to come from what the procedure actually wrote, in its own column order, including
    a column the contract never declared and omitting one the handler failed to write. Returns
    `None` if the table doesn't exist (or any other error reading it back). Raises
    `ReadBackError` for a column whose Snowpark type has no Alteryx counterpart
    (`types_map.snowpark_to_alteryx`), or for a cell that column's own (real) type cannot
    represent as a `typed_csv` value -- both domain failures, never a usage error (finding I3).

    Local testing's pandas round trip has quirks that would otherwise look like data bugs: a
    nullable integer column that contains a NULL comes back as `float64` (NaN for the null, `1.0`
    for a real value); a `DecimalType` column as `decimal.Decimal`; a `TimestampType` column as
    `pandas.Timestamp` (or `NaT`); a `DateType` column as `datetime.date`. `_coerce` undoes all of
    that, per column."""
    try:
        table = session.table(fqn)
        schema_fields = list(table.schema.fields)
        pdf = table.to_pandas()
    except Exception:
        return None

    fields: list[dict] = []
    alteryx_types: list[str] = []
    for struct_field in schema_fields:
        try:
            alteryx = snowpark_to_alteryx(struct_field.datatype)
        except ValueError as exc:
            raise ReadBackError(fqn, struct_field.name, None, str(exc)) from exc
        fields.append({"name": struct_field.name, **alteryx})
        alteryx_types.append(alteryx["type"])

    names = [struct_field.name for struct_field in schema_fields]
    rows = []
    for row_index, record in enumerate(pdf.to_dict("records")):
        row = []
        for name, alteryx_type in zip(names, alteryx_types):
            try:
                row.append(_coerce(record.get(name), alteryx_type))
            except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
                raise ReadBackError(fqn, name, row_index, str(exc)) from exc
        rows.append(row)
    return {"fields": fields, "rows": rows}
