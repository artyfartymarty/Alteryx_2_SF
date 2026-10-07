# wf_0001 — Sales summary by region

## Purpose

Totals each region's order amount for the weekly sales review, leaving out the WEST region.

## Inputs

- `ORDERS` (tool 1): REGION, AMOUNT.

## Outputs

### SALES_SUMMARY

One row per region, keyed by REGION: REGION (the sales region) and TOTAL (the sum of the region's amounts).

## Steps

- s1 — Total the orders by region (tools 1, 2, 3, 4).

## Business rules

- Orders whose region is WEST are left out of the summary.

## Alteryx behaviour to keep

None recorded.

## Questions

None.
