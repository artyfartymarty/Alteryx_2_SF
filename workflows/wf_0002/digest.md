# wf_0002 — digest

10 tools · 1 output · from parsed/dag.json (deterministic; never edited by hand)
5 nodes that carry no data (containers, comments, browse tools) are not listed.

Each line: `T<id> <Type>  <what it does>  ← <where its rows come from>  (<anchor> → <where they go>)  — <annotation>`. `T3.T` is anchor T of tool 3. Expressions are kept whole.

## Output T10 · dbo.CUSTOMER_ORDER_FACT

T1 Input  read customers.yxdb (yxdb)  — CRM customers
T2 DataCleansing  fields NAME, CITY: replace null text with blank, trim whitespace, upper case  ← T1  — Trim and upper-case NAME, CITY
T3 Input  read orders_export.csv (csv, delimiter ",", header row, codepage 28591)  — Nightly orders export
T4 Select  ORDER_ID:Int32; CUST_ID:Int32; AMOUNT:Double; ORDER_DATE:Date; other fields dropped  ← T3  — Strings to numbers and dates
T5 Join  on Left.CUST_ID = Right.CUST_ID; drop Right_CUST_ID  ← Left: T2, Right: T4   (L → T7, J → T6, R → T8)  — Customers to orders on CUST_ID
T6 Formula  MATCH_FLAG:V_String(12) = "MATCHED"  ← T5.J  — MATCHED
T7 Formula  MATCH_FLAG:V_String(12) = "NO_ORDERS"  ← T5.L  — NO_ORDERS
T8 Formula  MATCH_FLAG:V_String(12) = "ORPHAN"  ← T5.R  — ORPHAN
T9 Union  by name  ← T6, T7, T8  — Stack the three flags
T10 Output  write db:dw_sales table dbo.CUSTOMER_ORDER_FACT, mode append  ← T9  — dbo.CUSTOMER_ORDER_FACT
