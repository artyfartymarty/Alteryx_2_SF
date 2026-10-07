# wf_0006 — digest

5 tools · 1 output · from parsed/dag.json (deterministic; never edited by hand)

Each line: `T<id> <Type>  <what it does>  ← <where its rows come from>  (<anchor> → <where they go>)  — <annotation>`. `T3.T` is anchor T of tool 3. Expressions are kept whole.

## Output T5 · revenue_by_period.yxdb

T1 Input  read subscriptions.yxdb (yxdb)  — Subscription billing extract
T2 Filter  [BILLED] > 0  ← T1   (T → T3, F → none)  — Only billed periods
T3 Python  script: import pandas as pd\ndf = Alteryx.read("#1")\ndf = df.sort_values(["CUSTOMER", "PERIOD"]).reset_index(drop=True)\nout = []\nfor customer, group in df.groupby("CUSTOMER", sort=False):\n    deferred = 0.0\n    for _, row in group.iterrows():\n        if bool(row["CANCELLED"]):\n            deferred = 0.0\n        billed = 0.0 if pd.isna(row["BILLED"]) else float(row["BILLED"])\n        recognized = min(billed + deferred, float(row["CAP"]))\n        deferred = billed + deferred - recognized\n        out.append({"CUSTOMER": customer, "PERIOD": row["PERIOD"],\n                    "RECOGNIZED": round(recognized, 2), "DEFERRED": round(deferred, 2)})\nschedule = pd.DataFrame(out, columns=["CUSTOMER", "PERIOD", "RECOGNIZED", "DEFERRED"])\nschedule["RECOGNIZED"] = schedule["RECOGNIZED"].astype("float64")\nschedule["DEFERRED"] = schedule["DEFERRED"].astype("float64")\nAlteryx.write(schedule, 1)  ← T2.T   (1 → T4)  — Revenue schedule with carry-over
T4 Summarize  group by PERIOD; Sum(RECOGNIZED) as TOTAL_RECOGNIZED; Sum(DEFERRED) as TOTAL_DEFERRED; Count(CUSTOMER) as CUSTOMERS  ← T3.1  — Totals by period
T5 Output  write revenue_by_period.yxdb (yxdb), mode overwrite  ← T4  — revenue_by_period.yxdb
