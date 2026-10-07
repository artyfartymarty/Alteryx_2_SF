# wf_0002 — translation notes

One assumption per line. Nothing below has been checked against a real Alteryx engine or a real Snowflake account; every claim is against `docs/reference/simulator-semantics.md` (the parity oracle) and the objective.

- The two prep groups are the temporary tables `customers_clean` (tools 1 and 2) and `orders_typed` (tools 3 and 4), each read once; the Join's three outputs are built in the one `INSERT INTO` that appends them, with `UNION ALL`. No work table survives the run.
- Tool 2's Data Cleansing options run in the macro's own order: NULL to blank first (trimming a NULL leaves NULL), then trim, then upper-case, so `NAME` and `CITY` are never NULL downstream of it.
- `UPPER` is Snowflake's own Unicode case rules, which are not guaranteed to match Alteryx's for every script; the `edge` golden set exercises Latin Extended and CJK text.
- Every column of the orders export arrives as text; every conversion in tool 4 is a `TRY_` form (Alteryx warns and nulls on text it cannot read), so `'abc'` and the empty string give a NULL `AMOUNT`, and a date that does not exist (`2026-02-30`) or a non-ISO date (`31/12/2026`) gives a NULL `ORDER_DATE`; the date is read with an explicit `YYYY-MM-DD` format.
- Alteryx's integer coercion rounds half away from zero, which `TRY_TO_NUMBER(x, 38, 0)` also does; the sample data is whole numbers, so nothing here proves it.
- Tool 5's Join matches with `=`, so a NULL key matches nothing and the row leaves on L or R; the two anti-join branches use `NOT EXISTS`, where a `NOT IN` would drop every row once the other side held a NULL. Key comparison is exact and case-sensitive.
- Tool 9's Union is by name: every branch is projected into the first branch's column list, with NULL for a field the branch lacks, before the `UNION ALL`.
- The output is appended with an explicit column list (`INSERT INTO {{tgt}}.CUSTOMER_ORDER_FACT (...)`), so columns are matched to the table by name; the golden output is the table's state after the append, the rows it already held included.
- `CUSTOMER_ORDER_FACT` declares no keys, so its comparison is a row multiset.
- Tool 11 is a Browse: it shows rows to a person and changes nothing, so the objective lists it as having no effect and the file has no code for it.

No lint flag is raised by `scripts/compile_check.py`, so there is no `lint <kind> <name>: kept because ...` line to write.
