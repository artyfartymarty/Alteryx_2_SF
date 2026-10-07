-- 05_roles.sql — Enterprise governance roles (program spec docs/spec/00-README.md §11.1).
--
-- NOT executed against any Snowflake account. Nothing in this repo has run on real Snowflake;
-- review and adapt (database name, existing role hierarchy, resource monitors, warehouse grants)
-- before running this against a real account.
--
-- An OLDER template: written before `validate_build.py --backend snowflake` and `deploy.py` existed.
-- The roles and grants those two need are in docs/reference/snowflake-backend.md §5.
--
-- <DB> is this program's own sandbox/ops database (holding MIG_WORK, MIG_GOLDEN, OPS and
-- ANALYTICS as schemas) -- fill it in once, consistently, everywhere it appears below; the
-- program spec names no concrete database. MIGRATION_WH is this program's warehouse.

CREATE ROLE IF NOT EXISTS MIGRATION_AGENT;   -- sandbox-only read access, for any agent-facing Snowflake tool (none is configured here)
CREATE ROLE IF NOT EXISTS MIGRATION_CI;      -- deploys the build file, its procedure or its dbt project, from the PR the pipeline opens
CREATE ROLE IF NOT EXISTS MIGRATION_RUN;     -- executes migrated workflows in production

-- ---------------------------------------------------------------------------------------------
-- MIGRATION_AGENT — sandbox only, least privilege, read only: USAGE on the sandbox database, on
-- schemas MIG_WORK/MIG_GOLDEN and on the warehouse, and SELECT on the catalog table below. Nothing
-- on any production database or schema, and no CREATE or DML privilege anywhere: the pipeline's
-- validation runs on local doubles (DuckDB, the Snowpark Local Testing Framework), and the one run
-- on a real account (`validate_build.py --backend snowflake`) is a person's, under the role of
-- docs/reference/snowflake-backend.md §5. No agent session in this repository connects to Snowflake.
-- ---------------------------------------------------------------------------------------------
GRANT USAGE ON DATABASE <DB> TO ROLE MIGRATION_AGENT;
GRANT USAGE ON WAREHOUSE MIGRATION_WH TO ROLE MIGRATION_AGENT;
GRANT USAGE ON SCHEMA MIG_WORK TO ROLE MIGRATION_AGENT;
GRANT USAGE ON SCHEMA MIG_GOLDEN TO ROLE MIGRATION_AGENT;

-- catalog read: the sanitized column catalog described below is a table in MIG_WORK. Read only.
GRANT SELECT ON ALL TABLES IN SCHEMA MIG_WORK TO ROLE MIGRATION_AGENT;
GRANT SELECT ON FUTURE TABLES IN SCHEMA MIG_WORK TO ROLE MIGRATION_AGENT;

-- Intake's source-candidate lookups (program spec §5.3, intake agent step 3: "Query
-- INFORMATION_SCHEMA.COLUMNS ... for tables whose columns cover those fields") must NOT be served
-- by granting MIGRATION_AGENT USAGE on any production database: a database's INFORMATION_SCHEMA is
-- visible to any role with USAGE on it, so that would let the agent enumerate every table and
-- column in that database. An earlier draft of this file granted IMPORTED PRIVILEGES ON DATABASE
-- SNOWFLAKE instead, which is worse: it exposes all of SNOWFLAKE.ACCOUNT_USAGE account-wide
-- (every role's query text, login history, and object metadata) to a role reachable from agent/MCP
-- sessions. Neither is least privilege (program spec §11.1, principle 5). Recommended pattern:
--
-- A separate job -- run by a human, or by MIGRATION_CI, never by an agent session -- publishes a
-- sanitized column catalog (structure only, no row data) into a sandbox table MIGRATION_AGENT
-- has SELECT on via the MIG_WORK grant above. This mirrors this repo's local stand-in,
-- catalog/columns.csv.
--
-- CREATE TABLE MIG_WORK.CATALOG_COLUMNS (
--   SOURCE_DATABASE STRING, SOURCE_SCHEMA STRING, SOURCE_TABLE STRING,
--   COLUMN_NAME STRING, DATA_TYPE STRING, ROW_COUNT NUMBER
-- );
--
-- -- run periodically (by MIGRATION_CI or a human), once per source database being catalogued;
-- -- <SOURCE_DB> is filled in per source -- this is a template, never executed as written:
-- INSERT INTO MIG_WORK.CATALOG_COLUMNS
-- SELECT c.TABLE_CATALOG, c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, t.ROW_COUNT
-- FROM <SOURCE_DB>.INFORMATION_SCHEMA.COLUMNS c
-- JOIN <SOURCE_DB>.INFORMATION_SCHEMA.TABLES t
--   ON t.TABLE_CATALOG = c.TABLE_CATALOG AND t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME;


-- ---------------------------------------------------------------------------------------------
-- MIGRATION_CI — deploys. Owns the DDL delivered through the PR pipeline (schemachange / Snowflake
-- CLI): the build file put on a stage, the stored procedure `deploy.py` prints around a `build.py`,
-- or the dbt project's run, across every environment (program spec §10.1). Never used from an
-- agent session.
-- ---------------------------------------------------------------------------------------------
GRANT USAGE ON DATABASE <DB> TO ROLE MIGRATION_CI;
GRANT USAGE ON WAREHOUSE MIGRATION_WH TO ROLE MIGRATION_CI;
GRANT USAGE ON SCHEMA MIG_WORK TO ROLE MIGRATION_CI;
GRANT USAGE ON SCHEMA MIG_GOLDEN TO ROLE MIGRATION_CI;
GRANT CREATE PROCEDURE, CREATE TABLE, CREATE VIEW ON SCHEMA MIG_WORK TO ROLE MIGRATION_CI;

GRANT USAGE ON SCHEMA OPS TO ROLE MIGRATION_CI;
GRANT CREATE TABLE, CREATE TASK, CREATE ALERT ON SCHEMA OPS TO ROLE MIGRATION_CI;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA OPS TO ROLE MIGRATION_CI;
GRANT SELECT, INSERT ON FUTURE TABLES IN SCHEMA OPS TO ROLE MIGRATION_CI;

GRANT USAGE ON SCHEMA ANALYTICS TO ROLE MIGRATION_CI;
GRANT CREATE TABLE ON SCHEMA ANALYTICS TO ROLE MIGRATION_CI;   -- shadow tables (02_shadow_table_template.sql)

-- Deploying a migrated procedure into its own production target schema (CREATE OR REPLACE
-- PROCEDURE <that schema>.<WF>_<SEG>) happens per workflow, at onboarding, once that workflow's
-- production database/schema is known; those grants travel with that workflow's PR and are not
-- enumerated here, since this file only covers what every workflow needs unconditionally.

-- ---------------------------------------------------------------------------------------------
-- MIGRATION_RUN — executes migrated workflows in production. The procedure `deploy.py` prints around a
-- `build.py` is EXECUTE AS CALLER, and a `build.sql` run with EXECUTE IMMEDIATE FROM runs as the role that
-- issues it; either way MIGRATION_RUN itself needs the privileges on every source and target object a
-- workflow touches -- it does not inherit them from a procedure owner. This role therefore holds the
-- run/reconciliation bookkeeping privileges below plus, per workflow at deployment (see the
-- MIGRATION_CI note above), SELECT/INSERT/UPDATE on that workflow's specific production source and target
-- tables and, for a Python workflow, EXECUTE on its procedure -- verify the exact privilege set against
-- the workflow's objective.json (its inputs and outputs) on the target account before granting it for
-- real.
-- ---------------------------------------------------------------------------------------------
GRANT USAGE ON DATABASE <DB> TO ROLE MIGRATION_RUN;
GRANT USAGE ON WAREHOUSE MIGRATION_WH TO ROLE MIGRATION_RUN;
GRANT USAGE ON SCHEMA OPS TO ROLE MIGRATION_RUN;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA OPS TO ROLE MIGRATION_RUN;
GRANT SELECT, INSERT ON FUTURE TABLES IN SCHEMA OPS TO ROLE MIGRATION_RUN;

GRANT USAGE ON SCHEMA ANALYTICS TO ROLE MIGRATION_RUN;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA ANALYTICS TO ROLE MIGRATION_RUN;    -- shadow writes, recon reads
GRANT SELECT, INSERT ON FUTURE TABLES IN SCHEMA ANALYTICS TO ROLE MIGRATION_RUN;

-- EXECUTE on each migrated procedure, and USAGE plus the SWAP privilege on its production target
-- schema/tables for cutover (program spec §10.5: ALTER TABLE ... SWAP WITH), are granted per
-- workflow when MIGRATION_CI deploys that procedure — that schema is workflow-specific and not
-- named here, for the same reason as MIGRATION_CI above.
