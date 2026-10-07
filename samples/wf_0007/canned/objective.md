# wf_0007 — Regional targets and attainment

## Purpose

Pairs each region's plan target with its booked actuals, keeps the current year, and totals target, actual and line count per region and period into REGION_ATTAINMENT (replaced on every run) and into ATTAINMENT_HISTORY (merged, so earlier years stay).

## Inputs

- **TARGETS** (tool 1): REGION VARCHAR(10), PERIOD VARCHAR(7), TARGET NUMBER(19,2).
- **ACTUALS** (tool 2): REGION VARCHAR(10), PERIOD VARCHAR(7), ACTUAL NUMBER(19,2).

## Outputs

### REGION_ATTAINMENT (tool 6)

- Grain: one row per region and period of the current year.
- Written: overwrite.
- Key: REGION, PERIOD.
- Shape: aggregate rows; no row order to keep.
- Columns:
  - `REGION` VARCHAR(10) NOT NULL: Sales region
  - `PERIOD` VARCHAR(7) NOT NULL: Period, starting with its year
  - `TARGET_TOTAL` NUMBER(19,2): Sum of the plan targets for the region and period (a target counts once per matching actual line)
  - `ACTUAL_TOTAL` NUMBER(19,2): Sum of the booked actuals for the region and period; NULL when every actual line is NULL
  - `LINES` NUMBER(38,0) NOT NULL: Number of joined target and actual rows in the group

### ATTAINMENT_HISTORY (tool 7)

- Grain: one row per region and period; rows of earlier years stay in the table.
- Written: update_insert on REGION, PERIOD.
- Key: REGION, PERIOD.
- Shape: aggregate rows; no row order to keep.
- Columns:
  - `REGION` VARCHAR(10): Sales region
  - `PERIOD` VARCHAR(7): Period, starting with its year
  - `TARGET_TOTAL` NUMBER(19,2): Sum of the plan targets for the region and period (a target counts once per matching actual line)
  - `ACTUAL_TOTAL` NUMBER(19,2): Sum of the booked actuals for the region and period; NULL when every actual line is NULL
  - `LINES` NUMBER(38,0): Number of joined target and actual rows in the group

## Steps

- **s1 · Pair plan targets with actuals** (tools 1, 2, 3): Reads TARGETS and ACTUALS and inner-joins them on REGION and PERIOD; a target with several actual lines is paired with each, and rows with a NULL key or no partner are dropped. Feeds s2.
- **s2 · Keep the current year and total** (tools 4, 5): Keeps the periods whose first four characters equal the workflow constant User.CurrentYear (2026), then sums TARGET and ACTUAL and counts the joined rows per REGION and PERIOD. Feeds s3, s4.
- **s3 · Write the attainment summary** (tools 6): Writes REGION_ATTAINMENT, replacing the whole table. Feeds output 6.
- **s4 · Add to the attainment history** (tools 7): Merges the same rows into ATTAINMENT_HISTORY on REGION and PERIOD: a matched row is overwritten, a new key is inserted and every other row stays. Feeds output 7.

## Business rules

- A row only appears when at least one actual line matches its target's REGION and PERIOD; targets and actuals without a partner are dropped.
- A target is counted once per matching actual line, so TARGET_TOTAL adds the same target again for every extra line; that is Alteryx's own fan-out and the golden data keeps it.
- Only periods that start with the current year (User.CurrentYear, 2026) are kept; a malformed period such as 2026-1 is kept too.
- REGION_ATTAINMENT holds exactly this run's rows and nothing else.
- ATTAINMENT_HISTORY is merged on REGION and PERIOD: every row the run does not produce stays exactly as it was, prior years included.

## Alteryx behaviour to keep

- Tool 3, NULL_SEMANTICS: A NULL join key matches nothing, so a target row with a NULL REGION or PERIOD leaves on the L anchor, which is wired to nothing, and never reaches the J side. An inner join on `=` keeps that behaviour; a join written with IS NOT DISTINCT FROM would not. That is also why REGION and PERIOD are never NULL once the join is done.
- Tool 3, LOGIC: The join is many-to-many on (REGION, PERIOD): every target row is paired with every actual line of its key, so a key with two actual lines carries its TARGET twice. The fan-out is Alteryx's own behaviour and the golden data keeps it; the downstream Sum therefore counts a target once per actual line, and a translation that pre-aggregated the actuals before joining would disagree.
- Tool 3, LOGIC: Key comparison is exact and case-sensitive -- no trimming, no case folding. Right_REGION and Right_PERIOD are deselected, so the joined rows carry the left side's key columns only, which on a matched row are equal to the right side's anyway.
- Tool 4, NULL_SEMANTICS: Only rows the expression makes really true leave on the True anchor. Left of a NULL PERIOD is NULL, NULL = '2026' is NULL, and the row is dropped with the False anchor, which is wired to nothing. SQL's WHERE drops a NULL condition the same way. (Upstream, the join already refuses a NULL PERIOD, so none reaches this tool.)
- Tool 4, LOGIC: User.CurrentYear is a text constant (IsNumeric False), so the comparison is string against string and is inlined as the literal '2026' -- never a number, which would make Snowflake cast PERIOD's prefix. Left takes the first four characters whatever follows them, so a malformed PERIOD such as '2026-1' is kept (the edge set carries one), in the model exactly as in Alteryx.
- Tool 5, LOGIC: Alteryx's Count counts ROWS, not values, so LINES is COUNT(*). Because the join fans a target out over its actual lines, LINES is the number of actual lines matched, and TARGET_TOTAL adds the same target once per line -- the sum the golden data records.
- Tool 5, NULL_SEMANTICS: Sum skips NULLs and a group whose values are all NULL sums to NULL, not 0: WEST 2026-01 in the normal set has one NULL actual line, so its ACTUAL_TOTAL is NULL. SQL's SUM agrees; a COALESCE(SUM(...), 0) would not.
- Tool 5, ROUNDING: TARGET_TOTAL and ACTUAL_TOTAL are FixedDecimal(19,2) sums of FixedDecimal(19,2) values, so the addition is exact on both sides and the result is cast back to NUMBER(19,2). No golden sum comes near 17 integer digits.
- Tool 6, LOGIC: Overwrite replaces the file wholesale, so REGION_ATTAINMENT holds exactly this run's summary rows and no prior state; the model is a `table`, rebuilt on every run.
- Tool 7, LOGIC: Update; Insert if new on (REGION, PERIOD) maps columns to the target by name: a matched row is overwritten, a new key is inserted and every row the run does not produce stays exactly as it was -- including prior-year rows the filter keeps out of the summary. The golden output is the table's state AFTER the write. The merge key must be both columns: a key of REGION alone would match a region's other periods and lose or overwrite rows.
- Tool 7, NULL_SEMANTICS: A NULL update key matches nothing and would be inserted rather than merged. The summary never carries one (the inner join refuses NULL keys), so this is recorded rather than exercised.

## Questions

None open.
