# wf_0006 — intake plan

Subscription revenue recognition · owner `wf_owner` · schedule `0 6 * * 1-5` ·
source `subscription_revenue.yxmd`, `yxmdVer 2023.1`, E1 engine.

This plan restates `parsed/dag.json`, `intake/touchpoints.json` and `mappings/global.yaml`.
Nothing here has run on Alteryx or on Snowflake, and no count, diff or tolerance in this project
comes from an agent — those come only from `scripts/`.

## Tier

**T2 — one tool needs Snowpark.** Four of the five tools are ones the cookbook has a SQL
pattern for: Input Data, Filter, Summarize and Output Data. Tool 3 is a **Python tool**, and there
is no SQL pattern for one: the embedded script *is* the specification. `plugin_map.TARGET_CLASS`
classifies a `python` node as `snowpark`, so the workflow is written as one Snowpark `build.py`
rather than a `build.sql`.

Nothing needs a human rewrite, so the status does not go to `NEEDS_HUMAN`. T2 means "not SQL alone", not "not migratable".

## Touchpoints and how they resolve

| Tool | Touchpoint | Resolution |
|------|-----------|------------|
| 1 | `C:\data\billing\subscriptions.yxdb` (key `billing/subscriptions.yxdb`) | `BILLING.RAW.SUBSCRIPTIONS`, logical `SUBSCRIPTIONS` — confirmed by the owner |
| 5 | `C:\data\out\revenue_by_period.yxdb` (key `out/revenue_by_period.yxdb`) | `ANALYTICS.CURATED.REVENUE_BY_PERIOD`, logical `REVENUE_BY_PERIOD`, write mode `overwrite` — confirmed |

There are no constants, no app parameters and no macros. The one Output tool is a file target, so
it has no PreSQL, no PostSQL and no update keys, and no prior state to merge into — which is why
this workflow has no `targets_before` golden data.

The Python tool's script is **not** a touchpoint: it is configuration that the parser carries
through into `config.script`, and the migration translates it rather than asking about it.

Program-level answers all come from `mappings/global.yaml` and none had to be asked again: target
`ANALYTICS.CURATED`, work schema `MIG_WORK`, warehouse `MIG_WH`, owning role `MIGRATION_ROLE`,
`EXECUTE AS CALLER`, column-name policy `sanitize`, session `TIMEZONE America/New_York` and
`WEEK_START 1`, accepted diff classes `ROUNDING` and `ORDERING`, and Snowpark runtime `3.11` — the last of
which matters here for the first time in this repository, because it is the `RUNTIME_VERSION` of
the stored procedure `scripts/deploy.py` prints for a `build.py`.

## Expected shape

One build file, `build.py`, writing one output (`REVENUE_BY_PERIOD`). The Filter before the Python tool and the
Summarize after it are written in the same module as the tool's loop: the objective says why the file is Python
(the deferred balance carries from one row to the next, which no window function expresses).

## Unsupported or manual tools

None. The Python tool is supported — in a `build.py`, not a `build.sql`.

## Parity risks by tool id

| Tool | Risk | Why it matters |
|------|------|----------------|
| 2 | `NULL > 0` is not true | A period with no billed amount leaves on the Filter's False anchor, which is wired to nothing, so it is dropped |
| 2 | The comparison is strict | A period billed exactly 0 is dropped too, not kept as zero revenue |
| 3 | The deferred balance carries between rows | The result depends on the order rows are processed in; the script sorts by `CUSTOMER, PERIOD` itself, and that pair has to be unique for the sort to be total |
| 3 | A cancellation resets the balance **before** the cap applies | The single most likely thing to get wrong when re-reading this loop, and it moves `RECOGNIZED`, not only `DEFERRED` |
| 3 | `groupby` drops NULL keys | A NULL `CUSTOMER` vanishes rather than forming its own schedule — a silent row loss, in Alteryx as much as in the translation |
| 3 | `to_pandas()` materialises the whole stream | Safe only because the workflow's input is bounded (`expected_rows`, `large: false`); this is the translation's single scaling assumption |
| 4 | Alteryx's `Count` counts rows | `CUSTOMERS` is `COUNT(*)`, not `COUNT(CUSTOMER)` |
| 4 | `Sum` over a `Double` is exact-decimal in the oracle | The addition goes through `NUMBER(38,10)` rather than accumulating in `FLOAT` |
| 5 | Write mode `overwrite` | The target is replaced wholesale and holds no prior state |

## Fix-loop budget

Three iterations. The risk is concentrated in tool 3, and not where a reader expects: the loop is
short and the arithmetic is obvious, but the cancellation reset sits two lines above the
arithmetic and is easy to drop, and the sort that makes the whole thing deterministic is easy to
mistake for tidiness. `broken/01_cancellation_reset_ignored.py` is the first of those
mistakes, made on purpose, so that the parity run is shown catching it.

## Dependencies, owner and consumers

No macros, no shared sub-workflows, no upstream workflow producing `subscriptions.yxdb`.
`manifest.json` records no consumers for `revenue_by_period.yxdb`. Owner `wf_owner`; the schedule
`0 6 * * 1-5` is the Alteryx Server schedule recorded at parse time.
