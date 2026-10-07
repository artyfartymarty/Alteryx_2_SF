-- BROKEN ON PURPOSE -- do not fix: a fixture for tests/test_validate_build.py and the orchestrator's
-- fix-loop scenarios. It is samples/wf_0003/canned/build.sql with one mistake:
-- The Sort before the Unique orders ENTRY_ID ascending, so the Unique keeps the lowest ENTRY_ID of each ACCT and
-- POSTED_DT instead of the highest.
-- wf_0003 · GL period close
-- Written from objective.md; checked against golden data. {{src}} = source schema, {{tgt}} = target schema.
-- Nothing in this file has run on Snowflake or Alteryx.
ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;

-- step s1, s2
-- The Input tool's own query is `WHERE AMOUNT <> 0`, written out again because the mapped table is read whole.
-- User.Region is a workflow constant, resolved here to 'EMEA'; a NULL REGION is not true, so WHERE drops it as
-- Alteryx's unwired False anchor does. POSTED is dd/mm/yyyy text that Alteryx reads as a DateTime and nulls when
-- it cannot (31/02/2026). Sort then Unique keeps, for each ACCT and POSTED_DT, the first row in incoming order
-- -- the largest ENTRY_ID -- and two NULL POSTED_DT rows of one ACCT are one key, as PARTITION BY treats them.
CREATE OR REPLACE TEMPORARY TABLE latest_entries AS
SELECT ACCT, PERIOD, AMOUNT, ENTRY_ID, TRY_TO_TIMESTAMP_NTZ(POSTED, 'DD/MM/YYYY') AS POSTED_DT
FROM {{src}}.GL_LEDGER
WHERE AMOUNT <> 0 AND REGION = 'EMEA'
QUALIFY ROW_NUMBER() OVER (PARTITION BY ACCT, TRY_TO_TIMESTAMP_NTZ(POSTED, 'DD/MM/YYYY')
                           ORDER BY ENTRY_ID ASC NULLS FIRST) = 1;

-- step s4
-- Output tool 10's PreSQL, against the target.
DELETE FROM {{tgt}}.GL_SUMMARY WHERE PERIOD < '2020-01';

-- step s3, s4
-- Update; Insert if new, by name, on ACCT and PERIOD; LOADED_FLAG is left alone on a match and arrives NULL on an
-- insert (the PostSQL fills it); every column of the output is named, in the objective's order.
-- The Sort's order (ACCT, POSTED_DT ascending, ENTRY_ID descending, NULL first ascending and last descending)
-- is what the running balance and Record ID read, so each window spells it out. RUN_BAL adds the exact NUMBER
-- amounts and becomes a Double once. Alteryx's Last is the value on the group's largest RECORD_ID; Count counts
-- rows; a NULL condition in IIF takes the else branch.
MERGE INTO {{tgt}}.GL_SUMMARY AS T
USING (
    SELECT ACCT, PERIOD,
           CAST(SUM(AMOUNT) AS NUMBER(19,2)) AS TOTAL,
           MAX_BY(RUN_BAL, RECORD_ID) AS CLOSING_BAL,
           COUNT(*) AS ENTRIES,
           MAX(IFF(TO_CHAR(POSTED_DT, 'YYYY-MM-DD') = '2026-08-31', 'Y', 'N')) AS HAS_PERIOD_END
    FROM (
        SELECT ACCT, PERIOD, AMOUNT, POSTED_DT,
               CAST(SUM(AMOUNT) OVER (PARTITION BY ACCT ORDER BY POSTED_DT ASC NULLS FIRST, ENTRY_ID DESC NULLS LAST
                                      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS FLOAT) AS RUN_BAL,
               ROW_NUMBER() OVER (ORDER BY ACCT ASC NULLS FIRST, POSTED_DT ASC NULLS FIRST,
                                  ENTRY_ID DESC NULLS LAST) AS RECORD_ID
        FROM latest_entries
    )
    GROUP BY ACCT, PERIOD
) AS S
ON T.ACCT = S.ACCT AND T.PERIOD = S.PERIOD
WHEN MATCHED THEN UPDATE SET TOTAL = S.TOTAL, CLOSING_BAL = S.CLOSING_BAL, ENTRIES = S.ENTRIES,
                             HAS_PERIOD_END = S.HAS_PERIOD_END
WHEN NOT MATCHED THEN INSERT (ACCT, PERIOD, TOTAL, CLOSING_BAL, ENTRIES, HAS_PERIOD_END, LOADED_FLAG)
                      VALUES (S.ACCT, S.PERIOD, S.TOTAL, S.CLOSING_BAL, S.ENTRIES, S.HAS_PERIOD_END, NULL);

-- step s4
-- Output tool 10's PostSQL: only fills NULLs, so a row the table already held keeps its flag.
UPDATE {{tgt}}.GL_SUMMARY SET LOADED_FLAG = 'Y' WHERE LOADED_FLAG IS NULL;
