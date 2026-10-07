{{ config(materialized='ephemeral') }}
-- step s1
-- Data Cleansing's options in the macro's own order: NULL to blank first (trimming a NULL leaves NULL), then
-- trim, then upper-case. TIER and CUST_ID pass through.
SELECT CUST_ID, UPPER(TRIM(COALESCE(NAME, ''))) AS NAME, UPPER(TRIM(COALESCE(CITY, ''))) AS CITY, TIER
FROM {{ source('wf_0002', 'CUSTOMERS') }}
