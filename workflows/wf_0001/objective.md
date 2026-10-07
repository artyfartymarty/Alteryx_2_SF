# wf_0001 — Sales summary by region and size band

## Purpose

Totals each region's net order value by size band for the weekly sales review, leaving the WEST region out, and lists the orders it left out.

## Inputs

- **ORDERS** (tool 1): ORDER_ID NUMBER(38,0), CUSTOMER VARCHAR(50), REGION VARCHAR(20), AMOUNT_TXT VARCHAR(20), QTY NUMBER(38,0), ORDER_DATE DATE, STATUS VARCHAR(10).

## Outputs

### SALES_SUMMARY (tool 7)

- Grain: one row per region and size band.
- Written: overwrite.
- Key: REGION, SIZE_BAND.
- Shape: aggregate rows; sorted by TOTAL_NET, REGION.
- Columns:
  - `REGION` VARCHAR(20) NOT NULL: Sales region of the orders in the row
  - `SIZE_BAND` VARCHAR(10) NOT NULL: Size band (LARGE, MEDIUM or SMALL) of the orders in the row
  - `TOTAL_NET` FLOAT: Sum of NET, the discounted order value, for the region and band
  - `ORDERS` NUMBER(38,0): Number of orders in the region and band
  - `PRICED_ORDERS` NUMBER(38,0): Number of those orders that have a numeric AMOUNT
  - `LAST_ORDER` DATE: Latest ORDER_DATE in the region and band

### EXCLUDED_ORDERS (tool 8)

- Grain: one row per excluded order.
- Written: overwrite.
- Key: none declared (compared as a set of rows).
- Shape: filter rows; no row order to keep.
- Columns:
  - `CUSTOMER` VARCHAR(10): Customer name, cut to 10 characters
  - `ORDER_STATUS` VARCHAR(10): Order status (the source STATUS column, renamed)
  - `ORDER_ID` NUMBER(38,0): Order number
  - `REGION` VARCHAR(20): Region of the order; NULL for an order that had none
  - `AMOUNT_TXT` VARCHAR(20): Order amount exactly as the source holds it, as text
  - `QTY` NUMBER(38,0): Quantity ordered
  - `ORDER_DATE` DATE: Date the order was placed

## Steps

- **s1 · Read and clean orders** (tools 1, 2): Reads the ORDERS extract, cuts CUSTOMER to its first 10 characters and renames STATUS to ORDER_STATUS. Feeds s2.
- **s2 · Set the WEST region aside** (tools 3): Splits the orders on REGION != "WEST": the True side carries on to the summary; the False side (WEST orders, and orders with no REGION) goes to the excluded list. Feeds s3, s5.
- **s3 · Work out net value and size band** (tools 4): Converts AMOUNT_TXT to a number (AMOUNT), applies a 10% discount on quantities of 10 or more and rounds to the cent (NET), and bands each order LARGE, MEDIUM or SMALL by AMOUNT (SIZE_BAND). Feeds s4.
- **s4 · Total by region and size band** (tools 5, 6, 7): Sums NET, counts orders and priced orders and takes the latest order date for each region and size band, sorts the biggest totals first and writes SALES_SUMMARY. Feeds output 7.
- **s5 · List the excluded orders** (tools 8): Writes the orders that were set aside, with their cleaned columns, to EXCLUDED_ORDERS. Feeds output 8.

## Business rules

- Orders whose REGION is WEST are left out of SALES_SUMMARY and written to EXCLUDED_ORDERS instead.
- An order with no REGION is not counted in the summary either: it is written to EXCLUDED_ORDERS with the WEST orders.
- NET is AMOUNT times 0.9 when QTY is 10 or more, otherwise AMOUNT times 1, rounded to the cent.
- SIZE_BAND is LARGE when AMOUNT is 1000 or more, MEDIUM when it is 100 or more and SMALL otherwise; an order whose AMOUNT_TXT is not a plain number has no AMOUNT and is SMALL.
- CUSTOMER is cut to its first 10 characters, as Alteryx's String(10) does without complaint.
- SALES_SUMMARY is sorted by TOTAL_NET, biggest first, then by REGION.

## Alteryx behaviour to keep

- Tool 3, NULL_SEMANTICS: Filter [REGION] != "WEST": a NULL REGION makes the expression NULL, and Alteryx sends NULL to the False anchor, so tool 8's stream must keep those rows.
- Tool 4, ROUNDING: Round([AMOUNT] * IIF([QTY] >= 10, 0.9, 1), 0.01) is half away from zero on exact decimals; the arithmetic is done in NUMBER(38,10) and only the result is stored back as a Double.
- Tool 4, NULL_SEMANTICS: ToNumber warns and nulls on text that is not a plain decimal (abc, 1,200.50, the empty string), and a NULL AMOUNT makes every comparison in the SIZE_BAND band NULL, so those rows fall to the ELSE branch SMALL.
- Tool 2, TRUNCATION: Select retypes CUSTOMER to String(10): Alteryx truncates silently where Snowflake's VARCHAR(10) would raise, so the translation uses LEFT(CUSTOMER, 10).
- Tool 5, LOGIC: Alteryx's Count counts rows where CountNonNull counts values: ORDERS is the number of rows in the group, PRICED_ORDERS only the rows with a non-NULL AMOUNT.
- Tool 5, ROUNDING: Sum over a Double column accumulates in binary floating point; the translation adds the exact decimal values and stores the total once, so TOTAL_NET agrees to the cent.
- Tool 6, ORDERING: NULL placement differs between ascending and descending: Alteryx puts NULL first ascending and last descending, so the ORDER BY spells the NULLS placement out.

## Questions

None open.
