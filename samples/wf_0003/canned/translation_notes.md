# wf_0003 — translation notes

One assumption per line. Nothing below has been checked against a real Alteryx engine or a real Snowflake account; every claim is against `docs/reference/simulator-semantics.md` (the parity oracle) and the objective.

- `User.Region` and `User.PeriodEnd` are workflow constants, resolved here to the literals `'EMEA'` and `'2026-08-31'`; nothing at run time supplies them.
- Tool 1's own query carries `WHERE AMOUNT <> 0`; the file reads the mapped table, so the predicate is written out again (a NULL `AMOUNT` is not `<> 0` either).
- Tool 3 reads `dd/mm/yyyy` text with `TRY_TO_TIMESTAMP_NTZ(POSTED, 'DD/MM/YYYY')`, the warn-and-null form (`31/02/2026` is the row that proves it); `POSTED_DT` is a `TIMESTAMP_NTZ` because an Alteryx DateTime carries midnight.
- Tools 4 and 5 are one `QUALIFY ROW_NUMBER() ... = 1`: the Sort's order is `ENTRY_ID DESC`, so the surviving duplicate of each `ACCT, POSTED_DT` is the one with the largest `ENTRY_ID`, and two NULL `POSTED_DT` rows of one `ACCT` are one key, as `PARTITION BY` treats them.
- Tools 6 and 7 spell the Sort's order out in each window (`ACCT`, `POSTED_DT` ascending NULLS FIRST, `ENTRY_ID` descending NULLS LAST): `RUN_BAL` adds the exact `NUMBER` amounts and casts to `FLOAT` once, which agrees for every amount in the golden sets; a running total long enough to lose a double's precision would not.
- Alteryx's `Last` has no ordered aggregate in either dialect, so `CLOSING_BAL` is `MAX_BY(RUN_BAL, RECORD_ID)`, the same row as long as `RECORD_ID` (a `ROW_NUMBER`) is unique; `ENTRIES` is `COUNT(*)` because Alteryx's `Count` counts rows.
- `HAS_PERIOD_END` is `MAX(IFF(TO_CHAR(POSTED_DT, 'YYYY-MM-DD') = '2026-08-31', 'Y', 'N'))`; a NULL date makes the condition NULL, which takes the else branch, so those rows are `'N'`.
- Output tool 10: the PreSQL (`DELETE ... WHERE PERIOD < '2020-01'`) stands immediately before the `MERGE` and the PostSQL (`UPDATE ... SET LOADED_FLAG = 'Y' WHERE LOADED_FLAG IS NULL`) immediately after it; the merge is on `ACCT, PERIOD` and `LOADED_FLAG` is left alone on a match and arrives NULL on an insert, which the PostSQL then fills.
- A NULL update key matches nothing, so the `edge` set's NULL-`ACCT` group is inserted rather than merged; the golden output is the table's state after all three statements, rows the run never touched included.

No lint flag is raised by `scripts/compile_check.py`, so there is no `lint <kind> <name>: kept because ...` line to write.
