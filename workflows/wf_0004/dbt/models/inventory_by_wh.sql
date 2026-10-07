{{ config(materialized='table', alias='INVENTORY_BY_WH') }}
-- step s4
-- Cross Tab, header columns frozen to the tool's EAST, NORTH, WEST (any other warehouse has no column). A cell
-- with no stock is NULL, not 0, so nothing wraps the SUM in COALESCE; the Double sum is taken over the exact
-- integers and cast once.
SELECT SKU, FAMILY,
       CAST(SUM(IFF(WAREHOUSE = 'EAST', QTY, NULL)) AS FLOAT) AS EAST,
       CAST(SUM(IFF(WAREHOUSE = 'NORTH', QTY, NULL)) AS FLOAT) AS NORTH,
       CAST(SUM(IFF(WAREHOUSE = 'WEST', QTY, NULL)) AS FLOAT) AS WEST
FROM {{ ref('stock_clean') }}
GROUP BY SKU, FAMILY
ORDER BY SKU ASC NULLS FIRST, FAMILY ASC NULLS FIRST
