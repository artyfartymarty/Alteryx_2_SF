# wf_0003 — digest

10 tools · 1 output · from parsed/dag.json (deterministic; never edited by hand)
2 nodes that carry no data (containers, comments, browse tools) are not listed.

Each line: `T<id> <Type>  <what it does>  ← <where its rows come from>  (<anchor> → <where they go>)  — <annotation>`. `T3.T` is anchor T of tool 3. Expressions are kept whole.

## Workflow constants (as parsed; intake may override them)

User.PeriodEnd = 2026-08-31
User.Region = EMEA

## Output T10 · dbo.GL_SUMMARY

T1 Input  read db:prod_fin; query: SELECT ACCT, PERIOD, POSTED, AMOUNT, REGION, ENTRY_ID FROM dbo.GL_LEDGER WHERE AMOUNT <> 0  — GL ledger extract
T2 Filter  [REGION] = [User.Region]  ← T1   (T → T3, F → none)  — Keep the run's region
T3 DateTime  to_datetime POSTED with format "%d/%m/%Y" → POSTED_DT  ← T2.T  — POSTED is dd/mm/yyyy text
T4 Sort  ACCT asc, POSTED_DT asc, ENTRY_ID desc  ← T3  — Latest entry per posting first
T5 Unique  keys ACCT, POSTED_DT  ← T4   (U → T6, D → none)  — One entry per ACCT and day
T6 MultiRowFormula  new field RUN_BAL:Double = [Row-1:RUN_BAL] + [AMOUNT]; rows 1; group by ACCT; missing rows → zero  ← T5.U  — Running balance per ACCT
T7 RecordID  new field RecordID:Int32 starting at 1, position first  ← T6  — Number the closing rows
T8 Formula  PERIOD_END_FLAG:V_String(1) = IIF(DateTimeFormat([POSTED_DT], "%Y-%m-%d") = [User.PeriodEnd], "Y", "N")  ← T7  — Flag the period end posting
T9 Summarize  group by ACCT, PERIOD; Sum(AMOUNT) as TOTAL; Last(RUN_BAL) as CLOSING_BAL; Count(ENTRY_ID) as ENTRIES; Max(PERIOD_END_FLAG) as HAS_PERIOD_END  ← T8  — Closing balance is the last row
T10 Output  write db:prod_fin table dbo.GL_SUMMARY, mode update_insert, keys ACCT, PERIOD; PreSQL: DELETE FROM dbo.GL_SUMMARY WHERE PERIOD < '2020-01'; PostSQL: UPDATE dbo.GL_SUMMARY SET LOADED_FLAG = 'Y' WHERE LOADED_FLAG IS NULL  ← T9  — dbo.GL_SUMMARY
