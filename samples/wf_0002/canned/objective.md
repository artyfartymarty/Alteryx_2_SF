# wf_0002 — Customer orders fact load

## Purpose

Pairs each customer with their orders and appends the result to CUSTOMER_ORDER_FACT, flagging each row MATCHED (a customer with an order), NO_ORDERS (a customer with none) or ORPHAN (an order whose customer is unknown).

## Inputs

- **CUSTOMERS** (tool 1): CUST_ID NUMBER(38,0), NAME VARCHAR(60), CITY VARCHAR(40), TIER VARCHAR(10).
- **ORDERS_EXPORT** (tool 3): ORDER_ID VARCHAR(254), CUST_ID VARCHAR(254), AMOUNT VARCHAR(254), ORDER_DATE VARCHAR(254).

## Outputs

### CUSTOMER_ORDER_FACT (tool 10)

- Grain: one row per customer and order that matched, plus one per customer with no orders and one per order with no known customer.
- Written: append.
- Key: none declared (compared as a set of rows).
- Shape: expand rows; no row order to keep.
- Columns:
  - `CUST_ID` NUMBER(38,0): Customer number
  - `NAME` VARCHAR(60): Customer name, trimmed and upper-cased (blank when the source had none); NULL on ORPHAN rows
  - `CITY` VARCHAR(40): Customer city, trimmed and upper-cased (blank when the source had none); NULL on ORPHAN rows
  - `TIER` VARCHAR(10): Customer tier; NULL on ORPHAN rows
  - `ORDER_ID` NUMBER(38,0): Order number; NULL on NO_ORDERS rows
  - `AMOUNT` FLOAT: Order amount as a number; NULL when the text was not a number or the row has no order
  - `ORDER_DATE` DATE: Order date; NULL when the text was not a valid date or the row has no order
  - `MATCH_FLAG` VARCHAR(12) NOT NULL: MATCHED, NO_ORDERS or ORPHAN

## Steps

- **s1 · Clean the customer list** (tools 1, 2): Reads CUSTOMERS and, for NAME and CITY, replaces NULL with blank, trims the blanks and upper-cases. Feeds s3.
- **s2 · Type the orders export** (tools 3, 4): Reads the nightly orders export, where every column arrives as text, and converts ORDER_ID and CUST_ID to whole numbers, AMOUNT to a number and ORDER_DATE to a date; text that cannot be converted becomes NULL. Feeds s3.
- **s3 · Match customers to orders and flag the outcome** (tools 5, 6, 7, 8): Joins customers to orders on CUST_ID and flags each outcome: MATCHED for a customer with an order, NO_ORDERS for a customer with none, ORPHAN for an order whose customer is not in the list. Feeds s4.
- **s4 · Stack the outcomes and append them** (tools 9, 10): Stacks the three outcomes by column name and appends them to CUSTOMER_ORDER_FACT, which keeps the rows it already holds. Feeds output 10.

No effect on the data:
- Tool 11: Browse tool: shows the stacked rows to a person, changes nothing.

## Business rules

- A customer with several orders produces one row per order.
- A customer with no order is kept, with MATCH_FLAG NO_ORDERS and NULL order columns.
- An order whose customer is not in the customer list is kept, with MATCH_FLAG ORPHAN and NULL customer columns.
- Text in the orders export that is not a valid number or date becomes NULL; it never fails the run.
- The run appends to CUSTOMER_ORDER_FACT: the rows already in it stay, and the table after the append is what counts.

## Alteryx behaviour to keep

- Tool 2, NULL_SEMANTICS: Data Cleansing replaces NULL strings with blank before trimming, so NAME and CITY are never NULL downstream of it.
- Tool 2, LOGIC: The options run in the macro's own order (replace nulls, trim, modify case); applying UPPER before TRIM would agree here but not once a case-changing option is combined with a character-removing one.
- Tool 2, LOGIC: UPPER is applied by Snowflake's own Unicode case rules, which are not guaranteed to match Alteryx's for every script; the edge golden set exercises Latin Extended and CJK text.
- Tool 4, NULL_SEMANTICS: The Select retypes text to Int32/Double/Date; Alteryx warns and nulls on text it cannot read, so every conversion is a TRY_ form -- 'abc' and the empty string give a NULL AMOUNT.
- Tool 4, TYPE: A date that does not exist (2026-02-30) and a non-ISO date (31/12/2026) both become NULL rather than raising; ORDER_DATE is read with an explicit YYYY-MM-DD format.
- Tool 4, ROUNDING: Alteryx's integer coercion rounds half away from zero, which TRY_TO_NUMBER(x, 38, 0) also does; the sample data is whole numbers, so nothing here proves it.
- Tool 5, NULL_SEMANTICS: A NULL join key matches nothing, so a NULL-CUST_ID customer leaves on L and a NULL-CUST_ID order leaves on R; the anti-joins use NOT EXISTS, which keeps that behaviour, where a NOT IN would drop every row once the other side held a NULL.
- Tool 5, LOGIC: Join key comparison is exact and case-sensitive -- no trimming, no case folding, no numeric coercion of text; both sides are already NUMBER here.
- Tool 9, LOGIC: Union by name: the output keeps input #1's field order and a field an input lacks arrives NULL, so every branch is projected into tool 6's column list with typed NULLs before the UNION ALL.
- Tool 10, LOGIC: Append Existing adds to whatever the target already holds and maps columns to it by name, so the golden output is the target's state AFTER the append, not just this run's rows.

## Questions

None open.
