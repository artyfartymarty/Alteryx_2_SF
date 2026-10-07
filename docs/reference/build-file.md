# The build file

A workflow's deliverable is **one file**: `workflows/<id>/build.sql` (plain Snowflake SQL), or `build.py` (one
Snowpark module) when the objective's `language` is `python` and says why. The translator writes it from
`objective.json`; `scripts/compile_check.py <id>` checks it; `scripts/validate_build.py <id>` runs it against the
golden data; `scripts/render_mapping.py <id>` draws its step regions into `docs/mapping.md`. There are no segment
procedures, no master procedure and no work tables; a SQL workflow is also exported as a dbt project (below, "dbt
export"), converted from the validated file by a script. Design: `docs/superpowers/specs/2026-10-06-objective-first-design.md`
§4.4 to §4.6.

Nothing here has run on a real Snowflake account or in Alteryx. A file that passes `compile_check.py` was accepted
by sqlglot's Snowflake parser and run on DuckDB (SQL), or by the Snowpark static rules and a Snowpark Local Testing
run (Python): that is the whole claim.

## `build.sql`

```sql
-- wf_0001 · Sales summary by region and size band
-- Written from objective.md; checked against golden data. {{src}} = source schema, {{tgt}} = target schema.
ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;

-- step s1, s3
CREATE OR REPLACE TEMPORARY TABLE clean_orders AS
SELECT LEFT(CUSTOMER, 10) AS CUSTOMER, ..., TRY_TO_DOUBLE(AMOUNT_TXT) AS AMOUNT
FROM {{src}}.ORDERS;

-- step s2, s3, s4
CREATE OR REPLACE TABLE {{tgt}}.SALES_SUMMARY AS
SELECT ... FROM clean_orders WHERE REGION <> 'WEST' GROUP BY ... ORDER BY ...;
```

`{{src}}` and `{{tgt}}` are the source and target schemas, rendered by Snowflake's own templating when the file is
run (`EXECUTE IMMEDIATE FROM @<stage>/build.sql USING (src => '<DB>.<SCHEMA>', tgt => '<DB>.<SCHEMA>')`; that form
is unverified, and `deploy.py --render` writes a `build.rendered.sql` with the names filled in for an account where
it does not run). The source schema is the view schema `gen_source_views.py` makes, so the file never names a
production table.

Each error in `compile_check.json` is one line: the check name, `: `, the line of the build file it is about (when
there is one) and what was expected. The checks, in the order they are reported:

| check | what it holds |
|---|---|
| `template:placeholder` | The only placeholders are `{{src}}` and `{{tgt}}` (spaces inside the braces are fine, a `{{` must close), and in code each is followed by `.<LOGICAL>`. A comment may name a placeholder bare. |
| `session:form` | An optional **first** statement `ALTER SESSION SET <parameter> = <literal>, ...`; the parameter is `TIMEZONE`, `WEEK_START` or a key of `mappings/global.yaml`'s `session` block, the value a quoted string, a number or `TRUE`/`FALSE`. The run records it, it does not apply it. |
| `external_access` | Nothing reaches a file, a stage, the network, an extension or the engine's settings: `COPY`, `PUT`/`GET`, `ATTACH`, `INSTALL`/`LOAD`, `PRAGMA`, `SET`, `CREATE STAGE`/`SECRET`/`... INTEGRATION`/`FILE FORMAT`, file/stage/network functions, a table that is a file or a stage. Such a file is never run. |
| `statement:kind` | Only `CREATE [OR REPLACE] [TEMPORARY] TABLE ... AS <query>`, `INSERT INTO`, `MERGE INTO`, and `TRUNCATE TABLE` / `DELETE FROM` / `UPDATE` on a `{{tgt}}.<LOGICAL>` target (for a write mode or an Output tool's PreSQL/PostSQL). No procedure, `CALL`, `EXECUTE IMMEDIATE`, scripting block, `LET`, `DECLARE`, view, transient table, bare `SELECT`. |
| `parse` | Every statement parses with sqlglot's Snowflake dialect and is not a raw `Command` fallback. |
| `tables:source` | A source is read only as `{{src}}.<input logical>` (a logical the objective declares), never written, never spelled with a schema of your own, and never named through `IDENTIFIER(...)`: write `{{src}}.<LOGICAL>` / `{{tgt}}.<LOGICAL>`, not `IDENTIFIER(...)`. |
| `tables:target` | A target is written only as `{{tgt}}.<output logical>` (never through `IDENTIFIER(...)`); it may be read after the statement that writes it (an `append`/`update_insert` target also by its own write, any target by a `DELETE`/`UPDATE` of it), not before. |
| `tables:temporary` | A temporary table has a plain unqualified name that is not a logical name, made with `CREATE [OR REPLACE] TEMPORARY TABLE ... AS` before anything reads it. |
| `write_mode` | Each output is written exactly once in its write mode's form: `overwrite` one `CREATE OR REPLACE TABLE {{tgt}}.X AS ...`; `append` one `INSERT INTO`; `truncate_append` a `TRUNCATE`/`DELETE FROM` (no `WHERE`) then one `INSERT INTO`, nothing between; `update_insert` one `MERGE INTO` on exactly the objective's keys (`<a>.K = <b>.K` joined by `AND`) with `WHEN MATCHED THEN UPDATE` and `WHEN NOT MATCHED THEN INSERT`. |
| `write_mode:pre_post` | Any other statement on the target is the Output tool's PreSQL (**immediately** before the write) or PostSQL (**immediately** after it): allowed only when the objective's output has one, required when it does, and no other statement of the file may sit between it and the write. A `truncate_append`'s `TRUNCATE`/`DELETE` is held the same way to its `INSERT` (that one is a `write_mode` error). |
| `steps:missing` | Every objective step has at least one region. |
| `steps:unknown` | A marker naming a step the objective does not have. |
| `steps:marker` | A comment that starts like a marker (`-- Step s1: clean`, `-- step s1 s2`, `-- step s01`) but is not one. |
| `run` | The file runs on an empty sandbox: each input table is created empty from the objective's input columns (and each `append`/`truncate_append`/`update_insert` target, or target whose Output tool has a PreSQL, too), every statement runs, nothing needs a file. |
| `outputs:columns` | Afterwards every output table exists and has exactly the objective's column names, in order, compared case-insensitively. The run cannot see which columns an `append`/`truncate_append`/`update_insert` write names (the target exists beforehand with the objective's columns), so the statement's own column list is held to the same names, in the same order: an `INSERT INTO {{tgt}}.X (...)` and a `MERGE`'s `WHEN NOT MATCHED THEN INSERT (...)`; a column the write does not mean to set is named with `NULL`, an `INSERT` with no column list is refused, and the error names the missing, extra and out-of-order columns. |
| `dbt:form` | While `mappings/global.yaml` has `program.dbt: true` (the default) and the objective's language is `sql`, the build must be one `export_dbt.py` can convert (below, "The conversion"): the check runs that conversion in memory, writes nothing, and names a statement with no dbt form by its line and the reason (`dbt:form: line 14: no dbt form for ...`). It sees what the checks above accept: a `MERGE` with a condition on a `WHEN`, a `MERGE`'s `UPDATE SET` that is not a copy of the source (`<column> = <source>.<column>`), an `INSERT ... VALUES`, a `WITH` before an `INSERT` (write `INSERT INTO ... (<columns>) WITH ... SELECT ...`), an `INSERT` into a temporary table, a temporary table created twice, a PreSQL/PostSQL statement that reads a temporary table. Avoid those shapes. It is checked only once the static checks, the write forms and the column lists are clean, and not at all when `program.dbt` is false. |
| `language` | The build file is the one the objective's `language` names (`build.sql` for `sql`, `build.py` for `python`); a `build.py` for a `sql` objective (or the reverse) is exit 1; both files present is a usage error, exit 2. |

A step marker is a comment line `-- step s1` or `-- step s1, s3` and nothing else on the line; the region it starts
runs to the line before the next marker (or the end of the file). Ids are the objective's (`s1`, `s2`, ...), a step
may appear in several markers, and code before the first marker belongs to no step.

When a static check (`template:placeholder`, `session:form`, `external_access`, `statement:kind`, `parse`,
`tables:*`) fails, nothing is run: the run would only repeat the problem. `write_mode*` and `steps:*` are reported
beside the run's own result, and `dbt:form` after it, only when the static checks and the write forms are clean.

## `build.py`

One Snowpark module: `def main(session, src: str, tgt: str) -> str`, returning `"OK"`; `src` and `tgt` are the
schemas `{{src}}` and `{{tgt}}` stand for. It reads `session.table(f"{src}.<LOGICAL>")`, writes
`<df>.write.mode("overwrite" | "append" | "truncate").save_as_table(f"{tgt}.<LOGICAL>")` (or `.merge(...)` on
`session.table(f"{tgt}.<LOGICAL>")` for `update_insert`), and marks steps with `# step s1` comment lines. `deploy.py`
wraps it in one Python stored procedure with handler `main`.

Checks: the step checks above (with `#` comments), then

| check | what it holds |
|---|---|
| `python:dollar_quote` | No `$$` anywhere in the file: `deploy.py` wraps the module in `$$ ... $$`. |
| `rule:syntax`, `rule:imports`, `rule:signature`, `rule:session_scope`, `rule:table_names`, `rule:write_mode`, `rule:no_io`, `rule:no_raw_sql`, `rule:no_session_sql` | The Snowpark static rules of `lib/snowpark_rules.py` (the same allow-listed surface as ever: imports, names and methods refused, `session` only as the receiver of `session.table(...)` / `session.create_dataframe(...)` in `main`, one literal-or-f-string table name that is a logical the objective declares, each output written once in its write mode). Written `rule:<name>: line N: <expectation>`. |
| `run`, `outputs:columns` | `main` runs in the runtime sandbox (`lib/snowpark_sandbox.py`, a child process under an audit hook) on empty input tables, and every output table it wrote has the objective's column names in order. The Local Testing Framework cannot `filter` an empty table, so a run that raises is repeated once on one row of NULLs per input; the second run's answer stands. Whether the build copes with genuinely empty data is `validate_build.py`'s question, on the `empty` golden set. |

## `compile_check.json`

```json
{ "ok": false,
  "errors": [ "write_mode: line 14: SALES_SUMMARY is overwrite: write it with CREATE OR REPLACE TABLE {{tgt}}.SALES_SUMMARY AS ...; found INSERT" ],
  "lint": { "lines": 31, "statements": 4, "ctes": 1,
            "flags": [ { "kind": "pass_through_cte", "name": "t2", "line": 9,
                         "note": "only selects or renames columns of one relation; inline it" } ] } }
```

`lint` is computed for a `build.sql` only (a `build.py` report has no `lint` key) and never changes `ok` or the exit
code. Its flags (`lib/minimality.py`):

| kind | what it flags |
|---|---|
| `pass_through_cte` | A CTE or temporary table whose query only selects or renames bare columns of one relation (or `SELECT *` from one): no `WHERE`, `GROUP BY`, `HAVING`, `QUALIFY`, `JOIN`, `DISTINCT`, window, `ORDER BY`, `LIMIT`, set operation or expression. Inline it. |
| `tool_by_tool` | At least four CTEs (temporary tables count) and as many as the objective has tools that are neither an input nor an output: the shape of a per-tool translation. Reported at the first CTE. |
| `single_use_chain` | Three or more CTEs (or temporary tables) in a row, each read only by the next, none filtering, grouping or joining. Reported at the first of them. |

The translator fixes a flag or adds a line `lint <kind> <name>: kept because <the reason>` to `translation_notes.md`
(`<kind>` and `<name>` as the report shows them); the reviewer blocks a flag that is neither.

Exit codes of `compile_check.py`: **0** every check passed (`ok`), **1** a check failed (the report says which),
**2** nothing to check: no `objective.json`, an unreadable one, tier `T3`, an output or input with no `logical`
name, an output with no write mode (or `update_insert` with no keys), no build file, or both `build.sql` and
`build.py`.

## Validation: `validate_build.py`

`python scripts/validate_build.py <wf_id> [--set NAME]... [--root .] [--backend duckdb|snowflake] [--connection NAME]
[--sandbox-database DB] [--sandbox-timeout SECONDS]` runs the one build file against the golden data and compares every
output of the objective with its golden file. Nothing here has run on Snowflake or in Alteryx: locally, a PASS means
DuckDB (or the Snowpark Local Testing Framework) agrees with the Alteryx simulator (or with a real Alteryx capture,
when one was imported), which is the whole claim.

**One golden set** (default: every set in `manifest.json`'s `golden_sets`, in that order; `--set` names some):

1. a fresh in-memory DuckDB; each source from `intake/mappings.yaml` is loaded from `golden/inputs/<set>/<tool_id>.csv`
   as a view by its logical name under `MIGDB.MIG_GOLDEN_<WF>_<SET>`, and each `golden/targets_before/<set>/<LOGICAL>.csv`
   as `MIGDB.MIG_WORK.<LOGICAL>` (the state an `append`, `truncate_append` or `update_insert` output starts from);
2. `{{src}}` is rendered to that view schema and `{{tgt}}` to `MIGDB.MIG_WORK`, and every statement is run in order (the
   `ALTER SESSION` is recorded, not applied, on the double; DuckDB's external access is locked first). A `build.py` runs
   `main(session, src, tgt)` the same way in the runtime sandbox, after the Snowpark static rules;
3. each objective output is compared with `golden/outputs/<set>/<tool_id>.csv` by `compare.py`, given the objective as the
   contract it reads (`lib/objective.py: contract_view`: the output's keys, `row_relation`, `ordering`, `tolerances` and
   `normalizations`). The set's verdict is the worst output verdict, `needs_human` if any output says so, and every diff
   cluster names its output in `stream`. The `keys_not_unique` and `nullability_contract` advisories and the
   update-insert caveat are `compare.py`'s and come through unchanged.

There is no golden data for anything between an input and a final output: the golden data is the inputs and the
outputs only.

**Idempotency.** Every set's build is run twice, each time from scratch in its own fresh backend (or sandbox
child); the re-run loads every input and every `targets_before` table in reversed row order, so a build whose
result depends on the order its rows arrive in (a `LIMIT` without `ORDER BY`, a window without a total order, a row
number taken from the input order) gives a different answer, on whichever set shows it. It is idempotent when every
output table holds the same rows, as a multiset, both times. That catches `RANDOM()`, a clock and a dependence on
input row order without being fooled by an `append` (running twice without a reset is never the test). A set that is
not idempotent FAILs, with an `idempotency_diff` naming the output tables that differ. Each set's report carries its own
`idempotent` and `idempotency_diff`; `validation.json`'s `idempotent` is the AND over the sets.

**Reports.** `workflows/<id>/validation.<set>.json` per set and `validation.json` for the workflow (the worst verdict,
the body of the first set with that verdict, `needs_human` if any set says so, `sets` listing every set's own verdict).
A report is `{golden_set, language, verdict, checks, diff_clusters, normalizations_applied, idempotent,
idempotency_diff, runtime_ms, credits, needs_human, truncated}`, plus `error` when the build itself failed. Both are
deleted before anything else runs, so a report on disk always belongs to the latest invocation and a usage error leaves
none.

| what happened | result |
|---|---|
| every output matches in every set | `PASS`, exit 0 (`PASS_WITH_ACCEPTED_DIFF`, exit 0, when a human approved the class over those columns in `manifest.json`'s `accepted_diffs` -- an entry with no `segment` -- and `mappings/global.yaml`'s `accepted_diff_classes` lists it) |
| an output differs | `FAIL`, exit 1: the clusters name the class, the columns and the output |
| the build does not compile or run, or never creates an output | that set is `FAIL` with an `error` (the statement's line, or the table's name), exit 1 |
| the build is the wrong language, or a `build.py` breaks a Snowpark rule | every set `FAIL` with the reason, nothing is run, exit 1 |
| the two runs of a set differ | that set is `FAIL`, `idempotent: false` (and so is the top-level `idempotent`) |
| no objective, tier `T3`, no build file (or both), no `intake/mappings.yaml`, an unknown set, a missing golden file, a name that is not an identifier | exit 2, nothing written |

**The `empty` golden set and a `build.py`.** The Snowpark Local Testing Framework cannot `filter` an empty table or
`Cast` inside an aggregate, so a Python build that has to cope with no rows filters in pandas (as
`samples/wf_0006/canned/build.py` does) and builds its output from a list. The `empty` set is run like any other and
compared in full, nothing is skipped or loosened for it.

**`--backend snowflake`** is for a person with an account: each run gets a fresh sandbox in a database that
`orchestrator.config.json`'s `policy.sandboxDatabases` lists, reached through a named connection; a `build.sql` is run
statement by statement (the `ALTER SESSION` included) and a `build.py` in a Snowpark session built from the
connection. Never run against a real account; the tests drive it through `tests/fake_snowflake.py`.

`samples/<wf>/broken/` holds one whole-file broken build per mistake (`01_<what>.sql`, or `.py` for a Python workflow)
and a `broken.json` naming, for each, the golden set to validate and the cluster to expect
(`expect: {class, columns, output, scope}`), whether `compile_check.py` passes it (`compiles`) and, for the one build
whose result is not repeatable, `deterministic: false`. The orchestrator's mock serves the first file in name order
for its fix-loop scenario, so the first file of every sample compiles.

## dbt export: `export_dbt.py` and `validate_dbt.py`

A SQL workflow is also delivered as a small dbt project, `workflows/<id>/dbt/`. It is not translated a second time:
`python scripts/export_dbt.py <wf_id> [--root .]` converts the validated `build.sql`, statement by statement, and
`python scripts/validate_dbt.py <wf_id> [--set NAME]... [--root .]` runs the result on the same golden data. Neither
is an agent's job. The orchestrator runs both after `render_mapping.py`: exit 1 of either parks translate with the
reason `dbt: <the first line of its stderr>`, exit 2 is a `script-error`, and two zeros end the loop at `VALIDATED`.
Design: decision D9 and §4.10 of `docs/superpowers/specs/2026-10-06-objective-first-design.md`. Nothing here has run
on Snowflake: a project that passes `validate_dbt.py` was run by `dbt-duckdb` on DuckDB, no more.

The export is on by default (`mappings/global.yaml`, `program.dbt: true`). It is **skipped** -- exit 0, a line saying
why, nothing written and any old `dbt/` removed -- when `program.dbt` is false, when the objective's `language` is
`python` (a Python workflow stays one Snowpark procedure) and when its tier is `T3` (there is no build file).

### The project

```
workflows/<id>/dbt/
  dbt_project.yml        name mig_<id>, model-paths [models], models: +transient: false and, when the build has
                         an ALTER SESSION, a Snowflake-guarded +pre-hook of every model
  profiles.yml           a local DuckDB target (what validate_dbt.py runs on) and a COMMENTED Snowflake template
  README.md              what each model is, how to run it, that nothing has run on Snowflake
  models/sources.yml     source "<id>": one table per objective input, schema var('src_schema')
  models/schema.yml      one entry per output: the grain, each column's meaning, not_null for every NOT NULL column,
                         unique for a one-column key (both `severity: warn`, see "Validation" below)
  models/<logical>.sql   one per output           models/<temp>.sql   one per temporary table (ephemeral)
```

`+transient: false` is there because `dbt-snowflake` creates transient tables unless told otherwise, and the
build's `CREATE TABLE` is a permanent one. `profiles.yml` holds placeholders only, never a credential, and its
Snowflake target stays commented out.

The build's `ALTER SESSION` (`TIMEZONE`, `WEEK_START`: parity-critical) is a **project-level `+pre-hook` of the
models**, `{% if target.type == 'snowflake' %}ALTER SESSION SET ...{% endif %}`, so that it runs on each model's own
connection, before the model's SQL. It is not an `on-run-start`: dbt runs the start hooks on its "master"
connection and each model on a worker-thread connection that is opened and released separately (also with
`threads: 1`), so a session parameter set there never reaches a model. The guard renders to nothing on the local
DuckDB target, which has no sessions (`validate_build.py` records the statement and does not apply it, for the same
reason). A model's own PreSQL `pre_hook` runs after the project's: dbt appends the model's `config()` list to the
project's (dbt-core 1.12, `dbt/context/context_config.py` `calculate_node_config` applies the project's configs
first and the `config()` call after; `dbt_common/contracts/config/base.py` `_merge_field_value`, `MergeBehavior.Append`,
is `self + other`), and `tests/test_dbt_project.py` checks the order by running dbt. Everything in `dbt/` is written by
the exporter: change `build.sql` and export again. The output is deterministic, so a re-run is byte-identical.

An `incremental` model (append and merge) carries `full_refresh=false`, so `dbt build --full-refresh` cannot rebuild
the table the build relies on for its history. A hook that reads a temporary table has no dbt form (dbt injects an
ephemeral model's CTE into a model's SQL, never into a hook); `README.md` of the project names dbt-core 1.8 as the
minimum (`data_tests:`).

### The conversion

| `build.sql` statement | model |
|---|---|
| `CREATE OR REPLACE TEMPORARY TABLE t AS <query>` | `models/t.sql`, `materialized='ephemeral'`, body `<query>` |
| `CREATE OR REPLACE TABLE {{tgt}}.X AS <query>` (`overwrite`) | `models/x.sql`, `materialized='table'`, `alias='X'` |
| `INSERT INTO {{tgt}}.X (cols) <query>` (`append`) | `materialized='incremental'`, `incremental_strategy='append'`, `full_refresh=false` |
| `TRUNCATE TABLE {{tgt}}.X` (or `DELETE FROM` with no `WHERE`) then `INSERT INTO` (`truncate_append`) | `materialized='table'`: the table is replaced on every run |
| `MERGE INTO {{tgt}}.X USING (<query>) s ON <keys> ...` (`update_insert`) | `materialized='incremental'`, `incremental_strategy='merge'`, `full_refresh=false`, `unique_key` the **objective's** keys, `merge_update_columns` the columns of the `UPDATE SET` |
| an Output tool's PreSQL or PostSQL around a write | that model's `pre_hook` or `post_hook`, the target as `{{ this }}` |

In every body `{{src}}.X` becomes `{{ source('<id>', 'X') }}`, a temporary table `t` becomes `{{ ref('t') }}`, and a
target `{{tgt}}.Y` read after the statement that writes it becomes `{{ ref('y') }}` (a read of its own table is
`{{ this }}`). The body is the build's own SQL text with those substitutions -- not a re-generated rendering -- so a
model reads like the build file; the comments above each statement are kept, and the step markers its statements sit
under are one `-- step s1, s3` line at the top. Two places need more than a cut of the text, both because dbt writes
the rows, not the build:

- **A positional `INSERT`.** dbt inserts by name from the model's result. When the query's own output names are not
  exactly the `INSERT`'s column list (a constant, an expression with no alias), the body is
  `WITH q (a, b) AS (<query>) SELECT * FROM q`.
- **A `MERGE` that fills a column with something of its own.** dbt's `merge` inserts every column of the table and
  updates only `merge_update_columns`. A column the `WHEN NOT MATCHED THEN INSERT` gives a value other than
  `S.<column>` (the `NULL` of `LOADED_FLAG` in `wf_0003`, which its PostSQL fills later) is projected by the model:
  `SELECT S.*, NULL AS LOADED_FLAG FROM (<query>) AS S` (`S.* EXCLUDE (...)` when the query also has it). An
  `UPDATE SET` may only copy columns of the source, and a `USING` that is a table is `SELECT * FROM {{ ref('t') }}`.

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    full_refresh=false,
    unique_key=['ACCT', 'PERIOD'],
    merge_update_columns=['TOTAL', 'CLOSING_BAL', 'ENTRIES', 'HAS_PERIOD_END'],
    alias='GL_SUMMARY',
    pre_hook="DELETE FROM {{ this }} WHERE PERIOD < '2020-01'",
    post_hook="UPDATE {{ this }} SET LOADED_FLAG = 'Y' WHERE LOADED_FLAG IS NULL"
) }}
-- step s3, s4
-- Update; Insert if new, by name, on ACCT and PERIOD; LOADED_FLAG is left alone on a match ...
SELECT S.*, NULL AS LOADED_FLAG
FROM (
SELECT ACCT, PERIOD, ... FROM {{ ref('latest_entries') }} ...
) AS S
```

A statement with no row in the table is **exit 1**, naming its line: `build.sql line 14: no dbt form for ...`. These are the
ones: a `MERGE` with a condition on a `WHEN`, a `DELETE` branch, an `UPDATE SET` that is not a copy of the source, or
an `ON` that is not exactly the objective's keys; an `INSERT ... VALUES`; an `INSERT` into a temporary table; a
`CREATE TABLE` that is neither a target nor `TEMPORARY`; a temporary table created twice; a table named without
`{{src}}`/`{{tgt}}` or through `IDENTIFIER(...)`; a PreSQL/PostSQL statement that reads a temporary table (it becomes
a hook, and dbt gives a hook no CTE for an ephemeral model); a target read before it is written; a write in another form than
the objective's write mode names; an output the build never writes; a name that is not a plain identifier. Any old
`dbt/` is removed on exit 1, so a project that does not match the build is never left behind. Exit 2 is a usage error
(no objective or build file, both build files, an objective that cannot say what the export needs, a
`program.dbt` that is not true or false); it leaves an old `dbt/` in place, because nothing was learned about the build.

### Validation: `validate_dbt.py`

For each golden set (default: every one in `manifest.json`'s `golden_sets`): a fresh on-disk DuckDB sandbox
`workflows/<id>/dbt_sandbox_<set>.duckdb` (git-ignored) is loaded exactly as `validate_build.py` loads one
(`load_golden.load_set`); `dbt build` runs on it through `lib/dbt_project.run_dbt` -- the models, then the tests of
`models/schema.yml` -- with `--vars` naming the golden view schema as `src_schema` and `MIGDB.MIG_WORK` as
`tgt_schema`; each output model's table is read back and compared with its golden file through `compare.py`
(`contract_view`, as in `validate_build.py`). A set's verdict is the worst output verdict. Every set is run a
second time in a fresh sandbox loaded with every input in reversed row order, and the set is idempotent when
every output holds the same rows both times (the top-level `idempotent` is the AND over the sets). The dbt log is
kept in `dbt/logs/validate_<set>.log` (git-ignored).

The report is `validate_build.py`'s (`{golden_set, language, verdict, checks, diff_clusters,
normalizations_applied, idempotent, idempotency_diff, runtime_ms, credits, needs_human, truncated}` plus `error`),
in `dbt/validation.<set>.json` and `dbt/validation.json`; stale ones are deleted before anything else runs.

- **The generated tests are warnings.** `models/schema.yml` states the objective's contract with `not_null` (every
  NOT NULL column) and `unique` (a one-column key), each with `config: {severity: warn}`. `compare.py` already treats
  a golden NULL in a NOT NULL column (`nullability_contract`) and a key that repeats in the golden rows
  (`keys_not_unique`) as advisories that `validate_build.py` passes, so a dbt test must not fail what
  `validate_build.py` passes: dbt reports these as warnings and exits 0. `validate_dbt.py` does not count a `warn`
  status as a failure, and names every test that warned in `advisories` (one `{kind: "dbt_test_warned", test,
  golden_set, message}` each; on a set's report only when there is one, and on `validation.json` over all the sets)
  and as `advisory:` lines on stdout. A test with status `fail` or `error` would still be a `FAIL`.
- **The files stay Snowflake SQL; the run is on a translated copy.** `dbt-duckdb` hands a model's SQL to DuckDB
  as written, and DuckDB has no `IFF` or `TRY_TO_NUMBER`. `validate_build.py` runs the same SQL through
  `DuckDBBackend`, which translates it with sqlglot, so this runs a temporary copy of the project in which each
  model's body and hooks are translated the same way, with the `{{ ref() }}`, `{{ source() }}` and `{{ this }}`
  left as they are. The configs, tests, sources and profile are the delivered files.
- **It is the delivered project that is judged.** The files on disk must be exactly what `export_dbt.py` writes from
  `build.sql` now: a file edited by hand, a missing or an extra one, or a build changed after the export, is a
  `FAIL` naming it, and dbt never starts.
- **A dbt failure is a `FAIL`, not a crash.** `dbt build` exiting non-zero (a model that does not run, a failed
  test that failed rather than warned) FAILs that set with an `error` naming the failed models and tests and the first
  message; so does a build with no dbt form. Exit 1 prints the first problem on stderr as one line, which is what the orchestrator records.
- **Skipped** where the export is (exit 0, nothing written). Exit 2: no objective, no `dbt/` project, no
  `intake/mappings.yaml`, no golden sets, an unknown set, a missing golden file, no dbt beside the interpreter. There is
  no `--backend snowflake`: nothing in this repository connects to Snowflake, and a person runs the command that
  `deploy.py <id> --database DB --schema SCHEMA --dbt` prints, with their own profile.

## What the local doubles do not prove

- **The Snowpark Local Testing Framework** (the double a `build.py` is run on): it implements a *subset* of Snowflake's
  SQL functions and types, has no `session.sql`, resolves no real `RUNTIME_VERSION` or `PACKAGES` and represents no
  performance characteristic. A PASS means the logic matched the golden data in that subset, not that the procedure
  `deploy.py` prints will create, let alone run, on your account. The framework runs `build.py` as ordinary Python on
  the host, so the agent's code only ever runs in a sandbox whose two gates are **allow-lists**: the static gate
  (`lib/snowpark_rules.py`) accepts only the Snowpark/pandas surface the benign corpus uses (imports from a fixed list,
  `pd.`/`np.` attributes from a per-module list, method names from the Snowpark DataFrame/Column plus pandas
  carry-over list, no `engine="python"`), and the module then runs in a **child process** under an audit hook
  (`lib/snowpark_sandbox.py`) armed before it is imported, default-deny: it allows exactly the audit events a benign
  build records (`lib/sandbox_events.json`, re-recorded by `scripts/dev/record_sandbox_events.py` from the canned
  `build.py` and the cookbook's Snowpark examples) with argument checks that keep file access inside a per-run temp
  directory, and refuses every other event. The child runs under a wall-clock timeout (`--sandbox-timeout`, default
  600 s, its process tree killed on expiry), in a minimal environment with no credentials, and signs its result with a
  per-run nonce the parent verifies. A blocked operation, a timeout or an unauthenticated result is that set's FAIL,
  naming the reason. This is **defence in depth, not a security boundary**: agent code still shares the harness's
  Python process, so it depends on the static gate; real isolation is the program spec's container
  (`docs/production-backlog.md`, "Isolate agent-code execution").
- **DuckDB** (the double a `build.sql` is run on): DuckDB types and identifier casing differ from Snowflake's, and
  `VARCHAR(n)` length is not enforced the way a real account enforces it.
- **The Alteryx simulator** is a model of Alteryx's tools, not the tools (`docs/reference/simulator-semantics.md`).
- **"Idempotent" compares two *independent* runs, not a run repeated against persisted state.** `validate_build.py`
  runs the build twice per golden set from the same inputs (the second time in reversed row order), each in a fresh
  backend or sandbox, and compares the outputs as row multisets. A build that writes `append` where it meant `overwrite` is therefore `idempotent: true` here, although a
  second real run against a persistent target would double the table.
