# wf_0006 — translation notes

One assumption per line. Nothing below has been checked against a real Alteryx engine or a real Snowflake account; every claim is against `docs/reference/simulator-semantics.md` (the parity oracle) and the objective.

- The workflow is Python because tool 3's loop carries a deferred balance from one row to the next inside a customer: `min(carried + billed, cap)` does not distribute over a running sum, so no window function expresses it. Tool 3 is the tool that forced `pandas`.
- Tool 2's Filter is `[BILLED] > 0`, strict: a `BILLED` of exactly 0 goes too, and a NULL `BILLED` is dropped as Alteryx's unwired False anchor drops it. It is done on the pandas frame, because the Local Testing Framework cannot filter an empty table.
- Tool 3 sorts by `CUSTOMER, PERIOD` itself, as its script does; a cancellation resets the deferred balance to 0 before that period's own cap applies, and `RECOGNIZED` and `DEFERRED` are rounded with Python's `round`, not Alteryx's `Round`.
- A NULL `CANCELLED` raises (`bool(pd.NA)` does), as the script does, where the framework's `None` would quietly be false; `groupby` drops a NULL `CUSTOMER`, in Alteryx as much as here.
- `to_pandas()` materialises the whole stream, which is safe only because the input is bounded; this is the translation's single scaling assumption.
- A NULL `CAP` that survives the Filter is not guarded: `float(NULL)` is `nan` on both engines and `min(x, nan)` returns `x`, so the cap is silently not applied, identically in Alteryx; it is a data-quality risk in the source, not a translation risk.
- Tool 4 sums a `Double` to a `Double` and counts rows (`CUSTOMERS` is `count(lit(1))`, not a count of customers' values); no sort is applied, the target being keyed on `PERIOD`.
- The output is written with `.write.mode("overwrite").save_as_table(f"{tgt}.REVENUE_BY_PERIOD")`, its column order that of the objective.

No lint flag is raised by `scripts/compile_check.py`, so there is no `lint <kind> <name>: kept because ...` line to write.
