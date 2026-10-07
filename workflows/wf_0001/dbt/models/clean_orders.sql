{{ config(materialized='ephemeral') }}
-- step s1, s3
-- Both outputs read the cleaned orders: CUSTOMER cut to 10 characters as Alteryx's String(10) does without
-- complaint, STATUS renamed, AMOUNT the text amount where it is a plain number and NULL where Alteryx warns.
SELECT LEFT(CUSTOMER, 10) AS CUSTOMER, STATUS AS ORDER_STATUS, ORDER_ID, REGION, AMOUNT_TXT, QTY, ORDER_DATE,
       TRY_TO_DOUBLE(AMOUNT_TXT) AS AMOUNT
FROM {{ source('wf_0001', 'ORDERS') }}
