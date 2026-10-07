# wf_0007 — translation notes

One assumption per line. Nothing below has been checked against a real Alteryx engine or a real Snowflake account; every claim is against `docs/reference/simulator-semantics.md` (the parity oracle) and the objective.

- Both outputs total the same rows, so they are built once, in the temporary table `attainment`; `REGION_ATTAINMENT` is a `CREATE OR REPLACE TABLE` over it and `ATTAINMENT_HISTORY` a `MERGE` from it.
- Tool 3 is the J anchor only, an inner join on `=`: a NULL key matches nothing, and a target is paired with each of its actual lines, so its `TARGET` is added once per line, as Alteryx does; a pre-aggregation of the actuals would disagree.
- `User.CurrentYear` is the text constant `'2026'` and tool 4 is `LEFT(PERIOD, 4) = '2026'`, so a malformed period such as `2026-1` is kept, as in Alteryx.
- `Sum` skips NULLs and an all-NULL group is NULL, never 0; `LINES` is `COUNT(*)` because Alteryx's `Count` counts rows; the totals add exact `NUMBER(19,2)` values.
- `ATTAINMENT_HISTORY` is merged on exactly `REGION, PERIOD`: every row the run does not produce stays, prior years included; a NULL update key would match nothing and be inserted, which the inner join never produces here.

No lint flag is raised by `scripts/compile_check.py`, so there is no `lint <kind> <name>: kept because ...` line to write.
