{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    full_refresh=false,
    unique_key=['REGION', 'PERIOD'],
    merge_update_columns=['TARGET_TOTAL', 'ACTUAL_TOTAL', 'LINES'],
    alias='ATTAINMENT_HISTORY'
) }}
-- step s4
-- Update; Insert if new, by name, on REGION and PERIOD: every row this run does not produce stays, prior years
-- included.
SELECT * FROM {{ ref('attainment') }}
