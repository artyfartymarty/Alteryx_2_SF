-- BROKEN ON PURPOSE -- do not fix: a fixture for tests/test_validate_build.py and the orchestrator's
-- fix-loop scenarios. It is samples/wf_0007/canned/build.sql with one mistake:
-- The MERGE's key is narrowed to REGION -- a translator who read the first of tool 7's two UpdateKeys only. It runs
-- without an error, but now matches a region's other periods: history rows are overwritten with another period's
-- values and new periods of a region already in the table are never inserted. compile_check.py refuses this one
-- (write_mode: the MERGE must match on exactly the objective's keys), so only the first broken build of this
-- sample reaches the validator in the orchestrator's fix-loop scenario.
-- wf_0007 · Regional targets and attainment
-- Written from objective.md; checked against golden data. {{src}} = source schema, {{tgt}} = target schema.
-- Nothing in this file has run on Snowflake or Alteryx.
ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;

-- step s1, s2
-- Both outputs total the same rows, so they are built once. The Join is the J anchor only, an inner join on `=`:
-- a NULL key matches nothing, and a target is paired with each of its actual lines (so its TARGET is added once
-- per line, as Alteryx does). User.CurrentYear is the text constant '2026'; Left takes the first four characters
-- whatever follows, so a period such as '2026-1' stays. Sum skips NULLs and an all-NULL group is NULL, never 0;
-- Count counts rows.
CREATE OR REPLACE TEMPORARY TABLE attainment AS
SELECT t.REGION, t.PERIOD,
       CAST(SUM(t.TARGET) AS NUMBER(19,2)) AS TARGET_TOTAL,
       CAST(SUM(a.ACTUAL) AS NUMBER(19,2)) AS ACTUAL_TOTAL,
       COUNT(*) AS LINES
FROM {{src}}.TARGETS t
JOIN {{src}}.ACTUALS a ON t.REGION = a.REGION AND t.PERIOD = a.PERIOD
WHERE LEFT(t.PERIOD, 4) = '2026'
GROUP BY t.REGION, t.PERIOD;

-- step s3
-- Overwrite: exactly this run's rows.
CREATE OR REPLACE TABLE {{tgt}}.REGION_ATTAINMENT AS
SELECT REGION, PERIOD, TARGET_TOTAL, ACTUAL_TOTAL, LINES
FROM attainment;

-- step s4
-- Update; Insert if new, by name, on REGION: every row this run does not produce stays, prior years included.
MERGE INTO {{tgt}}.ATTAINMENT_HISTORY AS T
USING attainment AS S
ON T.REGION = S.REGION
WHEN MATCHED THEN UPDATE SET TARGET_TOTAL = S.TARGET_TOTAL, ACTUAL_TOTAL = S.ACTUAL_TOTAL, LINES = S.LINES
WHEN NOT MATCHED THEN INSERT (REGION, PERIOD, TARGET_TOTAL, ACTUAL_TOTAL, LINES)
                      VALUES (S.REGION, S.PERIOD, S.TARGET_TOTAL, S.ACTUAL_TOTAL, S.LINES);
