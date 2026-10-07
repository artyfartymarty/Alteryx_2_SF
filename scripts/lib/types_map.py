"""Alteryx field types → Snowflake column types, and the coarse families parity checks compare on.

The mapping is the one the cookbook documents for hand-written procedures, so a golden CSV loaded
into the sandbox has the same column type the translated procedure will meet on Snowflake. Nothing
here has been checked against a live Snowflake account.

`type_family` deliberately collapses widths and precisions: `compare.py` treats
`NUMBER(19,2)` and `NUMBER(38,0)` as the same family so a harmless precision change is not
reported as a schema failure, while `VARCHAR` vs `NUMBER` still is.
"""
from __future__ import annotations

import re

# Alteryx integer types all become NUMBER(38,0): Alteryx widths are storage hints, and Snowflake's
# NUMBER is one physical type regardless of the declared precision.
_INTEGER_TYPES = frozenset({"Byte", "Int16", "Int32", "Int64"})

_FIXED_TYPES = {
    "Float": "FLOAT",
    "Double": "FLOAT",
    "Bool": "BOOLEAN",
    "Date": "DATE",
    "Time": "TIME",
    "DateTime": "TIMESTAMP_NTZ",
    "Blob": "BINARY",
    "V_String": "VARCHAR",
    "V_WString": "VARCHAR",
}

# Sized strings keep their declared length. DuckDB does not enforce it (see backend.py), so a
# length overflow that Snowflake would reject cannot be reproduced locally. V_String/V_WString stay
# unsized in both `alteryx_to_snowflake` and `alteryx_to_snowpark` (Task 3 fix round 1, coordinator
# ruling: alteryx_to_snowpark mirrors this SQL policy exactly, not a size-when-present rule of its
# own -- an earlier version of this file sized V_String/V_WString for Snowpark; that was wrong).
_SIZED_STRING_TYPES = frozenset({"String", "WString"})

_FAMILIES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"NUMBER", "NUMERIC", "DECIMAL", "DEC", "INT", "INTEGER", "BIGINT", "SMALLINT",
                "TINYINT", "BYTEINT"}), "number"),
    (frozenset({"FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "REAL"}), "float"),
    (frozenset({"VARCHAR", "CHAR", "CHARACTER", "STRING", "TEXT", "NVARCHAR", "NCHAR"}), "string"),
    (frozenset({"DATE"}), "date"),
    (frozenset({"TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "DATETIME"}), "timestamp"),
    (frozenset({"TIME"}), "time"),
    (frozenset({"BOOLEAN", "BOOL"}), "bool"),
    (frozenset({"BINARY", "VARBINARY", "BLOB", "BYTEA"}), "binary"),
)


def alteryx_to_snowflake(field: dict) -> str:
    """{"name", "type", "size", "scale"} → a Snowflake column type.

    Raises ValueError for a type the mapping does not cover: the pipeline never guesses a type.
    """
    alteryx_type = field["type"]
    if alteryx_type in _INTEGER_TYPES:
        return "NUMBER(38,0)"
    if alteryx_type == "FixedDecimal":
        return f"NUMBER({field['size']},{field.get('scale') or 0})"
    if alteryx_type in _SIZED_STRING_TYPES:
        return f"VARCHAR({field['size']})"
    if alteryx_type in _FIXED_TYPES:
        return _FIXED_TYPES[alteryx_type]
    raise ValueError(f"no Snowflake type mapping for Alteryx type {alteryx_type!r} "
                     f"(field {field.get('name', '?')!r})")


def alteryx_to_snowpark(field: dict):
    """{"name", "type", "size", "scale"} -> a `snowflake.snowpark.types.DataType` instance.

    Mirrors `alteryx_to_snowflake`'s families (contract C4's Snowpark procedures read/write the
    same columns a SQL procedure would), just as Snowpark type objects instead of SQL text.
    Imports `snowflake.snowpark.types` lazily so this module -- and everything that imports it,
    including `backend.py` on every test run -- stays importable without Snowpark installed.
    """
    from snowflake.snowpark.types import (  # noqa: PLC0415  (lazy: see docstring)
        BooleanType, DateType, DecimalType, DoubleType, LongType, StringType, TimestampType, TimeType,
    )

    alteryx_type = field["type"]
    if alteryx_type in _INTEGER_TYPES:
        return LongType()
    if alteryx_type in ("Float", "Double"):
        return DoubleType()
    if alteryx_type == "FixedDecimal":
        return DecimalType(field["size"], field.get("scale") or 0)
    if alteryx_type == "Bool":
        return BooleanType()
    if alteryx_type == "Date":
        return DateType()
    if alteryx_type == "DateTime":
        return TimestampType()
    if alteryx_type == "Time":
        return TimeType()
    if alteryx_type in _SIZED_STRING_TYPES:
        return StringType(field["size"])
    if alteryx_type in ("V_String", "V_WString"):
        return StringType()
    raise ValueError(f"no Snowpark type mapping for Alteryx type {alteryx_type!r} "
                     f"(field {field.get('name', '?')!r})")


#: The maximum VARCHAR length the Local Testing Framework (and real Snowflake) report for a column
#: declared with no explicit length: an "unsized" string is not represented as "no length" on
#: read-back, it is represented as this concrete number (task-4 fix round 1, finding C1/I3 rulings).
_VARCHAR_MAX = 16777216


def snowpark_to_alteryx(data_type) -> dict:
    """A `snowflake.snowpark.types.DataType` (as read back from a real column, e.g.
    `session.table(fqn).schema`) -> `{"type", "size", "scale"}` (no `"name"` -- the caller already
    has that from the `StructField`). `alteryx_to_snowpark`'s exact inverse, used to find out what
    a procedure *actually* wrote, never what a contract merely declared (task-4 fix round 1,
    findings C1/C2: the whole point of reading this back is to catch a handler whose real output
    disagrees with the contract, so the type has to come from Snowpark itself). Imports
    `snowflake.snowpark.types` lazily, like its sibling `alteryx_to_snowpark`.

    Raises `ValueError` for a Snowpark type this project has no Alteryx counterpart for (`Binary`,
    `Variant`, `Array`, `Map`, nested `Struct`, ...) -- the pipeline never guesses a type here
    either, any more than `alteryx_to_snowflake`/`alteryx_to_snowpark` do in the other direction.
    """
    from snowflake.snowpark.types import (  # noqa: PLC0415  (lazy: see docstring)
        BooleanType, ByteType, DateType, DecimalType, DoubleType, FloatType, IntegerType, LongType,
        ShortType, StringType, TimestampType, TimeType,
    )

    if isinstance(data_type, (LongType, IntegerType, ShortType, ByteType)):
        return {"type": "Int64", "size": None, "scale": None}
    if isinstance(data_type, (DoubleType, FloatType)):
        return {"type": "Double", "size": None, "scale": None}
    if isinstance(data_type, DecimalType):
        return {"type": "FixedDecimal", "size": data_type.precision, "scale": data_type.scale}
    if isinstance(data_type, BooleanType):
        return {"type": "Bool", "size": None, "scale": None}
    if isinstance(data_type, DateType):
        return {"type": "Date", "size": None, "scale": None}
    if isinstance(data_type, TimestampType):    # any tz variant (NTZ/LTZ/TZ/default)
        return {"type": "DateTime", "size": None, "scale": None}
    if isinstance(data_type, TimeType):
        return {"type": "Time", "size": None, "scale": None}
    if isinstance(data_type, StringType):
        length = data_type.length
        is_max = bool(getattr(data_type, "_is_max_size", False))
        if is_max or length is None or length >= _VARCHAR_MAX:
            return {"type": "V_String", "size": None, "scale": None}
        return {"type": "String", "size": length, "scale": None}
    raise ValueError(f"no Alteryx type mapping for Snowpark type {data_type!r}")


def type_family(sql_type: str) -> str:
    """A Snowflake (or DuckDB) column type → its comparison family, or "other" if unrecognised."""
    base = re.sub(r"\(.*", "", str(sql_type)).strip().upper()
    for names, family in _FAMILIES:
        if base in names:
            return family
    return "other"


def snowflake_text_to_snowpark(type_text: str):
    """A Snowflake column type as `objective.json` writes it (`NUMBER(38,0)`, `VARCHAR(20)`, `FLOAT`, `DATE`, ...)
    -> a `snowflake.snowpark.types.DataType`: what `compile_check.py` needs to create an empty input table for
    a `build.py` run. `NUMBER(p,0)` and the integer spellings are `LongType` (as `alteryx_to_snowpark` maps an
    Alteryx integer), `NUMBER(p,s)` with a scale is `DecimalType`, every float is `DoubleType`. Raises
    `ValueError` for a type with no mapping (`VARIANT`, `ARRAY`, ...): the caller decides what an empty
    table of that column should be."""
    from snowflake.snowpark.types import (  # noqa: PLC0415  (lazy, like alteryx_to_snowpark)
        BinaryType, BooleanType, DateType, DecimalType, DoubleType, LongType, StringType, TimestampType, TimeType,
    )

    text = str(type_text).strip().upper()
    family = type_family(text)
    numbers = [int(number) for number in re.findall(r"\d+", text)]
    if family == "number":
        if len(numbers) >= 2 and numbers[1] > 0:
            return DecimalType(numbers[0], numbers[1])
        return LongType()
    if family == "float":
        return DoubleType()
    if family == "string":
        return StringType(numbers[0]) if numbers else StringType()
    if family == "date":
        return DateType()
    if family == "timestamp":
        return TimestampType()
    if family == "time":
        return TimeType()
    if family == "bool":
        return BooleanType()
    if family == "binary":
        return BinaryType()
    raise ValueError(f"no Snowpark type mapping for Snowflake type {type_text!r}")
