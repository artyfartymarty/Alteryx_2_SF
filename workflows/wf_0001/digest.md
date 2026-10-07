# wf_0001 — digest

8 tools · 2 outputs · from parsed/dag.json (deterministic; never edited by hand)

Each line: `T<id> <Type>  <what it does>  ← <where its rows come from>  (<anchor> → <where they go>)  — <annotation>`. `T3.T` is anchor T of tool 3. Expressions are kept whole.

## Shared by more than one output

T1 Input  read orders.yxdb (yxdb)  — Orders extract
T2 Select  CUSTOMER:String(10); STATUS → ORDER_STATUS; other fields kept  ← T1  — Shorten CUSTOMER, rename STATUS
T3 Filter  [REGION] != "WEST"  ← T2   (T → T4, F → T8)  — Exclude WEST

## Output T7 · sales_summary.yxdb

Shared tools used: T1, T2, T3
T4 Formula  AMOUNT:Double = ToNumber([AMOUNT_TXT]); NET:Double = Round([AMOUNT] * IIF([QTY] >= 10, 0.9, 1), 0.01); SIZE_BAND:V_String(10) = IF [AMOUNT] >= 1000 THEN "LARGE" ELSEIF [AMOUNT] >= 100 THEN "MEDIUM" ELSE "SMALL" ENDIF  ← T3.T  — AMOUNT, NET, SIZE_BAND
T5 Summarize  group by REGION, SIZE_BAND; Sum(NET) as TOTAL_NET; Count(ORDER_ID) as ORDERS; CountNonNull(AMOUNT) as PRICED_ORDERS; Max(ORDER_DATE) as LAST_ORDER  ← T4  — Totals by region and band
T6 Sort  TOTAL_NET desc, REGION asc  ← T5  — Biggest totals first
T7 Output  write sales_summary.yxdb (yxdb), mode overwrite  ← T6  — sales_summary.yxdb

## Output T8 · excluded_orders.csv

Shared tools used: T1, T2, T3
T8 Output  write excluded_orders.csv (csv), mode overwrite  ← T3.F  — excluded_orders.csv
