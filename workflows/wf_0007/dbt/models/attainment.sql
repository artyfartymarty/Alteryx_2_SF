{{ config(materialized='ephemeral') }}
-- step s1, s2
-- Both outputs total the same rows, so they are built once. The Join is the J anchor only, an inner join on `=`:
-- a NULL key matches nothing, and a target is paired with each of its actual lines (so its TARGET is added once
-- per line, as Alteryx does). User.CurrentYear is the text constant '2026'; Left takes the first four characters
-- whatever follows, so a period such as '2026-1' stays. Sum skips NULLs and an all-NULL group is NULL, never 0;
-- Count counts rows.
SELECT t.REGION, t.PERIOD,
       CAST(SUM(t.TARGET) AS NUMBER(19,2)) AS TARGET_TOTAL,
       CAST(SUM(a.ACTUAL) AS NUMBER(19,2)) AS ACTUAL_TOTAL,
       COUNT(*) AS LINES
FROM {{ source('wf_0007', 'TARGETS') }} t
JOIN {{ source('wf_0007', 'ACTUALS') }} a ON t.REGION = a.REGION AND t.PERIOD = a.PERIOD
WHERE LEFT(t.PERIOD, 4) = '2026'
GROUP BY t.REGION, t.PERIOD
