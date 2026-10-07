# wf_0007 — digest

7 tools · 2 outputs · from parsed/dag.json (deterministic; never edited by hand)
2 nodes that carry no data (containers, comments, browse tools) are not listed.

Each line: `T<id> <Type>  <what it does>  ← <where its rows come from>  (<anchor> → <where they go>)  — <annotation>`. `T3.T` is anchor T of tool 3. Expressions are kept whole.

## Workflow constants (as parsed; intake may override them)

User.CurrentYear = 2026

## Shared by more than one output

T1 Input  read targets.yxdb (yxdb)  — Plan targets by region and period
T2 Input  read actuals.yxdb (yxdb)  — Booked actuals, one row per sales line
T3 Join  on Left.REGION = Right.REGION and Left.PERIOD = Right.PERIOD; drop Right_REGION, Right_PERIOD  ← Left: T1, Right: T2   (L → none, J → T4, R → none)  — Targets to actuals on REGION, PERIOD
T4 Filter  Left([PERIOD], 4) = [User.CurrentYear]  ← T3.J   (T → T5, F → none)  — Keep the current year's periods
T5 Summarize  group by REGION, PERIOD; Sum(TARGET) as TARGET_TOTAL; Sum(ACTUAL) as ACTUAL_TOTAL; Count(REGION) as LINES  ← T4.T  — Target and actual per region and period

## Output T6 · region_attainment.yxdb

Shared tools used: T1, T2, T3, T4, T5
T6 Output  write region_attainment.yxdb (yxdb), mode overwrite  ← T5  — region_attainment.yxdb

## Output T7 · dbo.ATTAINMENT_HISTORY

Shared tools used: T1, T2, T3, T4, T5
T7 Output  write db:prod_plan table dbo.ATTAINMENT_HISTORY, mode update_insert, keys REGION, PERIOD  ← T5  — dbo.ATTAINMENT_HISTORY
