-- BROKEN ON PURPOSE -- do not fix: a fixture for tests/test_validate_build.py and the orchestrator's
-- fix-loop scenarios. It is samples/wf_0001/canned/build.sql with one mistake:
-- EXCLUDED_ORDERS' Filter False side is written as `WHERE REGION = 'WEST'`, the naive mirror of the True side.
-- Alteryx sends rows whose Filter expression evaluates to NULL to the False anchor, so every NULL-REGION order is
-- silently lost from EXCLUDED_ORDERS.
-- wf_0001 · Sales summary by region and size band
-- Written from objective.md; checked against golden data. {{src}} = source schema, {{tgt}} = target schema.
-- Nothing in this file has run on Snowflake or Alteryx.
ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;

-- step s1, s3
-- Both outputs read the cleaned orders: CUSTOMER cut to 10 characters as Alteryx's String(10) does without
-- complaint, STATUS renamed, AMOUNT the text amount where it is a plain number and NULL where Alteryx warns.
CREATE OR REPLACE TEMPORARY TABLE clean_orders AS
SELECT LEFT(CUSTOMER, 10) AS CUSTOMER, STATUS AS ORDER_STATUS, ORDER_ID, REGION, AMOUNT_TXT, QTY, ORDER_DATE,
       TRY_TO_DOUBLE(AMOUNT_TXT) AS AMOUNT
FROM {{src}}.ORDERS;

-- step s2, s3, s4
-- The Filter's True side (a NULL REGION is not true, so SQL's WHERE already drops it). NET is worked out in
-- NUMBER so ROUND is half away from zero on exact decimals, then stored as a Double; the total adds the exact
-- decimals back up. Alteryx's Count counts rows (ORDERS) where CountNonNull counts values (PRICED_ORDERS).
CREATE OR REPLACE TABLE {{tgt}}.SALES_SUMMARY AS
SELECT REGION, SIZE_BAND,
       CAST(SUM(CAST(NET AS NUMBER(38,10))) AS FLOAT) AS TOTAL_NET,
       COUNT(*) AS ORDERS,
       COUNT(AMOUNT) AS PRICED_ORDERS,
       MAX(ORDER_DATE) AS LAST_ORDER
FROM (
    SELECT REGION, ORDER_DATE, AMOUNT,
           CAST(ROUND(CAST(AMOUNT AS NUMBER(38,10))
                      * IFF(QTY >= 10, CAST(0.9 AS NUMBER(38,10)), CAST(1 AS NUMBER(38,10))), 2) AS FLOAT) AS NET,
           CASE WHEN AMOUNT >= 1000 THEN 'LARGE' WHEN AMOUNT >= 100 THEN 'MEDIUM' ELSE 'SMALL' END AS SIZE_BAND
    FROM clean_orders
    WHERE REGION <> 'WEST'
)
GROUP BY REGION, SIZE_BAND
ORDER BY TOTAL_NET DESC NULLS LAST, REGION ASC NULLS FIRST;

-- step s2, s5
-- The Filter's False side: the WEST orders AND the orders with no REGION, which Alteryx sends to False too.
CREATE OR REPLACE TABLE {{tgt}}.EXCLUDED_ORDERS AS
SELECT CUSTOMER, ORDER_STATUS, ORDER_ID, REGION, AMOUNT_TXT, QTY, ORDER_DATE
FROM clean_orders
WHERE REGION = 'WEST';
