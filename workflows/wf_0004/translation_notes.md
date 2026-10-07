# wf_0004 — translation notes

One assumption per line. Nothing below has been checked against a real Alteryx engine or a real Snowflake account; every claim is against `docs/reference/simulator-semantics.md` (the parity oracle) and the objective.

- The macro's tools are translated in place, in the temporary table `stock_clean`, with its question `MinQty` answered `1` as the workflow passes it, not the macro's own default `0`; stock with `QTY` below 1 or NULL is dropped.
- The macro's RegEx is Replace with `CopyUnmatched` and case-insensitive: `REGEXP_REPLACE` returns a SKU that does not match unchanged, and `UPPER` follows; tool 3's Parse is case-sensitive.
- A SKU the Parse pattern does not match keeps its row with a NULL `FAMILY`; the match is tested with `REGEXP_LIKE` and the NULL written out, because the local DuckDB double returns an empty string where Snowflake's `REGEXP_SUBSTR` returns NULL.
- Snowflake's regex dialect is not Python's, which the simulator uses: `\d` and `\s` are ASCII-only on the local engine and Unicode-aware in the oracle. No golden set contains a non-ASCII digit or a non-breaking space, so the divergence is recorded rather than guarded against.
- `ITEM_NO` and `NOTE` reach neither output, so they are not carried.
- The Cross Tab's header columns are frozen to the tool's `EAST, NORTH, WEST`: a warehouse outside that list has no column and is dropped, in Alteryx as well; a cell with no stock is NULL, not 0, so nothing wraps the `SUM` in `COALESCE`, and the `Double` sum is taken over the exact integers and cast once.
- The Transpose keeps NULL cells (a bare `UNPIVOT` drops them), as a `UNION ALL` of three projections of the pivot just written, read back after its write, with rows within a `SKU` ordered `EAST, NORTH, WEST`; the union is a subquery because the DuckDB double will not `ORDER BY` an expression over a bare `UNION`.
- `INVENTORY_LONG` has `SKU` as its only key field, so `FAMILY` is dropped from the long table.

No lint flag is raised by `scripts/compile_check.py`, so there is no `lint <kind> <name>: kept because ...` line to write.
