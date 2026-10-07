{{ config(materialized='table', alias='SALES_SUMMARY') }}
-- step s2, s3, s4
-- The Filter's True side (a NULL REGION is not true, so SQL's WHERE already drops it). NET is worked out in
-- NUMBER so ROUND is half away from zero on exact decimals, then stored as a Double; the total adds the exact
-- decimals back up. Alteryx's Count counts rows (ORDERS) where CountNonNull counts values (PRICED_ORDERS).
SELECT REGION, SIZE_BAND,
       CAST(SUM(CAST(NET AS NUMBER(38,10))) AS FLOAT) AS TOTAL_NET,
       COUNT(*) AS ORDERS,
       COUNT(AMOUNT) AS PRICED_ORDERS,
       MAX(ORDER_DATE) AS LAST_ORDER
FROM (
    SELECT REGION, ORDER_DATE, AMOUNT,
           CAST(ROUND(CAST(AMOUNT AS NUMBER(38,10))
                      * IFF(QTY >= 10, CAST(0.9 AS NUMBER(38,10)), CAST(1 AS NUMBER(38,10))), 2) AS FLOAT) AS NET,
           CASE WHEN AMOUNT >= 1000 THEN 'LARGE' WHEN AMOUNT >= 100 THEN 'MEDIUM' ELSE 'SMALL' END AS SIZE_BAND
    FROM {{ ref('clean_orders') }}
    WHERE REGION <> 'WEST'
)
GROUP BY REGION, SIZE_BAND
ORDER BY TOTAL_NET DESC NULLS LAST, REGION ASC NULLS FIRST
