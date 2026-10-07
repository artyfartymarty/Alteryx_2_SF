# wf_0001 — translation notes

One assumption per line. Nothing below has been checked against a real Alteryx engine or a real Snowflake account; every claim is against `docs/reference/simulator-semantics.md` (the parity oracle) and the objective.

- Tools 1 and 2 are read once into the temporary table `clean_orders`, because both outputs start from the same cleaned rows; each output is then written once with `CREATE OR REPLACE TABLE`, its write mode being overwrite.
- Tool 2's `String(10)` retype of `CUSTOMER` is `LEFT(CUSTOMER, 10)`: Alteryx truncates silently where a Snowflake `VARCHAR(10)` would raise, and `LEFT` counts characters, so the non-ASCII customer in the `edge` set truncates by character and not by byte.
- Tool 2's `*Unknown` selection appends the five unlisted fields in incoming order, which fixes the column order of `EXCLUDED_ORDERS` as `CUSTOMER, ORDER_STATUS, ORDER_ID, REGION, AMOUNT_TXT, QTY, ORDER_DATE`.
- Tool 3's Filter sends a NULL `REGION` to the False anchor: `SALES_SUMMARY` reads `WHERE REGION <> 'WEST'`, where a NULL is not true, and `EXCLUDED_ORDERS` reads `WHERE NOT (REGION <> 'WEST') OR REGION IS NULL`, so the NULL-region order reaches it.
- `ToNumber` is translated as `TRY_TO_DOUBLE`; on the local runtime it accepts `inf` and `nan` where the model would give NULL, and no golden set contains either, so that divergence is recorded rather than guarded against.
- `CAST(<double> AS NUMBER(38,10))` is assumed to reproduce the model's decimal conversion of a stored `Double`. It does for every amount in the golden sets, each of which has at most three decimal places; a value needing more than ten would round here and not there.
- The local runtime gives `NUMBER(38,10) * NUMBER(38,10)` scale 20; Snowflake documents scale `min(S1 + S2, max(S1, S2, 12))` for multiplication, so the product carries scale 12 there. That is enough for this workflow's arithmetic and needs verification on Snowflake.
- `TOTAL_NET` adds the exact `NUMBER` values of `NET` and casts the total to `FLOAT` once, as the model adds exact decimals and stores once; `ORDERS` is `COUNT(*)` and `PRICED_ORDERS` is `COUNT(AMOUNT)`, matching Alteryx's `Count` against `CountNonNull`.
- The `ORDER BY` of `SALES_SUMMARY` spells the NULL placement out (`TOTAL_NET DESC NULLS LAST, REGION ASC NULLS FIRST`): Alteryx puts NULL first ascending and last descending.
- `EXCLUDED_ORDERS` declares no keys (the `edge` golden set holds two byte-identical `ORDER_ID` 103 rows), so its comparison is a row multiset: exact about whether the rows match, while a difference is attributed to a column by nearest-match pairing, which is a heuristic.

No lint flag is raised by `scripts/compile_check.py`, so there is no `lint <kind> <name>: kept because ...` line to write.
