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
-- Output tool 10's PreSQL, against the target.
-- Update; Insert if new, by name, on ACCT and PERIOD; LOADED_FLAG is left alone on a match and arrives NULL on an
-- insert (the PostSQL fills it); every column of the output is named, in the objective's order.
-- The Sort's order (ACCT, POSTED_DT ascending, ENTRY_ID descending, NULL first ascending and last descending)
-- is what the running balance and Record ID read, so each window spells it out. RUN_BAL adds the exact NUMBER
-- amounts and becomes a Double once. Alteryx's Last is the value on the group's largest RECORD_ID; Count counts
-- rows; a NULL condition in IIF takes the else branch.
-- Output tool 10's PostSQL: only fills NULLs, so a row the table already held keeps its flag.
SELECT S.*, NULL AS LOADED_FLAG
FROM (
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
    FROM {{ ref('latest_entries') }}
)
GROUP BY ACCT, PERIOD
) AS S
