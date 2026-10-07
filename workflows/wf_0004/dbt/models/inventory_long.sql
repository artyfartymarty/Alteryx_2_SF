{{ config(materialized='table', alias='INVENTORY_LONG') }}
-- step s5
-- Transpose on SKU, NULL cells kept (a bare UNPIVOT would drop them), read back from the pivot just written. Within
-- a SKU the rows follow the Alteryx table's order, EAST, NORTH, WEST; the union is a subquery because the DuckDB
-- double will not ORDER BY an expression over a bare UNION.
SELECT SKU, NAME, VALUE
FROM (
    SELECT SKU, 'EAST' AS NAME, EAST AS VALUE FROM {{ ref('inventory_by_wh') }}
    UNION ALL
    SELECT SKU, 'NORTH', NORTH FROM {{ ref('inventory_by_wh') }}
    UNION ALL
    SELECT SKU, 'WEST', WEST FROM {{ ref('inventory_by_wh') }}
)
ORDER BY SKU ASC NULLS FIRST, CASE NAME WHEN 'EAST' THEN 1 WHEN 'NORTH' THEN 2 ELSE 3 END
