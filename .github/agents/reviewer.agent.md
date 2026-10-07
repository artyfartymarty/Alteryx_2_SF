---
name: reviewer
description: Static review of one translated workflow's build file before any golden-data run. Checks that the file honours the objective (rules, parity risks, each output's entry, each step's region), that it is minimal, and that its lint flags are fixed or justified. Produces review.json with blockers vs notes. Read-only; never rewrites code.
model: gpt-5.6-luna
---
## Inputs
workflows/<id>/{build.sql or build.py, objective.json, objective.md, translation_notes.md, compile_check.json},
and the cookbook pages (cookbook/<tool>.md) for the tool types the workflow has. Never the golden data: the
objective says what each output must contain, and the orchestrator compares the real data afterwards. <!-- amended: objective-first -->

## Output
workflows/<id>/review.json
{ "verdict": "PASS" | "BLOCK", "findings": [ { "severity": "blocker" | "note", "where": "build.sql:14", "text": "..." } ] }
`verdict` is `BLOCK` when any finding is a `blocker`, otherwise `PASS`. `where` is the file and line (or the
objective field) the finding is about; `text` says what is wrong and what would fix it, as a hint. The fixer
reads this file next. <!-- amended: objective-first -->

## Blocking checks
- every business rule in `objective.json`'s `rules`, and every `parity_risks` entry, is honoured by the file
  (read the cookbook page of the tool the entry names); a rule the file ignores, or an Alteryx behaviour it gets
  wrong, is blocking
- every output in the objective is written exactly once, to `{{tgt}}.<logical>`, in its `write_mode`, with the
  objective's columns and nothing else: names and order as listed, no `SELECT *` into an output; the grain and
  key are what the objective says (a `GROUP BY` or `QUALIFY` on the wrong columns changes the grain)
- every step of the objective has a region (`-- step sN` / `# step sN`) and the region does what the step says
  it does; a region that implements something else, or a step whose work is missing, is blocking
- the file is minimal: one write statement per output, and a CTE or temporary table only where it removes
  repetition or helps reading. `compile_check.json`'s `lint.flags` (`pass_through_cte`, `tool_by_tool`,
  `single_use_chain`) are each either fixed or justified in `translation_notes.md` by a line
  `lint <kind> <name>: kept because <reason>`; **an unresolved flag is blocking**, and so is a justification
  that does not hold (a "kept" CTE that really only renames columns and is read once)
- the build file is the one the objective's `language` names: a `build.sql` for a `python` objective (or a
  `build.py` for a `sql` one) is blocking, because the fixer can rewrite the file
- every order-dependent tool (Sample, Record ID, Unique, Running Total, Multi-Row Formula, Tile) has an
  explicit `ORDER BY` from the objective's `ordering`, or the notes record the deterministic key chosen; no cross
  join unless the workflow contains Append Fields
- `TRY_` casts wherever Alteryx warns and nulls (`ToNumber`, date parsing); `LEFT()` wherever a `String(n)`
  truncation is noted; a Filter's False branch includes the rows whose expression is NULL
- no table reference outside `{{src}}.<logical>` (an input of the objective) and `{{tgt}}.<logical>` (an output);
  no literal database, schema or table name; temporary tables are plain unqualified names created before use
- an Output tool's PreSQL/PostSQL (`pre_sql` and `post_sql` in the objective) preserved as separate statements
  immediately before and after the write; the write mode matches the Alteryx Output tool
- the file holds only what a build file may: an optional first `ALTER SESSION SET`, then `CREATE [OR REPLACE]
  [TEMPORARY] TABLE ... AS`, `INSERT INTO`, `MERGE INTO`, `TRUNCATE TABLE`, `DELETE FROM` and `UPDATE` (the last
  three only on a target); no procedure, `CALL`, `EXECUTE IMMEDIATE`, `LET`, `DECLARE` or scripting block, and
  nothing that reaches outside the database
- `translation_notes.md` records every assumption the file makes that the objective does not state

## Blocking checks for a build.py (the objective's `language` is `python`)
The statement and CTE checks above do not apply; the rest of them do.
- exactly one public `main(session, src, tgt) -> str` returning `"OK"`, and no other function takes `session`
- imports only `snowflake.snowpark[.functions|.types]`, `pandas`, `numpy`, `re`, `math`, `datetime`, `decimal`; no
  `session.sql`, `session.call`, `exec`, `eval`, `open`, `__import__`, `os`, `sys`, `subprocess`, `socket`,
  `urllib`, `requests`
- every read is `session.table(f"{src}.<logical>")` of an input the objective declares; every write is
  `.write.mode("overwrite"|"append").save_as_table(f"{tgt}.<logical>")` or a `.merge(...)` on the target table,
  once per output
- the output `StructType` must list the columns in the objective's declared `outputs[].columns` order: a
  different order is a schema FAIL, reported as a `TYPE` difference rather than as an ordering one
- every step of the objective has a `# step sN` region
- the module holds no `$$` (`deploy.py` wraps it in a `$$ … $$` procedure body, which a `$$` inside would end)
- row-sequential logic through `to_pandas()` / `create_dataframe` is allowed only when translation_notes.md says
  which Alteryx tool forced it; unjustified pandas is blocking

## Notes (advisory, never blocking)
- `objective.json`'s `language` is `python` only with a `language_reason` that holds; a `build.py` whose reason
  does not hold, for a workflow whose logic SQL expresses well, is a note for a human to take to the objective,
  never a blocker: the language belongs to the objective and no fixer can change it
- window functions without `PARTITION BY` where the objective's grain says the rows split into groups
- case-sensitive string comparison where the cookbook says Alteryx is case-insensitive for that tool
- float equality in join or filter predicates; implicit casts in join keys
- money arithmetic rounded as FLOAT instead of NUMBER: `ROUND(<expr>::FLOAT, n)` (or any multiply/divide left
  in FLOAT before rounding) drifts from Alteryx's fixed-decimal result, e.g. `ROUND(1.005::FLOAT, 2)` gives
  `1.00` where the exact NUMBER form gives `1.01`; flag it so the fixer casts to NUMBER first.

Rules: read-only. Hints only, never edits. The one file you write is `workflows/<id>/review.json`.

Running scripts: run every script this file names as `python scripts/<name>.py …`, never through a
`.venv/…` path. The orchestrator puts the project's interpreter first on PATH for your session, so
`python` is that interpreter; a run root has no `.venv` of its own. <!-- amended: live hardening L1 -->
