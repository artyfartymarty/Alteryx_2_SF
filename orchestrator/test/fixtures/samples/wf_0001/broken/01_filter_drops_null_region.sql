-- wf_0001 · Sales summary by region
-- BROKEN BUILD (test fixture): the filter drops the NULL-region rows that Alteryx keeps.
-- step s1
CREATE OR REPLACE TABLE {{tgt}}.SALES_SUMMARY AS
SELECT REGION, SUM(AMOUNT) AS TOTAL
FROM {{src}}.ORDERS
WHERE REGION <> 'WEST'
GROUP BY REGION;
