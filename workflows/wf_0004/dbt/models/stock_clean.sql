{{ config(materialized='ephemeral') }}
-- step s1, s2, s3
-- The clean_codes macro, with its question MinQty answered 1 (not the macro's default 0): the RegEx (Replace,
-- case-insensitive, CopyUnmatched: REGEXP_REPLACE returns a SKU that does not match unchanged) writes SKU as
-- FAMILY-NUMBER, Uppercase follows, and a NULL QTY is not >= 1 so it leaves with the small stock. Then the
-- case-sensitive Parse: a SKU the pattern does not match keeps its row with a NULL FAMILY (the match is tested
-- with REGEXP_LIKE because the DuckDB double returns '' where Snowflake returns NULL). ITEM_NO and NOTE reach
-- neither output, so they are not carried.
SELECT SKU, WAREHOUSE, QTY,
       IFF(REGEXP_LIKE(SKU, '^([A-Z]+)-(\\d+)$'), REGEXP_SUBSTR(SKU, '^([A-Z]+)-(\\d+)$', 1, 1, 'c', 1), NULL) AS FAMILY
FROM (
    SELECT UPPER(REGEXP_REPLACE(SKU, '^\\s*([A-Za-z]+)[- ]?(\\d+)\\s*$', '\\1-\\2', 1, 0, 'i')) AS SKU, WAREHOUSE, QTY
    FROM {{ source('wf_0004', 'STOCK') }}
    WHERE QTY >= 1
)
