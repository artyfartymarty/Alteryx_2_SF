-- wf_0001 · Sales summary by region
-- Test fixture for the orchestrator's own tests: it follows the canned layout and is never run.
-- step s1
CREATE OR REPLACE TABLE {{tgt}}.SALES_SUMMARY AS
SELECT REGION, SUM(AMOUNT) AS TOTAL
FROM {{src}}.ORDERS
WHERE REGION <> 'WEST' OR REGION IS NULL
GROUP BY REGION;
