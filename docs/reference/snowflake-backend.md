# The Snowflake backend: validating and deploying on a real account

**Nothing in this repository has run against a real Snowflake account.** Everything on this page is
code with fake-backed tests: `tests/fake_snowflake.py` is a DuckDB-backed stand-in patched in as
`snowflake.connector` (plus a Snowpark session double built on the Local Testing Framework), so the
tests prove which statements the pipeline sends, in which order, through which connection — and
that those statements compute on the DuckDB double what the local path computes. What a real
account does with them is not proven; §7 lists every piece that has never been exercised. The
first run on the company's account is the test.

`validate_build.py` keeps DuckDB as its default. `--backend snowflake` is selected only explicitly and
is run by a **human**: the orchestrator never passes a Snowflake option to anything it runs or asks
an agent to run, and nothing Snowflake is imported or constructed on the default path (the fake-backed
tests pin that the DuckDB default is unchanged). The orchestrator's
policy denies an agent `--root`, `--backend`, `--connection` and `--sandbox-database` in every
script call, argparse abbreviations included (`script-root` / `script-backend` in
`orchestrator/policy.ts`; the production procedure is `docs/handoff-production.md` §2).

## 1. Named connections only

The pipeline reaches an account through the **name** of an entry in your own `connections.toml` and
nothing else: `snowflake.connector.connect(connection_name=NAME)` and
`Session.builder.config("connection_name", NAME).create()` (`scripts/lib/snowflake_conn.py`). The
name comes from `--connection NAME`, else the environment variable `MIG_SNOWFLAKE_CONNECTION`, else
the run stops with a usage error (exit 2) before anything connects. The connector's own
default-connection setting is deliberately not consulted: which account a run touches is always an
explicit choice.

- No password, token or private key is ever accepted as an argument: every script's argparse
  refuses `--password` (and any other option it does not define), and `SnowflakeBackend(password=…)`
  raises `passwords are never passed as arguments; use a named connection`. Any connection
  parameter other than the name is refused the same way.
- Nothing credential-shaped is ever written to this repository, a report or a log. Every error text
  that comes back from the connector or a Snowpark session goes through
  `snowflake_conn.redact` before it reaches `validation*.json` or stderr, and a wrapped
  error is never chained to the unredacted original. After a credential key (`password`, `passwd`,
  `pwd`, `passcode`, `passphrase`, `token`, `secret`, `private_key…`, written `k=v`, `k: v` or
  `"k": "v"`) a quoted value is masked up to its closing quote and an unquoted one to the END OF ITS
  LINE — a passphrase or a key path may contain spaces, and a connector message may carry anything
  after it; other lines are untouched. PEM private-key blocks are removed.
- Every object name the pipeline builds from workflow data — a mapping's `logical`, a tool id, a
  golden set, an output's logical name — must be a plain upper-case identifier (`[A-Z_][A-Z0-9_$]*`, the
  rule intake already applies to a `logical`); `validate_build.py` checks all of them before it opens a
  backend on either engine, and refuses anything else as a usage error (exit 2) that names the value,
  so no such value can rewrite a statement.

Your `connections.toml` lives on your machine, outside every repository (the connector reads
`~/.snowflake/connections.toml` by default, or the directory `SNOWFLAKE_HOME` names — see the
connector's documentation for your OS). Use key-pair or SSO authentication; never a password in any
file. Two illustrative entries (names and values are placeholders):

```toml
[migration_sandbox]                  # key pair: no prompt, suitable for repeated validation runs
account = "<org>-<account>"
user = "<SERVICE_USER>"
role = "MIGRATION_VALIDATOR"
warehouse = "MIGRATION_WH"
authenticator = "SNOWFLAKE_JWT"
private_key_file = "C:\\Users\\<you>\\.snowflake\\keys\\migration_rsa_key.p8"

[migration_deploy_sso]               # SSO through the browser
account = "<org>-<account>"
user = "<you>@<company>"
role = "MIGRATION_DEPLOYER"
warehouse = "MIGRATION_WH"
authenticator = "externalbrowser"
```

A validation opens one connection per fresh sandbox (two per golden set: the judged run and the
idempotency re-run) and, for a `build.py`, one Snowpark session per run; with SSO, check the connector's
ID-token caching before a long run so it does not prompt every time.

## 2. The sandbox

A validation on `--backend snowflake` runs in ONE sandbox database you name with
`--sandbox-database DB` (or `MIG_SANDBOX_DATABASE`). `scripts/lib/snowflake_sandbox.py`'s
`SnowflakeSandbox.fresh()` opens a connection by name, runs `USE DATABASE DB`, and replaces the
run's schemas with `CREATE OR REPLACE SCHEMA`:

| Schema | Holds |
|---|---|
| `DB.MIG_GOLDEN` | the raw golden inputs, `<WF>_<SET>_IN_<tool id>` |
| `DB.MIG_GOLDEN_<WF>_<SET>` | the golden view schema: one view per source under its logical name (what `{{src}}` is rendered as) |
| `DB.MIG_WORK` | the build's targets, `{{tgt}}` (with their `targets_before` state) |
| `DB.MIG_COMPARE` | each golden output, loaded for `compare.py` |

`CREATE OR REPLACE SCHEMA` **destroys** whatever those schemas held. So a database is accepted as a
sandbox only when `orchestrator.config.json` (read from `--root`) lists it under
`policy.sandboxDatabases`; any other name — or a name that is not a plain upper-case identifier, or
no `orchestrator.config.json` at all — is a usage error (exit 2) raised before any connection is
opened. Add your sandbox database to that list before the first run. (The same list also bounds the
databases the orchestrator lets an agent's SQL reference.)

**One validation at a time per sandbox database.** `MIG_WORK` (and every other sandbox schema) is
shared by everything that runs in the database, and each fresh sandbox replaces them — nothing
locks the database, so two validations in the same one destroy each other's state. Use a second
sandbox database (listed in the policy) to run two at once.

The judged run and the idempotency re-run of each golden set share the sandbox database, so
`validate_build.py` snapshots the first run's outputs (`lib.validation.snapshot`) before the second fresh
sandbox replaces them, and compares the two as row multisets exactly as the local path does.

## 3. What `validate_build.py` does on `--backend snowflake`

The report is the local report: the same `compare.py` (called once per objective output), the same
keys, `credits` still `null` (no credit usage is read back). This is the old per-segment path with the
whole workflow as its unit (objective-first design §4.8); none of it has run on an account.

- **A `build.sql`** — per run a fresh sandbox; the golden set loaded by
  `load_golden.load_set(…, database=DB)`; then the file is rendered with `{{src}}` =
  `DB.MIG_GOLDEN_<WF>_<SET>` and `{{tgt}}` = `DB.MIG_WORK`, split into statements by
  `lib/build_sql.py`, and every statement is run through `SnowflakeBackend` in file order, the
  `ALTER SESSION` included, on the one connection (so a `CREATE TEMPORARY TABLE` is still there for the
  statements after it; an unqualified `CREATE TEMPORARY TABLE` needs a current schema in the sandbox
  database — `PUBLIC` exists by default, and if an administrator dropped it, `USE SCHEMA` one first). A
  failed statement is a domain FAIL (exit 1), as a local error is; every output is then judged in the
  sandbox. Snowflake's own `EXECUTE IMMEDIATE FROM … USING` templating is NOT used
  here: only `deploy.py` exercises it.
- **A `build.py`** — per run a session from `snowflake_conn.snowpark_session(NAME)`; the sandbox schemas
  replaced through that session (`USE DATABASE DB`, `CREATE OR REPLACE SCHEMA …`), the golden set saved
  into it; the module's `main(session, src, tgt)` called with the same two schemas; the outputs read back
  and judged locally with `compare.py`, as the local path does.

**Row order on the idempotency re-run.** The re-run, SQL or Python, loads the raw golden inputs and every
`targets_before` table in **reversed** row order: the one order a validation controls. Locally the double
returns rows in the order they were inserted, so a build that depends on arrival order (a `LIMIT` without
`ORDER BY`, a row number taken from the input order) gives a different answer and fails idempotency. A real
account is weaker: Snowflake promises no scan order either way — it may return the rows in the same order
whatever order they were inserted in. An order-dependent build file that the local re-run catches may pass
here; treat the local result as the order check and the Snowflake run as the engine check.

## 4. `deploy.py`

```bash
.venv/Scripts/python.exe scripts/deploy.py <wf> --database DB --schema SCHEMA [--src-schema S] \
    [--stage STAGE] [--connection NAME] [--execute] [--render] [--dbt] [--root .]
```

Dry run by default: it prints the deployment and connects to nothing. It refuses (exit 1) a
workflow whose `manifest.status.translate` is not `VALIDATED`: the golden-data validation is the gate,
and a deployment never bypasses it.

- **A `build.sql`**: `CREATE STAGE IF NOT EXISTS` for the stage (by default `<DB>.<SCHEMA>.MIG_BUILD_<WF>`;
  `--stage` names another), the `PUT` of `build.sql` to it, then one statement that runs it,
  `EXECUTE IMMEDIATE FROM @<stage>/build.sql USING (src => '<DB>.<SRC_SCHEMA>', tgt => '<DB>.<SCHEMA>')`,
  which fills `{{src}}` and `{{tgt}}` with Snowflake's own templating, and a commented
  `CREATE TASK … AS EXECUTE IMMEDIATE FROM …` carrying the workflow's schedule from
  `manifest.source.server_schedule` when it has one. The source schema is the one
  `scripts/gen_source_views.py` generates (`<target_database>.MIG_SRC_<WF>`: one view per source under
  its logical name); `--src-schema S` fills it in (in `DB`). **That templated form has never run on an
  account.** `--render` also writes `workflows/<wf>/build.rendered.sql`: the same file with `{{src}}` and
  `{{tgt}}` replaced by `<DB>.<SRC_SCHEMA>` and `<DB>.<SCHEMA>`, for an account where the templated form
  does not run.
- **A `build.py`**: one `CREATE OR REPLACE PROCEDURE <SCHEMA>.<WF>_BUILD(SRC STRING, TGT STRING)` with
  `LANGUAGE PYTHON`, a `RUNTIME_VERSION` (from `mappings/global.yaml`'s `program.snowpark_runtime`),
  `PACKAGES` (the Snowpark package and pandas always, numpy when the module imports it), `HANDLER = 'main'`
  and the module as its `$$` body, and the `CALL` that runs it.
- **`--dbt`** (a SQL workflow with a `workflows/<wf>/dbt/` project): prints the one command
  `dbt build --project-dir workflows/<wf>/dbt --profiles-dir <your profiles dir> --target <name> --vars '{"src_schema":
  …, "tgt_schema": …}'` and nothing else. It never connects and has no `--execute`; `--execute`, `--render`,
  `--stage` and `--connection` with it are usage errors, and a workflow with no `dbt/` is refused (exit 1). The
  human runs the command with their own profile and `dbt-snowflake` (neither is in this repository).
- `--execute` sends what the dry run printed through `SnowflakeBackend(connection_name=…)`, stopping at the
  first statement that fails (exit 1, the error redacted); the code path is `scripts/lib/snowflake_conn.py`'s,
  with fake-backed tests only. It never replaces a schema, and nothing is created in
  `MIG_GOLDEN`/`MIG_COMPARE`. The target schema `SCHEMA` must exist before the workflow runs.
  **`--execute` overwrites.** A build writes its outputs in their write modes: an `overwrite` output is a
  `CREATE OR REPLACE TABLE`, an `append` or `update_insert` output changes the rows the table already
  holds, and a Python build's procedure is a `CREATE OR REPLACE`. No history is kept. Review the dry run
  before `--execute`, and keep what it will replace first — e.g. `CREATE TABLE <backup> CLONE
  <DB>.<SCHEMA>.<OUTPUT>` for each output that already exists, and
  `SELECT GET_DDL('PROCEDURE', '<DB>.<SCHEMA>.<WF>_BUILD(VARCHAR, VARCHAR)')` for a Python build.

## 5. The role and the grants

Illustrative names; an administrator adapts them, and none of these grants has been tried on an
account. The validation role owns the schemas it creates in the sandbox database, which gives it CREATE
TABLE / VIEW / PROCEDURE in them.

```sql
-- validation (the sandbox database is listed in orchestrator.config.json policy.sandboxDatabases)
CREATE DATABASE MIGDB_SANDBOX;
CREATE ROLE MIGRATION_VALIDATOR;
GRANT USAGE ON WAREHOUSE MIGRATION_WH TO ROLE MIGRATION_VALIDATOR;
GRANT USAGE, CREATE SCHEMA ON DATABASE MIGDB_SANDBOX TO ROLE MIGRATION_VALIDATOR;

-- deployment and a real run (the role that runs a build needs all of the access it uses)
CREATE ROLE MIGRATION_DEPLOYER;
GRANT USAGE ON WAREHOUSE MIGRATION_WH TO ROLE MIGRATION_DEPLOYER;
GRANT USAGE, CREATE SCHEMA ON DATABASE ANALYTICS TO ROLE MIGRATION_DEPLOYER;      -- MIG_SRC_<WF>
GRANT USAGE, CREATE TABLE ON SCHEMA ANALYTICS.CURATED TO ROLE MIGRATION_DEPLOYER; -- the targets
GRANT CREATE STAGE ON SCHEMA ANALYTICS.CURATED TO ROLE MIGRATION_DEPLOYER;        -- MIG_BUILD_<WF>, build.sql's stage
GRANT USAGE ON DATABASE <SOURCE_DB> TO ROLE MIGRATION_DEPLOYER;                    -- every mapped source
GRANT USAGE ON SCHEMA <SOURCE_DB>.<SOURCE_SCHEMA> TO ROLE MIGRATION_DEPLOYER;
GRANT SELECT ON ALL TABLES IN SCHEMA <SOURCE_DB>.<SOURCE_SCHEMA> TO ROLE MIGRATION_DEPLOYER;
```

On top of those, a `build.sql` needs the stage it is `PUT` to (`deploy.py --execute` creates the default one,
hence `CREATE STAGE` above; a `--stage` the human names must exist or be creatable) and, for the commented Task,
`CREATE TASK` in the target schema and `EXECUTE TASK` on the account; a `build.py` needs
`CREATE PROCEDURE` in the target schema, and the account's Anaconda terms must have been accepted by an
organisation administrator first, since it deploys as a `LANGUAGE PYTHON` procedure with
`PACKAGES = (…)`. If a target already exists and is owned by another role, the role that runs the build
needs ownership (or the privileges the build's `CREATE OR REPLACE TABLE` / `MERGE` / `INSERT` needs) on
it.

## 6. The commands a human runs

```bash
# once: your connections.toml entry (§1); your sandbox database in orchestrator.config.json (§2)
export MIG_SNOWFLAKE_CONNECTION=migration_sandbox MIG_SANDBOX_DATABASE=MIGDB_SANDBOX

# validation on the account (same reports as the local run, written under workflows/<wf>/)
.venv/Scripts/python.exe scripts/validate_build.py <wf> --backend snowflake [--set normal]

# deployment: print, read, then execute through a deploying connection
.venv/Scripts/python.exe scripts/gen_source_views.py <wf>        # the MIG_SRC_<WF> views (DDL text)
.venv/Scripts/python.exe scripts/deploy.py <wf> --database ANALYTICS --schema CURATED --src-schema MIG_SRC_<WF>
.venv/Scripts/python.exe scripts/deploy.py <wf> --database ANALYTICS --schema CURATED --src-schema MIG_SRC_<WF> \
    --connection migration_deploy_sso --execute
```

`--connection NAME` and `--sandbox-database DB` override the two variables per run. Exit codes are
every script's: 0 PASS / done, 1 FAIL / refused, 2 usage error or crash (with a redacted traceback).

## 7. What has never been exercised

Everything below is untested against a real account; the first run will tell.

- **Any connection at all.** `connect(connection_name=…)` and the Snowpark builder have only ever
  been called on fakes; no `connections.toml` has been read by this repository's tests.
- **The build file on Snowflake.** A `build.sql` has only run through `lib/build_sql.py`'s statement
  splitter and DuckDB (the compile check on an empty sandbox, and `validate_build.py`), and its
  statements are parsed with sqlglot's Snowflake dialect. Whether Snowflake accepts each form as
  written — the `ALTER SESSION SET …` first statement, `CREATE OR REPLACE TEMPORARY TABLE … AS`, the
  `MERGE INTO` and `TRUNCATE`/`INSERT` forms — is unverified; the first real-account run (the hand-off's
  verification ladder, rung 3) confirms it.
- **Snowflake's templating.** `EXECUTE IMMEDIATE FROM @<stage>/build.sql USING (src => …, tgt => …)` has
  never run, so it is unknown whether it fills `{{src}}` and `{{tgt}}` as the design assumes, and
  whether it accepts `ALTER SESSION` inside the file, in a Task or outside one. `deploy.py --render` (the
  same file with the names substituted) is the fallback.
- **The Task.** The `CREATE TASK` that `deploy.py` prints is commented and has never been created or
  scheduled.
- **The deployed Snowpark procedure.** `validate_build.py` never CALLs the `LANGUAGE PYTHON` procedure
  `deploy.py` prints: it runs `build.py`'s `main()` in a session. Creating the procedure (runtime version,
  packages, Anaconda terms) is exercised only by `deploy.py --execute`.
- **The dbt project.** A SQL workflow's `dbt/` (`export_dbt.py`; `docs/reference/build-file.md`, "dbt export") has
  run only on DuckDB through `dbt-duckdb`, on a copy of its models translated to DuckDB SQL; `deploy.py --dbt`
  only prints the `dbt build` command, and `dbt-snowflake` is not installed here. Unverified on an account: dbt's
  `merge` and `append` strategies, `ephemeral` models, the hooks, `alias` against an existing upper-case table,
  the model that projects a column the build's `MERGE` fills with a constant, the project-level `+pre-hook` that
  runs the build's `ALTER SESSION` on each model's connection, `full_refresh=false`, an incremental model's first
  run on a missing table, and the
  `not_null` / `unique` tests (the hand-off's rung 3c).
- **Type names.** On the fake, `DESCRIBE TABLE` answers with DuckDB's type names; a real account
  answers `NUMBER(38,0)`, `VARCHAR(16777216)`, `TIMESTAMP_NTZ(9)`, … `compare.py`'s type families
  list Snowflake's names, but no comparison has seen them from a real `DESCRIBE`.
- **Snowpark's `to_pandas()` on a real account.** The read-back of a `build.py`'s outputs was
  written for the Local Testing Framework's pandas quirks; a real session's dtypes (e.g. a scaled
  `NUMBER`) may differ. A read-back that fails is reported as a missing table.
- **Identifier case.** Golden tables are created with quoted column names; a mixed-case golden field
  name would become a case-sensitive column on Snowflake, which an unquoted reference in a
  build file would not find. DuckDB matches case-insensitively and cannot show this.
- **Loading.** Golden rows are inserted with the connector's client-side `executemany` binding
  (pyformat), date and time values as ISO text; no `PUT`/`COPY`, and no measurement of how that
  scales for a large golden set.
- **Missing-table detection.** `SnowflakeBackend.table_exists` treats error 002003 / SQLSTATE `42S02`
  from `DESCRIBE TABLE` as "does not exist"; a missing privilege may surface the same way.
- **Failures mid-run.** Every connector error inside a statement of the build file becomes a domain FAIL
  in the report (as a local SQL error does), so a dropped connection mid-validation reads as a FAIL; read
  the report's `error` before handing it to a fixer.
- **Concurrency.** "One validation at a time per sandbox database" is documented, not enforced.
- **Agents.** The orchestrator's shell policy denies `--backend`, `--connection` and
  `--sandbox-database` (and `--root`) in every agent script call, in every spelling
  (`orchestrator/test/policy.test.ts`, "script-backend: --backend, --connection and
  --sandbox-database are denied in every spelling"). That is proven against the policy function
  only; no live session has tried one.
- **Credits.** Reports keep `credits: null`; nothing reads query history.
- **Order perturbation.** See §3: reversed INSERT order is a best-effort perturbation, weaker than
  the local reversal.
