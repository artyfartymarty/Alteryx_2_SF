# wf_0003 — GL period close

## Purpose

Closes a general-ledger period for one region: keeps the latest entry per account and day, runs a balance per account, and loads one row per account and period -- total, closing balance and entry count -- into GL_SUMMARY.

## Inputs

- **GL_LEDGER** (tool 1): ACCT VARCHAR(20), PERIOD VARCHAR(7), POSTED VARCHAR(10), AMOUNT NUMBER(19,2), REGION VARCHAR(10), ENTRY_ID NUMBER(38,0).

## Outputs

### GL_SUMMARY (tool 10)

- Grain: one row per account and period.
- Written: update_insert on ACCT, PERIOD.
- PreSQL, run before the write: `DELETE FROM dbo.GL_SUMMARY WHERE PERIOD < '2020-01'`
- PostSQL, run after the write: `UPDATE dbo.GL_SUMMARY SET LOADED_FLAG = 'Y' WHERE LOADED_FLAG IS NULL`
- Key: ACCT, PERIOD.
- Shape: aggregate rows; sorted by ACCT, POSTED_DT, ENTRY_ID; order-dependent columns: CLOSING_BAL.
- Columns:
  - `ACCT` VARCHAR(20): Account
  - `PERIOD` VARCHAR(7): Accounting period
  - `TOTAL` NUMBER(19,2): Sum of the kept entries' AMOUNT for the account and period
  - `CLOSING_BAL` FLOAT: Running balance of the account at its last entry in the period
  - `ENTRIES` NUMBER(38,0): Number of kept entries (rows, whether or not they carry an ENTRY_ID)
  - `HAS_PERIOD_END` VARCHAR(1): Y when any kept entry was posted on the period-end date, otherwise N
  - `LOADED_FLAG` VARCHAR(1): Set to Y by the PostSQL on rows that have none yet; not written by the summary itself, so NULL on a row the run inserted until the PostSQL runs

## Steps

- **s1 · Extract the region's ledger entries** (tools 1, 2, 3): Reads GL_LEDGER (the source query keeps only rows whose AMOUNT is not 0), keeps the run's region (the workflow constant User.Region, EMEA) and parses POSTED from dd/mm/yyyy text into a date-time (POSTED_DT). Feeds s2.
- **s2 · Keep the latest entry per account and day** (tools 4, 5): Sorts by ACCT, POSTED_DT ascending and ENTRY_ID descending, then keeps the first row for each ACCT and POSTED_DT, which is the one with the largest ENTRY_ID. Feeds s3.
- **s3 · Running balance and period-end flag** (tools 6, 7, 8): Adds a running balance (RUN_BAL) per account in that order, numbers the rows (RecordID) and flags the postings made on the period-end date (the constant User.PeriodEnd, 2026-08-31) in PERIOD_END_FLAG. Feeds s4.
- **s4 · Close the period and load GL_SUMMARY** (tools 9, 10): Summarizes per ACCT and PERIOD (total AMOUNT, the last RUN_BAL as the closing balance, the entry count, whether any posting is on the period-end date); deletes GL_SUMMARY rows before 2020-01, merges the summary in on ACCT and PERIOD, and then sets LOADED_FLAG to Y where it is still NULL. Feeds output 10.

## Business rules

- Only the ledger rows of the run's region (User.Region, EMEA) are used.
- Rows whose AMOUNT is 0 or NULL never reach the workflow: the source query drops them.
- When an account has several entries on one day, only the one with the largest ENTRY_ID is kept.
- CLOSING_BAL is the running balance on the last row of the account and period in the order the rows are numbered after the sort.
- GL_SUMMARY is updated, not replaced: a row with the same ACCT and PERIOD is overwritten, a new one is inserted and every other row stays exactly as it was.
- Before the load, GL_SUMMARY rows with PERIOD earlier than 2020-01 are deleted; after it, LOADED_FLAG is set to Y wherever it is still NULL.
- HAS_PERIOD_END is Y when any kept entry of the account and period was posted on the period-end date.

## Alteryx behaviour to keep

- Tool 1, LOGIC: The Input tool's own query carries `WHERE AMOUNT <> 0`. The translation reads the mapped table rather than running that query, so the predicate has to be written out again; without it every zero or NULL amount would reach the rest of the workflow, which Alteryx never saw.
- Tool 2, NULL_SEMANTICS: Filter [REGION] = [User.Region]: a NULL REGION makes the expression NULL, and Alteryx sends both false AND NULL to the False anchor. That anchor is not wired, so those rows simply leave the workflow -- which is also what a three-valued SQL WHERE does with them.
- Tool 2, LOGIC: [User.Region] is a workflow constant, not a column. Its value ('EMEA') is resolved at translation time into a literal; nothing at run time supplies it, and a run wanting another region needs a new translation.
- Tool 3, NULL_SEMANTICS: The DateTime tool reads dd/mm/yyyy text and writes NULL for text the format cannot read, so TRY_TO_TIMESTAMP_NTZ (not TO_TIMESTAMP_NTZ) is the warn-and-null form; 31/02/2026 is the row that proves it.
- Tool 3, TYPE: POSTED_DT is an Alteryx DateTime, so a parsed date carries midnight: the column is TIMESTAMP_NTZ, not DATE, and comparing it against a DATE would be a type difference.
- Tool 4, ORDERING: The Sort is not cosmetic: tools 5, 6, 7 and 9 all read its row order. NULL sorts first ascending and last descending, so every window's ORDER BY spells the NULLS placement out.
- Tool 5, LOGIC: Unique keeps the FIRST row of each (ACCT, POSTED_DT) in incoming order, and incoming order is the Sort's ENTRY_ID DESC, so the surviving duplicate is the one with the LARGEST ENTRY_ID. A row number taken in any other order changes which row's AMOUNT reaches the Summarize.
- Tool 5, NULL_SEMANTICS: Unique compares key values exactly, and two NULL POSTED_DT rows of the same ACCT count as the same key. PARTITION BY treats NULLs as one group, which is the same rule.
- Tool 6, ROUNDING: RUN_BAL is a Double field, so the model adds the exact decimal amounts and converts to a binary double when it stores each row. The translation sums the NUMBER column in a window and casts once, which agrees for every amount in the golden sets; a running total long enough to lose a double's precision would not.
- Tool 7, ORDERING: Record ID numbers rows in incoming order, so its value depends entirely on the Sort. It never reaches the target -- the Summarize drops it -- but CLOSING_BAL is defined as the last row by that number, so ordering.order_dependent_columns names CLOSING_BAL.
- Tool 8, NULL_SEMANTICS: IIF's condition is NULL when POSTED_DT is NULL, and a NULL condition takes the else branch in both Alteryx and SQL, so those rows are flagged 'N' rather than NULL.
- Tool 9, LOGIC: Alteryx's Count counts ROWS, not values, so ENTRIES is COUNT(*) and the edge set's NULL-ENTRY_ID row still counts as one.
- Tool 9, ORDERING: Last takes the group's last row in incoming order, including a NULL. There is no ordered aggregate for it in either dialect, so the translation takes the value at the largest RECORD_ID; that is the same row as long as RECORD_ID is unique, which it is because it is a row number.
- Tool 10, NULL_SEMANTICS: Update; Insert if new maps columns to the target by NAME and writes only the six the Summarize produces. LOADED_FLAG is left untouched on a matched row and arrives NULL on an inserted one, which is precisely what the PostSQL then fills. A NULL update key matches nothing, so the edge set's NULL-ACCT group is inserted rather than merged.
- Tool 10, LOGIC: The golden output is the target's state AFTER PreSQL, write and PostSQL -- not this run's rows. A row the run does not touch (9999) stays exactly as it was, and PERIOD < '2020-01' is deleted before anything is written.

## Questions

None open.
