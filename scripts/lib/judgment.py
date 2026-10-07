"""The analyzer's judgment fields: their vocabularies and shapes, in one place.

`row_relation`, `ordering`, `tolerances`, `normalizations` and `parity_risks` mean the same thing
in an objective's outputs (`objective_check.py`): `compare.py` reads them through
`lib/objective.py: contract_view`. The checker holds the analyzer to these words, so they live here;
`objective_scaffold.py` and `objective_questions.py` share the small formatting and type helpers that
print a problem the same way.

Nothing here has run on Snowflake or Alteryx: these are constants and pure functions.
"""
from __future__ import annotations

import json
import math
import re

ROW_RELATIONS = ("1:1", "filter", "aggregate", "expand")
ORDERING_FIELDS = ("keys", "alteryx_deterministic", "order_dependent_columns")
TOLERANCE_KEYS = frozenset({"float_abs", "float_rel"})
NORMALIZATION_OPS = ("trim", "upper")
RISK_FIELDS = ("tool_id", "class", "note")
LIST_LIMIT = 20

ORDERING_SHAPE = ('{"keys": [<column>...], "alteryx_deterministic": true|false, '
                  '"order_dependent_columns": [<output column>...]}')
TOLERANCE_SHAPE = '{"float_abs": <number >= 0>, "float_rel": <number >= 0>} (either or both)'
NORMALIZATIONS_SHAPE = 'a list of "trim:<COLUMN>" / "upper:<COLUMN>" directives (may be empty)'
TYPE_SHAPE = 'a Snowflake column type such as "VARCHAR(10)" or "NUMBER(38,0)"'

#: A column type as `compile_check.py` splices it into the DDL of a contract-shaped table: one or more
#: words (`GEOGRAPHY`, `TIMESTAMP_NTZ`, `DOUBLE PRECISION`) and an optional `(precision[, scale])`.
#: Anything else -- empty, not a string, a quote, a `;` -- is refused (review of L3, I2).
TYPE_GRAMMAR = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?: [A-Za-z][A-Za-z0-9_]*)*(?:\(\s*\d+\s*(?:,\s*\d+\s*)?\))?$")


def shown(value) -> str:
    """A value as a problem line quotes it: JSON, at most 120 characters."""
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 120 else text[:117] + "..."


def names(items) -> str:
    """At most `LIST_LIMIT` names, comma-separated, with the number left out."""
    items = list(items)
    head = ", ".join(str(n) for n in items[:LIST_LIMIT])
    return head + (f", ... ({len(items) - LIST_LIMIT} more)" if len(items) > LIST_LIMIT else "")


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value) -> bool:
    """A JSON number `compare.py` can read with `float()`: never a bool, never infinite, never an
    integer too large for a float (which overflows -- review of L3, M5)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False
