{{ config(materialized='ephemeral') }}
-- step s2
-- Every export column arrives as text. Alteryx warns and nulls on text it cannot read, which the TRY_ forms
-- do: 'abc' is a NULL AMOUNT and 2026-02-30 a NULL ORDER_DATE.
SELECT TRY_TO_NUMBER(ORDER_ID, 38, 0) AS ORDER_ID, TRY_TO_NUMBER(CUST_ID, 38, 0) AS CUST_ID,
       TRY_TO_DOUBLE(AMOUNT) AS AMOUNT, TRY_TO_DATE(ORDER_DATE, 'YYYY-MM-DD') AS ORDER_DATE
FROM {{ source('wf_0002', 'ORDERS_EXPORT') }}
