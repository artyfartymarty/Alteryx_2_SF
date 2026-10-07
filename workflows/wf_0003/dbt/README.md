# wf_0003 — GL period close, as a dbt project

Exported by `scripts/export_dbt.py` from the validated `workflows/wf_0003/build.sql`; it is written by code, never by hand
or by an agent, so change `build.sql` (or the objective) and export again rather than editing these files.
**Nothing in this project has run on a real Snowflake account or a real Alteryx engine.** It has been run only on the
local DuckDB double (`dbt-duckdb`), by `scripts/validate_dbt.py`, against the same golden data `build.sql` was checked
against (`dbt/validation.json`).

| Model | What | Materialization | `build.sql` |
|---|---|---|---|
| `models/latest_entries.sql` | temporary table `latest_entries` | ephemeral (a CTE inside the models that read it) | lines 12-17 |
| `models/gl_summary.sql` | output `GL_SUMMARY` | incremental, merge on ACCT, PERIOD; 1 pre_hook, 1 post_hook | lines 21-55 |

Each model starts with the step markers (`-- step s1, s3`) of the objective's steps it implements, so
`docs/mapping.md` and these files point at the same steps.

## Running it

On Snowflake, with a profile named `mig_wf_0003` in your own profiles directory (the commented `snowflake` target of
`profiles.yml` is a template for it):

```
dbt build --project-dir workflows/wf_0003/dbt --profiles-dir <your profiles dir> --target <name> \
  --vars '{"src_schema": "<SRC_SCHEMA>", "tgt_schema": "<TGT_SCHEMA>"}'
```

`scripts/deploy.py wf_0003 --dbt --database <DB> --schema <TGT_SCHEMA>` prints that command with the schema names filled
in. Run it from the repository root, or give absolute paths. `dbt build` runs the models and the tests of
`models/schema.yml` (`not_null` for every NOT NULL column of the objective, `unique` for a one-column key). Those tests
are `severity: warn`: a NULL in a column the objective calls NOT NULL, or a repeated key, is reported as a warning
(the objective's contract is worth revisiting) and never fails the build.

- `src_schema` is the schema of the source views `scripts/gen_source_views.py` generates (`MIG_SRC_WF0003`, one view per
  logical source) and `tgt_schema` the schema the outputs are written to; both live in the profile's database.
  `dbt_project.yml` declares them as `null`: pass both on every run.
- The build's `ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1` is a project-level `+pre-hook` of every model in `dbt_project.yml`, guarded for Snowflake: it renders to nothing on the local DuckDB double, which has no sessions. It runs on each model's own connection, before the model's SQL and before the model's own PreSQL `pre_hook` (dbt's `on-run-start` would run on a different connection and set nothing for the models).
- An output written as `append` or `update_insert` is an incremental model, and its table must already exist in
  `tgt_schema` with its history; so must the table of any output with a PreSQL (its `pre_hook` runs before the model).
  The first run of an incremental model on a missing table creates it from the model's query. An incremental model
  carries `full_refresh=false`, so `dbt build --full-refresh` leaves it alone: the table holds history that this
  project cannot rebuild from one run's rows.
- It needs dbt-core 1.8 or later (`data_tests:` in `models/schema.yml`).
- Locally, `python scripts/validate_dbt.py wf_0003` builds a fresh DuckDB sandbox per golden set and runs this project on it.
