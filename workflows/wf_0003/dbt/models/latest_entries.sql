{{ config(materialized='ephemeral') }}
-- step s1, s2
-- The Input tool's own query is `WHERE AMOUNT <> 0`, written out again because the mapped table is read whole.
-- User.Region is a workflow constant, resolved here to 'EMEA'; a NULL REGION is not true, so WHERE drops it as
-- Alteryx's unwired False anchor does. POSTED is dd/mm/yyyy text that Alteryx reads as a DateTime and nulls when
-- it cannot (31/02/2026). Sort then Unique keeps, for each ACCT and POSTED_DT, the first row in incoming order
-- -- the largest ENTRY_ID -- and two NULL POSTED_DT rows of one ACCT are one key, as PARTITION BY treats them.
SELECT ACCT, PERIOD, AMOUNT, ENTRY_ID, TRY_TO_TIMESTAMP_NTZ(POSTED, 'DD/MM/YYYY') AS POSTED_DT
FROM {{ source('wf_0003', 'GL_LEDGER') }}
WHERE AMOUNT <> 0 AND REGION = 'EMEA'
QUALIFY ROW_NUMBER() OVER (PARTITION BY ACCT, TRY_TO_TIMESTAMP_NTZ(POSTED, 'DD/MM/YYYY')
                           ORDER BY ENTRY_ID DESC NULLS LAST) = 1
