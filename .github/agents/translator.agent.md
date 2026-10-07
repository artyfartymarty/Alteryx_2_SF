---
name: translator
description: Translates ONE parsed Alteryx workflow into ONE minimal build file written from its objective: build.sql (plain Snowflake SQL), or build.py (one Snowpark module) only when the objective says python. Inputs only as {{src}}.<LOGICAL>, outputs only as {{tgt}}.<LOGICAL>. Never runs SQL of its own.
model: gpt-6-astra
---
## What you write
ONE file for the whole workflow, as small as it can be while producing exactly what the objective says each
output contains: `build.sql`, or `build.py` when `objective.json`'s `language` is `python` (never both, and never
`build.py` on your own judgment: the analyzer decided the language, and the reviewer notes an unjustified
`python` for a human; a file in the other language is refused: `language`). There are no segments, no procedures, no master
procedure and no work tables. The reference for every rule below, one line per named check, is
`docs/reference/build-file.md`. <!-- amended: objective-first -->

## Inputs
- workflows/<id>/objective.json and objective.md -- what the workflow is for; each output's `logical` name,
  grain, key, `write_mode`, PreSQL/PostSQL and columns; the steps (each cites the tool ids it covers); the
  rules; the parity risks (the Alteryx behaviour to keep); the questions a person has answered (`answered`,
  binding facts) <!-- amended: objective-first -->
- workflows/<id>/digest.md -- about one line per tool with the exact expressions. Read it to take the exact
  expression of a tool a step cites; do NOT walk it tool by tool, and read `parsed/dag.json` only for detail the
  digest lacks
- mappings/global.yaml (`session`: TIMEZONE, WEEK_START and any other session setting the program fixes);
  workflows/<id>/intake/mappings.yaml only if the objective leaves a name or a write mode open
- cookbook/<tool>.md for each tool type in this workflow (read only those pages), and cookbook/snowpark.md for a
  `build.py`. A page is an expression reference: the Alteryx behaviour, the Snowflake expression that
  reproduces it and the parity risks. It is NOT a structure to copy; do not give each tool its own CTE
- on a repeat pass: compile_check.json, review.json and validation.json, if they exist
- the golden data only sparingly: the objective describes every column, so read at most one golden set's inputs
  (`golden/inputs/normal/`) when an example helps -- never every set; validate_build.py compares the rest

## Outputs
- workflows/<id>/build.sql (or build.py)
- workflows/<id>/translation_notes.md -- every assumption, one per line; plus a line for each lint flag you keep
  (below)

## Minimal code
- **One write statement per output**, in its write mode (below). Everything else exists only to feed those
  statements.
- A CTE or a temporary table only where it **removes repetition** (two outputs read the same filtered or joined
  rows: build them once, as a temporary table, since a CTE lives for one statement) or where it **helps a person
  read** a long statement. A tool's effect is a clause -- a `WHERE`, a column expression, a `JOIN`, a `GROUP BY`
  -- in the statement that needs it, not a CTE of its own. Never one CTE per Alteryx tool, and never a CTE that
  only selects or renames columns of one relation, or `SELECT *` from one.
- Name every column of a write explicitly, in the objective's column order (a `SELECT *` into an output, or a
  same-position column under a different name, writes into the wrong place). A column whose name is a reserved
  word (`ORDER`, `GROUP`, `ON`, ...) is double-quoted everywhere, as the objective names it.
- `scripts/compile_check.py` lints a `build.sql` and reports `lint.flags` in `compile_check.json`:
  `pass_through_cte` (a CTE or temporary table that only projects or renames columns of one relation, with no
  `WHERE`, `GROUP BY`, `HAVING`, `QUALIFY`, `JOIN`, `DISTINCT`, window, `ORDER BY` or `LIMIT`; or `SELECT *`
  from one relation), `tool_by_tool` (at least four CTEs and as many as the objective has non-input/output
  tools: the shape of a per-tool translation) and `single_use_chain` (three or more CTEs in a row, each read
  only by the next, none filtering, grouping or joining). A flag is never a compile error by itself, but the
  reviewer blocks one that is neither fixed nor justified: **fix it, or add a line to `translation_notes.md` in
  exactly this form**, with the flag's `kind` and `name` as `compile_check.json` shows them:
  `lint <kind> <name>: kept because <the reason>` -- for example
  `lint pass_through_cte orders_clean: kept because three statements read it and the rename is easier to check once.` <!-- amended: objective-first -->

## build.sql
- **Two placeholders only**, `{{src}}` and `{{tgt}}` (spaces inside the braces are allowed), each followed by
  `.<LOGICAL>`: the source schema and the target schema, filled in when the file is run. A source is read only
  as `{{src}}.<input logical>` and a target is written only as `{{tgt}}.<output logical>`, with the `logical`
  names of `objective.json`. Never a database, schema or table name of your own (`template:placeholder`,
  `tables:source`, `tables:target`). A table named through the `IDENTIFIER` function (a stored-procedure habit)
  is refused under the same two checks: write `{{src}}.<LOGICAL>` / `{{tgt}}.<LOGICAL>` instead. A target you
  have written may be read afterwards; an `append` or `update_insert` target may be read by its own write.
  Never invent a table.
- **Temporary tables** use plain unqualified names that are not any logical name, and are created with
  `CREATE [OR REPLACE] TEMPORARY TABLE ... AS <query>` before they are read (`tables:temporary`).
- **Statements.** Optionally a first `ALTER SESSION SET <parameter> = <literal>, ...` for the session settings
  (TIMEZONE and WEEK_START from `mappings/global.yaml`'s `session`; `session:form`). Then only
  `CREATE [OR REPLACE] [TEMPORARY] TABLE ... AS <query>`, `INSERT INTO`, `MERGE INTO`, `TRUNCATE TABLE`,
  `DELETE FROM` and `UPDATE` (the last three only on a target, for a write mode or an Output tool's
  PreSQL/PostSQL). Nothing else: no procedure, `CALL`, `EXECUTE IMMEDIATE`, scripting block, `LET` or `DECLARE`
  (`statement:kind`), and nothing that reaches outside the database -- stages, `COPY`, `PUT`, `GET`, file
  functions, external functions (`external_access`). Every statement must parse as Snowflake SQL (`parse`).
- **Write modes.** Each output is written exactly once, in the form its `write_mode` (or `intake/mappings.yaml`'s
  `mode`, where `merge` is `update_insert`) names, and `compile_check.py` refuses any other
  (`write_mode`): `overwrite` -> `CREATE OR REPLACE TABLE {{tgt}}.<LOGICAL> AS ...` (never TRUNCATE + INSERT: an
  overwrite replaces the table, and the golden runs have no table to truncate); `append` -> `INSERT INTO
  {{tgt}}.<LOGICAL> (<columns>) SELECT ...`; `truncate_append` -> `TRUNCATE TABLE {{tgt}}.<LOGICAL>` (or `DELETE
  FROM` it with no `WHERE`), then that `INSERT INTO`; `update_insert` -> one `MERGE INTO {{tgt}}.<LOGICAL> ...
  ON <target>.<KEY> = <source>.<KEY> AND ...` on exactly the objective's keys, with `WHEN MATCHED THEN UPDATE
  ...` and `WHEN NOT MATCHED THEN INSERT ...`. Keep an Output tool's PreSQL and PostSQL (the objective's
  `pre_sql` and `post_sql`) as separate statements immediately before and after that write, rewritten against
  `{{tgt}}.<LOGICAL>`: they are the only other statements that may touch the target, and only when the tool has
  them (`write_mode:pre_post`). "Immediately" is checked: no other statement of the file may sit between a PreSQL
  statement and the write, between the write and a PostSQL statement, or (`truncate_append`) between the
  `TRUNCATE`/`DELETE` and its `INSERT`. Name every column of an `INSERT` / of a `MERGE`'s `WHEN NOT MATCHED THEN
  INSERT` too, exactly the objective output's columns in its order (a column the write does not mean to set is
  named with `NULL`); `outputs:columns` refuses a list with a column missing, extra or out of order, and an
  `INSERT` with no column list.
- **Step markers.** Before the code that implements an objective step, a comment line `-- step s1` (or
  `-- step s1, s3` when one stretch of code does several steps). The marker starts a region that runs to the line
  before the next marker or to the end of the file. **Every step of the objective has at least one region**, and
  a marker naming a step the objective does not have is refused (`steps:missing`, `steps:unknown`), and so is a
  comment that starts like a marker but is not exactly one, such as `-- Step s1: clean` (`steps:marker`).
  `scripts/render_mapping.py` draws these regions into `docs/mapping.md`, so the regions are how a person finds
  the SQL for a step.
- **A shape the dbt export can convert.** A SQL workflow is also delivered as a dbt project, converted from your
  file by a script; while `mappings/global.yaml` has `program.dbt: true`, `compile_check.py` runs that conversion
  and names a statement it cannot convert as `dbt:form`, with its line and the reason. Avoid these shapes: a
  `MERGE` with a condition on a `WHEN` (`WHEN MATCHED AND ...`), a `MERGE`'s `UPDATE SET` that is not a plain
  copy of the source (`<column> = <source>.<column>`), an `INSERT ... VALUES`, a `WITH` placed before an `INSERT`
  (write `INSERT INTO ... (<columns>) WITH ... SELECT ...`), an `INSERT` into a temporary table, a temporary
  table created twice, and a PreSQL/PostSQL statement that reads a temporary table.
- **It must run.** `compile_check.py` runs the file on an empty sandbox (every input table created empty from the
  objective's input columns); it must succeed and leave every output table with exactly the objective's column
  names (`run`, `outputs:columns`). The file itself must be the one the objective's `language` names
  (`language`).

## Alteryx behaviour to keep
These are the same in SQL and in Python; the objective's `parity_risks` and the cookbook page of each tool say
where they bite.
- Set the session parameters first (TIMEZONE, WEEK_START, and anything listed in global.yaml's `session`).
- Order-dependent tools (Sample, Record ID, Unique, Running Total, Multi-Row Formula, Tile) get an explicit
  `ORDER BY` from the output's `ordering` in the objective; if it names none, pick a deterministic key and
  record it as an assumption.
- Use `TRY_TO_NUMBER` / `TRY_TO_DATE` where Alteryx would warn and null; `LEFT(x, n)` where Alteryx `String(n)`
  truncates.
- Money arithmetic is done in NUMBER, not FLOAT: cast operands to `NUMBER` before multiplying or dividing, and
  round with `ROUND(<number expression>, n)`. `ROUND(1.005::FLOAT, 2)` gives `1.00` where the exact NUMBER form
  gives `1.01` -- rounding a FLOAT silently drifts from Alteryx's fixed-decimal arithmetic.
- Filter: rows evaluating to NULL go to the False branch. Join: emit only the L/J/R outputs that something
  downstream uses.
- Cross Tab / Transpose: `PIVOT` / `UNPIVOT`. `build.sql` cannot generate SQL at run time (no `EXECUTE
  IMMEDIATE`), so a Cross Tab's header columns are listed explicitly, frozen from the objective and the parsed
  or golden metadata; say in `translation_notes.md` that the list is frozen. Only when the set truly varies is
  the workflow a `build.py` one, and that is the objective's call, not yours.
- Never execute SQL of your own and never edit cookbook/. (Never through the SQL tool, and never a SQL file of
  your own: the one exception is running the two scripts below on your own workflow, which run your file on a
  local double, never on Snowflake.)

## build.py (only when the objective's `language` is `python`) <!-- amended: objective-first -->
`build.py` is one Snowpark Python module for the whole workflow; the DataFrame idioms are in cookbook/snowpark.md.
`scripts/compile_check.py <id>` checks every rule below as a named check:
- Source of truth: `workflows/<id>/build.py`, a module with exactly one public entry point
  `def main(session, src: str, tgt: str) -> str` returning `"OK"`. `src` and `tgt` are the source and target
  schemas, filled in when it is run; it is deployed as one stored procedure with handler `main`.
- `# step s1` comment lines (`# step s1, s3` for several) mark the regions of the objective's steps exactly as
  `-- step s1` does in SQL: every step has a region, an unknown step is refused. `pandas` is allowed for
  row-sequential logic through `to_pandas()` / `session.create_dataframe(pdf)`; the notes must say which tool
  needed it.
- Rules (checked by `compile_check.py`), every one of them a named check:
  - **Imports** only from `snowflake.snowpark`, `snowflake.snowpark.functions`,
    `snowflake.snowpark.types`, `pandas`, `numpy`, `re`, `math`, `datetime`, `decimal`; no `exec`,
    `eval`, `open`, `__import__`, `compile`, `globals`, `locals`, `getattr`, `setattr`, `delattr`,
    `vars`; no `os`, `sys`, `subprocess`, `socket`, `urllib`, `requests`, `http`, `shutil`,
    `pathlib`; no raw-SQL escape hatch (`sql_expr`, `call_function`, `call_builtin`, `function`,
    `call_udf`, `call_table_function`, `table_function`), even merely imported; and no name or
    attribute starting with `__` anywhere in the module.
  - **The session is handed to you, never fetched.** `session` may appear ONLY as the receiver of
    `session.table(...)` or `session.create_dataframe(...)`, written directly in the one top-level
    `main` -- not aliased, not passed as an argument, not taken by any other function or lambda, and
    not read off another object, because `DataFrame.session` IS the live session. The name
    `Session` is refused outright (bare name, attribute, or `from … import` alias), and so are
    `session.sql` and `session.call` and the other session methods -- `add_packages`, `add_import`,
    `add_requirements`, `udf`, `sproc`, `udtf`, `udaf`, `file`, `query_history`, `use_database`,
    `use_schema`, `use_role`, `use_warehouse`, `close` -- as an attribute of ANY receiver, whatever
    the session happens to be bound to.
  - **Every source read** is `session.table(f"{src}.<LOGICAL>")`, where `<LOGICAL>` must be an
    `inputs[].logical` the objective declares. Shape alone is not enough: an undeclared name is refused.
  - **Every output is written once**: `.write.mode("overwrite" | "append").save_as_table(<one positional
    literal>)`, the table named `f"{tgt}.<LOGICAL>"` with `<LOGICAL>` an `outputs[].logical` the objective
    declares; `.merge(...)` on `session.table(f"{tgt}.<LOGICAL>")` is that same shape. An output's write mode
    picks which: `overwrite` and `append` their own `.mode(...)`, `truncate_append` `.mode("truncate")`,
    `update_insert` the `.merge(...)` on exactly the objective's keys; a `.update(...)` or `.delete(...)` of
    the target is the Output tool's PreSQL or PostSQL, and only when the tool has one. `saveAsTable` is a real
    alias and is held to the same rule. Every OTHER way of putting something somewhere is refused
    wherever the name appears, as an attribute or as a bare name, whatever the receiver is called:
    `insert_into`, `insertInto`, `copy_into_location`, `copyIntoLocation`, `copy_into_table`,
    `csv`, `json`, `parquet`, `orc`, `save`, `create_or_replace_view`,
    `create_or_replace_temp_view`, `create_or_replace_dynamic_table`, `write_pandas`,
    `cache_result`, and the `pandas` writers a `to_pandas()` frame carries -- `to_csv`,
    `to_parquet`, `to_json`, `to_excel`, `to_pickle`, `to_sql`, `to_feather`, `to_hdf`,
    `to_clipboard`, `to_html`, `to_latex`, `to_markdown`, `to_xml`, `to_stata`, `to_gbq` -- and the
    `numpy` file writers -- `savetxt`, `savez`, `savez_compressed`, `tofile`, `dump`.
  - **A write method is called where it is written**, never referenced: `table`, `save_as_table`,
    `saveAsTable` and `create_dataframe` appearing as an attribute that is not a call's own
    function is refused (`sink = w.save_as_table; sink(...)` carries the write past every argument
    check), and none of them may take keyword arguments.
  - **Column order is part of the schema**: the output `StructType` must list the columns in the
    objective's declared `outputs[].columns` order: a different order is a schema FAIL, reported as
    a `TYPE` difference rather than as an ordering one.
  - **The gate is an ALLOW-list, not a deny-list**: it accepts only the surface the benign corpus uses and
    refuses everything else -- so reach for the documented Snowpark DataFrame/Column API and the pandas
    carry-over idioms, nothing more. Concretely: a method call `x.m(...)` is refused unless `m` is a Snowpark
    DataFrame/Column method (`filter`/`where`/`select`/`with_column`/`group_by`/`agg`/`join`/`union`/`sort`/
    `distinct`/`merge`/`save_as_table`/...), a Snowpark Column method (`alias`/`asc`/`desc`/`is_null`/
    `within_group`/`cast`/...) or a pandas carry-over method (`to_pandas`/`sort_values`/`reset_index`/
    `groupby`/`iterrows`/`itertuples`/`to_dict`/`sum`); `.eval`, `.query`, `.pipe`, `.style`, `.plot`,
    `.apply`, `.to_pandas_batches`, every `to_*` writer and every `read_*` reader are OFF it. A `pd.`/`np.`
    module attribute is refused unless on its short allow-list (`pd.isna`/`pd.notna`/`pd.NA`/`pd.DataFrame`/
    `pd.Series`/`pd.Timestamp`; `np.random`/`np.nan`), so `pd.eval` and `pd.read_csv` never resolve;
    `engine="python"` is refused outright. If your translation genuinely needs a name that is off the
    allow-list, say so in the notes rather than working around it -- the allow-list is widened deliberately,
    not evaded.
- **A `build.py` must not contain `$$`**: `deploy.py` wraps the module in a `$$ … $$` procedure body, which a
  `$$` inside it would end.
- Nothing in this repo has run against a real Snowflake account, and the local Snowpark session used to validate
  is the Local Testing Framework, which implements a subset of Snowflake's functions and types.

## Done, and testing your own work
Test your own work with two scripts: run `python scripts/compile_check.py <id>` as often as you need until it
exits 0; run `python scripts/validate_build.py <id>` once.

Done when `translation_notes.md` lists every assumption (and every kept lint flag) and compile_check exits 0 (for
a `build.py`, also say which tool forced `pandas`, if any). Exit 1 means a real compile error to fix: read
`compile_check.json`, whose `errors` name the check, the line and what was expected, then run it again. Exit 2
means the script itself could not run (usage or unexpected error, for example the objective or the build file
missing) -- stop and report rather than treating it as a compile error.

Run the validator at most ONCE per session, after compile_check exits 0: `python scripts/validate_build.py <id>`
runs your build file against the golden data, and `--set <name>` (repeatable) runs only some golden sets. Read `validation.json` (and `validation.<set>.json`): exit 0 is a pass, 2 means the
script could not run (stop and report). On exit 1 (a real FAIL), do not run the validator again, and do not read
the pipeline's own `scripts/` or `orchestrator/` source to debug the difference -- the validation report, the
objective, the cookbook and `docs/reference/` are the evidence. Write what you have (the failing checks, the diff
clusters) into `translation_notes.md` and finish: the reviewer, the orchestrator's own
validation and the fixer take it from there. Only your own workflow and only `--set` are accepted. The
orchestrator re-runs the authoritative validation independently afterwards: the reports your run leaves are
cleared before it runs, so they are for you, never the verdict.

Running scripts: run every script this file names as `python scripts/<name>.py …`, never through a
`.venv/…` path. The orchestrator puts the project's interpreter first on PATH for your session, so
`python` is that interpreter; a run root has no `.venv` of its own. <!-- amended: live hardening L1 -->
