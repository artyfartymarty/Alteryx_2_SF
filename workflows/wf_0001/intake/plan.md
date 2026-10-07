# wf_0001 — intake plan

Sales summary by region and size band · owner `wf_owner` · schedule `0 6 * * 1-5` ·
source `sales_summary.yxmd`, `yxmdVer 2023.1`, AMP engine.

This plan restates `parsed/dag.json`, `intake/touchpoints.json` and `mappings/global.yaml`.
Nothing here has run on Alteryx or on Snowflake, and no count, diff or tolerance in this project
comes from an agent — those come only from `scripts/`.

## Tier

**T1 — pure SQL.** Every tool in the workflow is one the cookbook has a SQL pattern for: Input
Data, Select, Filter, Formula, Summarize, Sort and two Output Data tools. Nothing needs Snowpark
(no Python, R, Download, Run Command or spatial tool) and nothing needs a human rewrite (no
unknown plugin, no macro, no dynamic rename or dynamic input). The Formula tool uses `ToNumber`,
`Round` and an `IF/ELSEIF` band, all of which §8.4 maps directly.

## Touchpoints and how they resolve

| Tool | Touchpoint | Resolution |
|------|-----------|------------|
| 1 | `C:\data\sales\orders.yxdb` (key `sales/orders.yxdb`) | `SALES.RAW.ORDERS`, logical `ORDERS` — confirmed by the owner |
| 7 | `C:\data\out\sales_summary.yxdb` (key `out/sales_summary.yxdb`) | `ANALYTICS.CURATED.SALES_SUMMARY`, logical `SALES_SUMMARY`, write mode `overwrite` — confirmed |
| 8 | `C:\data\out\excluded_orders.csv` (key `out/excluded_orders.csv`) | `ANALYTICS.CURATED.EXCLUDED_ORDERS`, logical `EXCLUDED_ORDERS`, write mode `overwrite` — confirmed |

There are no constants, no app parameters and no macros. Both Output tools are file targets, so
neither has PreSQL, PostSQL or update keys, and neither has a prior state to merge into.

Program-level answers all come from `mappings/global.yaml` and none had to be asked again: target
`ANALYTICS.CURATED`, work schema `MIG_WORK`, warehouse `MIG_WH`, owning role `MIGRATION_ROLE`,
`EXECUTE AS CALLER`, column-name policy `sanitize`, session `TIMEZONE America/New_York` and
`WEEK_START 1`, accepted diff classes `ROUNDING` and `ORDERING`.

## Expected shape

One build file, `build.sql`, writing both outputs. Tools 1–3 feed both Output tools (the Filter's True branch
leads to `SALES_SUMMARY`, its False branch to `EXCLUDED_ORDERS`), so the file reads and cleans the orders once and
both writes start from that. The objective divides the work into steps for the mapping diagram; the steps are not
separate units of deployment.

## Unsupported or manual tools

None.

## Parity risks by tool id

| Tool | Risk | Why it matters |
|------|------|----------------|
| 2 | Fixed-width `String(10)` on `CUSTOMER` | Alteryx truncates silently; Snowflake's `VARCHAR(10)` would raise, so the translation needs `LEFT(…, 10)` |
| 3 | Filter sends NULL evaluations to the **False** anchor | `[REGION] != "WEST"` is NULL for a NULL region, and those rows belong to Output tool 8 |
| 4 | `ToNumber` warn-and-null | Text that is not a plain decimal — including one with a thousands separator, and the empty string — must become NULL, not an error |
| 4 | `Round(x, 0.01)` half away from zero | Money arithmetic has to be done in `NUMBER`, not `FLOAT`, or the cent drifts from the model |
| 4 | NULL in a comparison is not false | A NULL `AMOUNT` makes both band comparisons NULL, so the row takes the `ELSE` branch |
| 5 | `Count` vs `CountNonNull` | `Count` counts rows and `CountNonNull` counts values; they differ on every NULL `AMOUNT` |
| 5 | `Sum` over a `Double` | The model adds exact decimals and stores once; a binary accumulation can differ |
| 6 | NULL placement in a sort | NULL sorts first ascending and last descending, so the `ORDER BY` has to say so |
| 7, 8 | Write mode `overwrite` | Both targets are replaced wholesale, so neither needs a prior state and neither is order-sensitive |

## Fix-loop budget

Two iterations. The workflow is small and every risk above has a cookbook pattern, so the expected
outcome is a first pass that validates; the budget exists for the rounding and NULL-semantics
risks, which are the two that a first pass most often gets wrong.

## Dependencies, owner and consumers

No shared macros and no upstream workflow: tool 1's extract is produced outside the Alteryx
estate. `manifest.json` records no consumers for either output. Owner `wf_owner`; the schedule
`0 6 * * 1-5` is the Alteryx Server schedule recorded at parse time.
