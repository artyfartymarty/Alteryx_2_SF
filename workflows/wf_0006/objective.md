# wf_0006 — Subscription revenue recognition

## Purpose

Recognises subscription revenue period by period for each customer, carrying unrecognised (deferred) amounts forward until they fit under the customer's cap, and totals what was recognised and deferred per period into REVENUE_BY_PERIOD.

## Inputs

- **SUBSCRIPTIONS** (tool 1): CUSTOMER VARCHAR(20), PERIOD VARCHAR(7), BILLED FLOAT, CAP FLOAT, CANCELLED BOOLEAN.

## Outputs

### REVENUE_BY_PERIOD (tool 5)

- Grain: one row per period.
- Written: overwrite.
- Key: PERIOD.
- Shape: aggregate rows; no row order to keep.
- Columns:
  - `PERIOD` VARCHAR(254): Billing period
  - `TOTAL_RECOGNIZED` FLOAT NOT NULL: Sum of the revenue recognised by every customer in the period
  - `TOTAL_DEFERRED` FLOAT NOT NULL: Sum of the amounts deferred to later periods by every customer
  - `CUSTOMERS` NUMBER(38,0) NOT NULL: Number of schedule rows in the period, one per customer billed in it

## Steps

- **s1 · Keep the billed periods** (tools 1, 2): Reads SUBSCRIPTIONS and keeps the periods whose BILLED is above 0; a period billed exactly 0, or with no BILLED, is dropped. Feeds s2.
- **s2 · Build each customer's revenue schedule** (tools 3): For each customer, in CUSTOMER and PERIOD order, carries a deferred balance forward, resets it to 0 when the period is cancelled (before that period's own cap applies), recognises the smaller of billed plus carried and the CAP, defers the rest, and rounds both amounts to 2 decimals. Feeds s3.
- **s3 · Total recognised and deferred revenue per period** (tools 4, 5): Sums RECOGNIZED and DEFERRED and counts the schedule rows per PERIOD, and writes REVENUE_BY_PERIOD. Feeds output 5.

## Business rules

- Only periods with BILLED strictly above 0 count; a period billed exactly 0 is dropped, not kept as zero revenue.
- Within a customer, periods are processed in PERIOD order and the deferred balance carries from one to the next.
- A cancelled period resets the carried balance to 0 before that period's own cap is applied.
- Each period recognises the smaller of (billed plus the carried balance) and the customer's CAP, and defers the rest.
- RECOGNIZED and DEFERRED are rounded to 2 decimals the way Python's round does it, not the way Alteryx's own Round does.
- REVENUE_BY_PERIOD has one row per PERIOD; CUSTOMERS counts the schedule rows in the period.

## Alteryx behaviour to keep

- Tool 2, NULL_SEMANTICS: `NULL > 0` is NULL, which is not true, so a period with no BILLED amount leaves on the unwired False anchor -- that is what makes BILLED never NULL downstream, and it is also what keeps the Python tool (tool 3) from meeting a NULL it would have to guess about.
- Tool 2, LOGIC: The predicate is strict: a BILLED of exactly 0 is dropped as well, not kept as a zero-revenue period. The `normal` golden set carries one of each so the two cases are told apart.
- Tool 1, TYPE: CUSTOMER and PERIOD are V_String(20)/V_String(7) in the yxdb, and `CREATE TABLE ... AS` takes each column's type from the expression that produced it rather than enforcing the declared width; the `edge` set's 20-character CUSTOMER sits exactly on its limit and proves nothing about one character further.
- Tool 3, LOGIC: The deferred balance carries from one row to the next inside a customer, so RECOGNIZED and DEFERRED depend on the order the rows are processed in. The script fixes that order itself with `sort_values(["CUSTOMER", "PERIOD"])`, and (CUSTOMER, PERIOD) is unique on the incoming stream in every golden set, so the sort is total and the result is reproducible.
- Tool 3, LOGIC: A cancelled period resets the carried balance to zero BEFORE that period's own cap is applied, so a cancellation is visible in RECOGNIZED as well as in DEFERRED.
- Tool 3, NULL_SEMANTICS: pandas `groupby` drops rows whose key is NULL, so a NULL CUSTOMER would silently vanish rather than form its own schedule. Nothing upstream guarantees a non-NULL CUSTOMER; the Filter only guarantees BILLED.
- Tool 3, NULL_SEMANTICS: A NULL CAP is silently NOT a cap: `float(NULL)` is `nan` on both engines and `min(x, nan)` returns `x`, so the row recognizes everything billed and carries nothing forward. Both sides do the identical thing, so parity holds and no diff would ever surface it -- it is a data-quality risk in the source, recorded here rather than guarded against.
- Tool 3, NULL_SEMANTICS: A NULL CANCELLED is where the two engines disagree by default: the simulator types the column as pandas `boolean`, where `bool(pd.NA)` raises, while the Local Testing Framework's `to_pandas()` yields `None`, where `bool(None)` is quietly `False`. A migration must not turn a loud failure into silent output, so build.py tests `pd.isna(row["CANCELLED"])` and raises -- it fails on exactly the row the Alteryx script fails on, and the validation would report it as a FAIL. No golden set carries such a row: the simulator cannot build one, because the Python tool raises before it can be written. How REAL Alteryx types a NULL Bool field inside the Python tool is UNVERIFIED -- the simulator's nullable dtype is this project's assumption, not an observation.
- Tool 3, ROUNDING: Both sides compute in IEEE doubles and round to 2 decimals with Python's own `round`, which is round-half-to-even on the binary value -- not the half-away-from-zero Alteryx uses in its formula engine. That agreement is only guaranteed because the same Python expression runs on both sides; a SQL rewrite of this tool would not round the same way.
- Tool 4, LOGIC: Alteryx's Count counts ROWS, not values, so CUSTOMERS is COUNT(*) and not COUNT(CUSTOMER); the two agree here only because CUSTOMER is never NULL in the schedule the Python tool writes. Grouping is by PERIOD alone, so CUSTOMERS is the number of subscription rows that fell in the period, which is the number of distinct customers only because (CUSTOMER, PERIOD) is unique upstream.
- Tool 4, ROUNDING: Summarize's Sum adds the exact decimal value of each Double and lands the result back in a Double field, so the sum goes through NUMBER(38,10) rather than accumulating in FLOAT -- adding floats in a different order would not give the same last bit.
- Tool 4, ORDERING: Alteryx emits summarize groups sorted ascending with NULL first; SQL's GROUP BY promises no order at all. The target declares PERIOD as its key, so the comparison joins on it and never depends on the physical order -- no ORDER BY is added to imitate a sort nothing downstream reads.
- Tool 5, LOGIC: Write mode Overwrite: the target is replaced wholesale, so it holds no prior state and there is no targets_before file for this workflow.

## Questions

None open.
