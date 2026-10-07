-- BROKEN ON PURPOSE -- do not fix: a fixture for tests/test_validate_build.py and the orchestrator's
-- fix-loop scenarios. It is samples/wf_0002/canned/build.sql with one mistake:
-- The Data Cleansing tool's upper-case option is not translated: NAME and CITY are trimmed and blanked but keep
-- their case.
-- wf_0002 · Customer orders fact load
-- Written from objective.md; checked against golden data. {{src}} = source schema, {{tgt}} = target schema.
-- Nothing in this file has run on Snowflake or Alteryx.
ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;

-- step s1
-- Data Cleansing's options in the macro's own order: NULL to blank first (trimming a NULL leaves NULL), then
-- trim, then upper-case. TIER and CUST_ID pass through.
CREATE OR REPLACE TEMPORARY TABLE customers_clean AS
SELECT CUST_ID, TRIM(COALESCE(NAME, '')) AS NAME, TRIM(COALESCE(CITY, '')) AS CITY, TIER
FROM {{src}}.CUSTOMERS;

-- step s2
-- Every export column arrives as text. Alteryx warns and nulls on text it cannot read, which the TRY_ forms
-- do: 'abc' is a NULL AMOUNT and 2026-02-30 a NULL ORDER_DATE.
CREATE OR REPLACE TEMPORARY TABLE orders_typed AS
SELECT TRY_TO_NUMBER(ORDER_ID, 38, 0) AS ORDER_ID, TRY_TO_NUMBER(CUST_ID, 38, 0) AS CUST_ID,
       TRY_TO_DOUBLE(AMOUNT) AS AMOUNT, TRY_TO_DATE(ORDER_DATE, 'YYYY-MM-DD') AS ORDER_DATE
FROM {{src}}.ORDERS_EXPORT;

-- step s3, s4
-- The Join's three outputs, each flagged, stacked by name, appended to the rows the table already holds.
-- `=` never matches a NULL key, so a NULL-CUST_ID customer is NO_ORDERS and a NULL-CUST_ID order ORPHAN; the
-- anti-joins use NOT EXISTS, where a NOT IN would drop every row once the other side held a NULL.
INSERT INTO {{tgt}}.CUSTOMER_ORDER_FACT (CUST_ID, NAME, CITY, TIER, ORDER_ID, AMOUNT, ORDER_DATE, MATCH_FLAG)
SELECT c.CUST_ID, c.NAME, c.CITY, c.TIER, o.ORDER_ID, o.AMOUNT, o.ORDER_DATE, 'MATCHED'
FROM customers_clean c
JOIN orders_typed o ON c.CUST_ID = o.CUST_ID
UNION ALL
SELECT c.CUST_ID, c.NAME, c.CITY, c.TIER, NULL, NULL, NULL, 'NO_ORDERS'
FROM customers_clean c
WHERE NOT EXISTS (SELECT 1 FROM orders_typed o WHERE o.CUST_ID = c.CUST_ID)
UNION ALL
SELECT o.CUST_ID, NULL, NULL, NULL, o.ORDER_ID, o.AMOUNT, o.ORDER_DATE, 'ORPHAN'
FROM orders_typed o
WHERE NOT EXISTS (SELECT 1 FROM customers_clean c WHERE c.CUST_ID = o.CUST_ID);
