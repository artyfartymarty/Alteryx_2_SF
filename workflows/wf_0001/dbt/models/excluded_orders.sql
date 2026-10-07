{{ config(materialized='table', alias='EXCLUDED_ORDERS') }}
-- step s2, s5
-- The Filter's False side: the WEST orders AND the orders with no REGION, which Alteryx sends to False too.
SELECT CUSTOMER, ORDER_STATUS, ORDER_ID, REGION, AMOUNT_TXT, QTY, ORDER_DATE
FROM {{ ref('clean_orders') }}
WHERE NOT (REGION <> 'WEST') OR REGION IS NULL
