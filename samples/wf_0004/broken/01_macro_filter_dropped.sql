-- BROKEN ON PURPOSE -- do not fix: a fixture for tests/test_validate_build.py and the orchestrator's
-- fix-loop scenarios. It is samples/wf_0004/canned/build.sql with one mistake:
-- The macro's Filter (QTY >= the question value) is not translated: the rows it removes reach both outputs.
-- wf_0004 · Inventory by warehouse
-- Written from objective.md; checked against golden data. {{src}} = source schema, {{tgt}} = target schema.
-- Nothing in this file has run on Snowflake or Alteryx.
ALTER SESSION SET TIMEZONE = 'America/New_York', WEEK_START = 1;

-- step s1, s2, s3
-- The clean_codes macro, with its question MinQty answered 1 (not the macro's default 0): the RegEx (Replace,
-- case-insensitive, CopyUnmatched: REGEXP_REPLACE returns a SKU that does not match unchanged) writes SKU as
-- FAMILY-NUMBER, Uppercase follows, and a NULL QTY is not >= 1 so it leaves with the small stock. Then the
-- case-sensitive Parse: a SKU the pattern does not match keeps its row with a NULL FAMILY (the match is tested
-- with REGEXP_LIKE because the DuckDB double returns '' where Snowflake returns NULL). ITEM_NO and NOTE reach
-- neither output, so they are not carried.
CREATE OR REPLACE TEMPORARY TABLE stock_clean AS
SELECT SKU, WAREHOUSE, QTY,
       IFF(REGEXP_LIKE(SKU, '^([A-Z]+)-(\\d+)$'), REGEXP_SUBSTR(SKU, '^([A-Z]+)-(\\d+)$', 1, 1, 'c', 1), NULL) AS FAMILY
FROM (
    SELECT UPPER(REGEXP_REPLACE(SKU, '^\\s*([A-Za-z]+)[- ]?(\\d+)\\s*$', '\\1-\\2', 1, 0, 'i')) AS SKU, WAREHOUSE, QTY
    FROM {{src}}.STOCK
);

-- step s4
-- Cross Tab, header columns frozen to the tool's EAST, NORTH, WEST (any other warehouse has no column). A cell
-- with no stock is NULL, not 0, so nothing wraps the SUM in COALESCE; the Double sum is taken over the exact
-- integers and cast once.
CREATE OR REPLACE TABLE {{tgt}}.INVENTORY_BY_WH AS
SELECT SKU, FAMILY,
       CAST(SUM(IFF(WAREHOUSE = 'EAST', QTY, NULL)) AS FLOAT) AS EAST,
       CAST(SUM(IFF(WAREHOUSE = 'NORTH', QTY, NULL)) AS FLOAT) AS NORTH,
       CAST(SUM(IFF(WAREHOUSE = 'WEST', QTY, NULL)) AS FLOAT) AS WEST
FROM stock_clean
GROUP BY SKU, FAMILY
ORDER BY SKU ASC NULLS FIRST, FAMILY ASC NULLS FIRST;

-- step s5
-- Transpose on SKU, NULL cells kept (a bare UNPIVOT would drop them), read back from the pivot just written. Within
-- a SKU the rows follow the Alteryx table's order, EAST, NORTH, WEST; the union is a subquery because the DuckDB
-- double will not ORDER BY an expression over a bare UNION.
CREATE OR REPLACE TABLE {{tgt}}.INVENTORY_LONG AS
SELECT SKU, NAME, VALUE
FROM (
    SELECT SKU, 'EAST' AS NAME, EAST AS VALUE FROM {{tgt}}.INVENTORY_BY_WH
    UNION ALL
    SELECT SKU, 'NORTH', NORTH FROM {{tgt}}.INVENTORY_BY_WH
    UNION ALL
    SELECT SKU, 'WEST', WEST FROM {{tgt}}.INVENTORY_BY_WH
)
ORDER BY SKU ASC NULLS FIRST, CASE NAME WHEN 'EAST' THEN 1 WHEN 'NORTH' THEN 2 ELSE 3 END;
