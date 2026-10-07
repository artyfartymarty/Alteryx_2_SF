{{ config(
    materialized='incremental',
    incremental_strategy='append',
    full_refresh=false,
    alias='CUSTOMER_ORDER_FACT'
) }}
-- step s3, s4
-- The Join's three outputs, each flagged, stacked by name, appended to the rows the table already holds.
-- `=` never matches a NULL key, so a NULL-CUST_ID customer is NO_ORDERS and a NULL-CUST_ID order ORPHAN; the
-- anti-joins use NOT EXISTS, where a NOT IN would drop every row once the other side held a NULL.
WITH q (CUST_ID, NAME, CITY, TIER, ORDER_ID, AMOUNT, ORDER_DATE, MATCH_FLAG) AS (
SELECT c.CUST_ID, c.NAME, c.CITY, c.TIER, o.ORDER_ID, o.AMOUNT, o.ORDER_DATE, 'MATCHED'
FROM {{ ref('customers_clean') }} c
JOIN {{ ref('orders_typed') }} o ON c.CUST_ID = o.CUST_ID
UNION ALL
SELECT c.CUST_ID, c.NAME, c.CITY, c.TIER, NULL, NULL, NULL, 'NO_ORDERS'
FROM {{ ref('customers_clean') }} c
WHERE NOT EXISTS (SELECT 1 FROM {{ ref('orders_typed') }} o WHERE o.CUST_ID = c.CUST_ID)
UNION ALL
SELECT o.CUST_ID, NULL, NULL, NULL, o.ORDER_ID, o.AMOUNT, o.ORDER_DATE, 'ORPHAN'
FROM {{ ref('orders_typed') }} o
WHERE NOT EXISTS (SELECT 1 FROM {{ ref('customers_clean') }} c WHERE c.CUST_ID = o.CUST_ID)
)
SELECT * FROM q
