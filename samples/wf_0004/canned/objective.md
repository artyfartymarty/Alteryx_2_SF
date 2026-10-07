# wf_0004 — Inventory by warehouse

## Purpose

Cleans the SKU codes of the warehouse stock list, drops empty stock, and writes two views of it: one row per SKU and family with a quantity column per warehouse (INVENTORY_BY_WH), and the same quantities as one row per SKU and warehouse (INVENTORY_LONG).

## Inputs

- **STOCK** (tool 1): SKU VARCHAR(30), WAREHOUSE VARCHAR(10), QTY NUMBER(38,0), NOTE VARCHAR(100).

## Outputs

### INVENTORY_BY_WH (tool 6)

- Grain: one row per SKU and family.
- Written: overwrite.
- Key: SKU, FAMILY.
- Shape: aggregate rows; sorted by SKU, FAMILY.
- Columns:
  - `SKU` VARCHAR(30) NOT NULL: Cleaned, upper-cased SKU code
  - `FAMILY` VARCHAR(20): The letters before the dash of the SKU; NULL when the SKU did not match
  - `EAST` FLOAT: Total QTY in the EAST warehouse for the SKU and family; NULL when it has no stock rows there
  - `NORTH` FLOAT: Total QTY in the NORTH warehouse for the SKU and family; NULL when it has no stock rows there
  - `WEST` FLOAT: Total QTY in the WEST warehouse for the SKU and family; NULL when it has no stock rows there

### INVENTORY_LONG (tool 7)

- Grain: one row per pivot row and warehouse (EAST, NORTH, WEST), without the family.
- Written: overwrite.
- Key: SKU, NAME.
- Shape: aggregate rows; sorted by SKU.
- Columns:
  - `SKU` VARCHAR(30) NOT NULL: Cleaned, upper-cased SKU code
  - `NAME` VARCHAR(255) NOT NULL: Warehouse the value comes from: EAST, NORTH or WEST
  - `VALUE` FLOAT: Total quantity of the SKU in that warehouse; NULL when it has none

## Steps

- **s1 · Read warehouse stock** (tools 1): Reads the STOCK list: SKU, WAREHOUSE, QTY and NOTE. Feeds s2.
- **s2 · Clean SKU codes and drop empty stock** (tools 2): Runs the clean_codes macro with MinQty = 1: normalises each SKU to FAMILY-NUMBER form in upper case (a SKU that does not match is kept as it was) and drops the rows whose QTY is below 1 or NULL. Feeds s3.
- **s3 · Split SKU into family and item** (tools 3): Parses SKU into FAMILY (the letters) and ITEM_NO (the digits); a SKU that does not match keeps its row with both NULL. Feeds s4.
- **s4 · Pivot quantity by warehouse** (tools 4, 6): Sums QTY per SKU and FAMILY with one column per warehouse (EAST, NORTH, WEST) and writes INVENTORY_BY_WH. Feeds s5, output 6.
- **s5 · Unpivot to one row per warehouse** (tools 5, 7): Turns the EAST, NORTH and WEST columns back into Name and Value rows per SKU and writes INVENTORY_LONG. Feeds output 7.

## Business rules

- The macro's question MinQty is 1, as the workflow passes it, not the macro's own default of 0: stock with QTY below 1 or NULL is dropped.
- A SKU that is not letters, then a dash or space, then digits is kept unchanged by the macro and then has NULL FAMILY and ITEM_NO.
- A SKU and warehouse with no stock rows is NULL in the pivot, never 0.
- The warehouse columns are the three the pivot was built with (EAST, NORTH, WEST); stock for any other warehouse has no column and is left out.
- INVENTORY_LONG has a row for every cell of the pivot, NULL cells included: three rows per pivot row.

## Alteryx behaviour to keep

- Tool 1, TYPE: The Input tool declares the four fields it expects (SKU 30, WAREHOUSE 10, QTY Int32, NOTE 100). The mapped Snowflake table has to present those names and families; the widths are the Alteryx widths and Snowflake does not enforce them when a table is created from a query.
- Tool 2, LOGIC: A macro is a workflow, not a tool: there is no cookbook pattern for `macro`, only for the tools inside clean_codes.yxmc. Its inner tools are translated in place, and a change to the .yxmc file changes this translation even though inventory.yxmd did not change.
- Tool 2, LOGIC: The question MinQty is 1 in the parent's <Value name="MinQty">, overriding the macro's own NumericUpDown default of 0. Translating the default instead would keep every zero-quantity row.
- Tool 2, LOGIC: The macro's RegEx is Replace with CopyUnmatched, so a SKU that does not match is kept as it was rather than becoming NULL. REGEXP_REPLACE already returns the subject unchanged on no match, so the two agree -- but a translator who reached for a CASE with an ELSE NULL would not.
- Tool 2, LOGIC: The macro's RegEx is case-INSENSITIVE (Alteryx spells the attribute CaseInsensitve), so `ab 12` and `zz9` normalise; the parent's RegEx at tool 3 is case-sensitive. The two flags are opposite and easy to swap.
- Tool 2, NULL_SEMANTICS: The macro's Filter is [QTY] >= 1 and a NULL QTY makes that NULL, which is not true, so those rows leave on the unwired False anchor. That is what keeps a NULL WAREHOUSE (and a NULL SKU) out of the Cross Tab downstream, so SKU, WAREHOUSE and QTY are never NULL after the macro.
- Tool 2, LOGIC: Snowflake's regex dialect is not Python's, which is what the simulator uses: \d and \s are ASCII-only in the local engine and Unicode-aware in the oracle, and case-insensitive [A-Za-z] folds differently at the edges of Unicode. No golden set contains a non-ASCII digit or a non-breaking space, so the divergence is recorded rather than guarded against.
- Tool 3, NULL_SEMANTICS: RegEx Parse never drops a row: a SKU the pattern does not match keeps its row and leaves FAMILY and ITEM_NO NULL. On Snowflake REGEXP_SUBSTR returns NULL for that row; the local DuckDB double returns an empty string, so the match is tested with REGEXP_LIKE and the NULL written out explicitly.
- Tool 3, LOGIC: This RegEx is case-SENSITIVE (^([A-Z]+)-(\d+)$), unlike the macro's. Every SKU reaching it has already been upper-cased inside the macro, so the flag changes nothing on this data -- but a non-ASCII initial (the edge set's ÄB-12) is outside [A-Z] under either flag, which is what leaves its FAMILY NULL.
- Tool 4, NULL_SEMANTICS: A group/header combination with no rows is NULL, not zero. SUM over an empty set already gives NULL, so nothing may wrap the pivot in COALESCE -- and a row whose only value for a warehouse is NULL sums to NULL too.
- Tool 4, LOGIC: The header columns are frozen by the tool's MetaInfo (EAST, NORTH, WEST) rather than discovered from the data, so the pivot is one conditional aggregate per column and a warehouse outside that list has no column and is dropped. Data with a fourth warehouse would silently lose it -- in Alteryx as well as here.
- Tool 4, ROUNDING: Cross Tab's Sum produces a Double whatever the data field's type, so the integer quantities land in a FLOAT column. The sum is taken over the exact NUMBER values and cast once.
- Tool 5, NULL_SEMANTICS: Transpose keeps NULL values. A bare UNPIVOT drops a row whose value is NULL, but UNPIVOT INCLUDE NULLS keeps it too (see cookbook/transpose.md). The translation may instead stack three projections with UNION ALL, which also keeps every cell -- half the rows of this output.
- Tool 5, LOGIC: The key field is SKU only, so FAMILY is dropped from the long table; the Name/Value pair is emitted per incoming row in the configured field order EAST, NORTH, WEST.

## Questions

None open.
