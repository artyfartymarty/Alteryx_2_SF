# wf_0005 — digest

4 tools · 1 output · from parsed/dag.json (deterministic; never edited by hand)

Each line: `T<id> <Type>  <what it does>  ← <where its rows come from>  (<anchor> → <where they go>)  — <annotation>`. `T3.T` is anchor T of tool 3. Expressions are kept whole.

## Output T4 · accounts_clean.yxdb

T1 Input  read accounts.yxdb (yxdb)  — Vendor accounts
T2 Unknown  plugin AcmeAnalytics.Dedupe.DedupeTool; key_field=ACCT, keep=MaxDate, date_field=UPDATED  ← T1  — Acme Dedupe (third party)
T3 RunCommand  command C:\scripts\notify.bat  ← T2  — Tell the vendor team
T4 Output  write accounts_clean.yxdb (yxdb), mode overwrite  ← T3  — accounts_clean.yxdb
