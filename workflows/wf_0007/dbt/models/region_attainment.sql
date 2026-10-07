{{ config(materialized='table', alias='REGION_ATTAINMENT') }}
-- step s3
-- Overwrite: exactly this run's rows.
SELECT REGION, PERIOD, TARGET_TOTAL, ACTUAL_TOTAL, LINES
FROM {{ ref('attainment') }}
