# wf_0004 — digest

7 tools · 2 outputs · from parsed/dag.json (deterministic; never edited by hand)

Each line: `T<id> <Type>  <what it does>  ← <where its rows come from>  (<anchor> → <where they go>)  — <annotation>`. `T3.T` is anchor T of tool 3. Expressions are kept whole.

## Shared by more than one output

T1 Input  read stock.yxdb (yxdb)  — Warehouse stock
T2 Macro  clean_codes.yxmc; MinQty = 1 (default 0)  ← T1  — Clean SKU codes, drop empty stock
  T2/1 MacroInput  — Stock rows
  T2/2 RegEx  replace on SKU: expression "^\s*([A-Za-z]+)[- ]?(\d+)\s*$"; replacement "$1-$2"; case-insensitive; copy unmatched  ← T2/1  — Normalise to FAMILY-NUMBER
  T2/3 Formula  SKU:V_String(30) = Uppercase([SKU])  ← T2/2  — Upper-case the code
  T2/4 Filter  [QTY] >= [%Question.MinQty%]  ← T2/3   (T → T2/5, F → none)  — Drop stock below MinQty
  T2/5 MacroOutput  ← T2/4.T  — Cleaned stock rows
T3 RegEx  parse on SKU: expression "^([A-Z]+)-(\d+)$"; case-sensitive; output fields FAMILY:V_String(20), ITEM_NO:V_String(10)  ← T2  — Split SKU into family and item
T4 CrossTab  group by SKU, FAMILY; header WAREHOUSE; data QTY; methods Sum  ← T3  — Quantity per warehouse

## Output T6 · inventory_by_wh.yxdb

Shared tools used: T1, T2, T3, T4
T6 Output  write inventory_by_wh.yxdb (yxdb), mode overwrite  ← T4  — inventory_by_wh.yxdb

## Output T7 · inventory_long.yxdb

Shared tools used: T1, T2, T3, T4
T5 Transpose  key fields SKU; data fields EAST, NORTH, WEST  ← T4  — Back to one row per warehouse
T7 Output  write inventory_long.yxdb (yxdb), mode overwrite  ← T5  — inventory_long.yxdb
