# Cookbook — index

The cookbook is the translator's and fixer's only source of tool semantics (program spec §8). One
page per Alteryx tool, each following the same template, each backed by a regression example under
`tests/cookbook_examples/<tool>/` and run by `tests/test_cookbook_examples.py`.

**The patterns are expression references, not structure.** A page says what Alteryx does, how to
reproduce it in Snowflake and where the parity risks are. Its "Snowflake pattern" snippet is written as
one `WITH tNN_<tool> AS (...)` query because the test harness runs one tool at a time; a translation
does not copy that shape. The translator writes ONE minimal build file for the whole workflow
(`build.sql`, or `build.py` when the objective says `python`): a tool's effect becomes a clause -- a
`WHERE`, a column expression, a `JOIN`, a `GROUP BY` -- inside the statement that needs it, and a CTE or
temporary table appears only where it removes repetition or helps a person read. Take from a page the
expression, the `NULL` handling, the cast and the `ORDER BY`; leave the CTE-per-tool wrapper behind
(`.github/agents/translator.agent.md`, "Minimal code"; `docs/reference/build-file.md`).

Pages: [Input Data](input.md) · [Output Data](output.md) · [Select](select.md) · [Filter](filter.md) ·
[Formula](formula.md) · [Join](join.md) · [Union](union.md) · [Summarize](summarize.md) ·
[Sort](sort.md) · [Unique](unique.md) · [Sample](sample.md) · [Record ID](record_id.md) ·
[Multi-Row Formula](multi_row_formula.md) · [Cross Tab](cross_tab.md) · [Transpose](transpose.md) ·
[RegEx](regex.md) · [DateTime](datetime.md) · [Data Cleansing](data_cleansing.md)

Proposed pages awaiting a runnable example live under `proposals/` (see its `.gitkeep`); nothing
there is wired into a translation until it has one.

## The Snowpark page

The pages above are one per Alteryx tool, written for `build.sql`. One more page covers the other
file a workflow can end in, `build.py`, which exists only when the objective's `language` is `python`:
[Snowpark (DataFrame idioms)](snowpark.md). It follows its own template and its own regression harness
(`tests/test_cookbook_snowpark.py`), not the per-tool template these tests check
(`test_every_cookbook_tool_has_a_page`'s `TARGET_PAGES` exclusion).

## Local verification

Every pattern on these pages is checked by `tests/test_cookbook_examples.py`: the same SQL shown
under a page's "Snowflake pattern" heading is run on DuckDB (`scripts/lib/backend.py`, Snowflake
dialect through sqlglot) against data the parity oracle (`scripts/dev/alteryx_sim.py`,
`docs/reference/simulator-semantics.md`) also runs, and `scripts/compare.py` has to call it a PASS.

**These patterns are verified on DuckDB through sqlglot against a simulator of Alteryx. They are
not yet verified on Snowflake, and not yet verified against a real Alteryx engine.** Nothing in
this repository has run on either. Three things the local runtime specifically cannot reproduce:

- **`String(n)` overflow.** Alteryx's `String(n)`/`WString(n)` truncate silently; a Snowflake
  `VARCHAR(n)` raises on an overflowing value instead. DuckDB does not enforce `VARCHAR(n)` length
  at all (`scripts/lib/backend.py`'s module docstring), so a translation that forgets the `LEFT(x,
  n)` idiom (see [select.md](select.md)) will not fail locally the way it would on Snowflake — only
  `compare.py`'s `TRUNCATION` diff class catches it, in the data rather than as an error.
- **`ALTER SESSION` on a real account.** A `build.sql` sets `TIMEZONE` and `WEEK_START` with one
  `ALTER SESSION SET` as its first statement. An owner's-rights procedure cannot run `ALTER SESSION`
  (the reason the earlier design made every procedure `EXECUTE AS CALLER`); a `build.sql` is not a
  procedure but is run by `EXECUTE IMMEDIATE FROM`, and whether that accepts `ALTER SESSION`, in a
  Task or outside one, has not been checked on any account. There is no local double for that (or for
  a real Snowflake session at all), so it cannot be exercised here — only stated.
- **Snowflake's multiplication scale rule.** Snowflake documents `NUMBER * NUMBER` as landing on
  scale `min(S1 + S2, max(S1, S2, 12))`; the local DuckDB runtime instead keeps the full `S1 + S2`.
  The NUMBER-arithmetic idiom on these pages therefore casts each operand to the scale it actually
  needs before multiplying, so `S1 + S2 <= 12` and the two runtimes cannot disagree — but that
  agreement itself has not been checked against Snowflake.

Money arithmetic on these pages follows the same idiom the hand-written canned builds use
(`samples/wf_0001/canned/build.sql`): cast to `NUMBER` before
multiplying or dividing, round the `NUMBER`, and cast back to `FLOAT` only at the end — never
`ROUND` a raw `FLOAT` (`ROUND(1.005::FLOAT, 2)` is `1.00` on this runtime where the exact decimal
form is `1.01`). The simulator itself computes formula arithmetic in exact decimal rather than
IEEE-754 binary64 — a deliberate, documented simplification
(`docs/reference/simulator-semantics.md` §1.1) — so [formula.md](formula.md) and
[summarize.md](summarize.md) call this out again in their own "Parity risks".

## Tool → Snowflake map

| Alteryx tool | Snowflake construct | Parity notes |
|--------------|---------------------|--------------|
| Input Data | table / stage + `COPY INTO` | types from yxdb header; CSV code page, header row, delimiter; Excel sheet & inferred types |
| Output Data | `INSERT` / `MERGE` / `CREATE OR REPLACE` | write mode; pre/post SQL; field mapping by name vs position; "Update; Insert if new" → `MERGE` on the configured keys |
| Select | projection + `CAST` + rename | fixed-width `String(n)` → `LEFT(x,n)`; deselected fields drop; type changes may truncate/round |
| Filter | `WHERE` (True) / `WHERE NOT (…) OR (…) IS NULL` (False) | rows evaluating to NULL go to the **False** output |
| Formula | expression per column, sequential | later expressions see earlier results in the *same* tool (nest the expressions, or chain a CTE only if that reads better); see §4 function map |
| Multi-Field Formula | same expression over N columns | generated per column; `_CurrentField_` semantics |
| Multi-Row Formula | window functions (`LAG/LEAD`) with `ORDER BY` + `PARTITION BY` group | "Num rows" lookback; behavior for unknown rows (null vs 0); requires deterministic order |
| Join | `INNER JOIN` (J) + anti-joins (L, R) | equi-join only; duplicate names get `Right_` prefix; case/trim behavior verified empirically per version |
| Union | `UNION ALL` with name-or-position alignment | "auto config by name" vs by position; output order not guaranteed |
| Summarize | `GROUP BY` | `Count` vs `CountNonNull`; `Concat` separator/quote/nulls; `First/Last` are order-dependent; percentiles interpolation |
| Cross Tab | `PIVOT` | dynamic header set → a frozen list from the golden/parsed metadata, noted in `translation_notes.md` (`build.py` only when the set truly varies; no run-time SQL in `build.sql`); header sanitization rules; aggregation method |
| Transpose | `UNPIVOT` | key columns kept; null handling in value column |
| Unique | `QUALIFY ROW_NUMBER() … = 1` | which row is "first" depends on incoming order; case-sensitivity |
| Sort | `ORDER BY` | only meaningful when a downstream tool is order-dependent; null placement; dictionary vs binary order |
| Sample | `QUALIFY ROW_NUMBER()` / `LIMIT` | first/last/skip N, 1 of every N, random → needs seed policy |
| Record ID | `ROW_NUMBER() OVER (ORDER BY …)` | starting value; requires deterministic order |
| Running Total | `SUM() OVER (ORDER BY … ROWS UNBOUNDED PRECEDING)` | group-by fields; order |
| Tile | `NTILE` / `WIDTH_BUCKET` | equal records vs equal sum vs manual cutoffs |
| Append Fields | `CROSS JOIN` | warning thresholds in Alteryx; verify cardinality |
| Find Replace | `REPLACE` / lookup join | whole-word, case-insensitive options; first match wins |
| Text to Columns | `SPLIT_PART` / `SPLIT_TO_TABLE` | to columns vs to rows; extra-chars behavior |
| RegEx | `REGEXP_REPLACE` / `REGEXP_SUBSTR` | Perl vs Snowflake regex dialect; case-insensitive flag; tokenize mode |
| DateTime | `TO_DATE` / `TO_TIMESTAMP` / `TO_CHAR` | format strings differ; two-digit years; invalid → null + warning |
| Data Cleansing | `TRIM`, `REGEXP_REPLACE`, `COALESCE` | each option maps to a separate transform; "replace nulls with 0/blank" changes downstream semantics |
| Dynamic Rename / Dynamic Select | frozen at migration time from the golden/parsed metadata (`build.py` only when the set truly varies) | schema must be known; flag as parity risk; note the frozen list in `translation_notes.md` |
| Dynamic Input | `IN (…)` rewrite over the known values, or a `build.py` loop | a `build.sql` has no run-time SQL (`statement:kind`); note the frozen values in `translation_notes.md` |
| In-DB tools | SQL lifted near-verbatim | dialect translation if source wasn't Snowflake |
| Batch macro | set-based rewrite (join on control parameter) | loop only if unavoidable |
| Iterative macro | recursive CTE (in `build.py`, a bounded loop) | termination condition |
| Interface tools / app params | fixed in the build file at the value the objective records (a build file takes no run-time arguments but `{{src}}` and `{{tgt}}`) | defaults; question types |
| Block Until Done / Control Container | statement order in the build file | no semantic effect otherwise |
| Download / Run Command / Python / R / Email / Render / Spatial / Fuzzy Match | T2 (Snowpark) or T3 | Fuzzy Match may approximate with `JAROWINKLER_SIMILARITY`/`EDITDISTANCE` but never claims parity |

## Type map

| Alteryx | Snowflake | Notes |
|---------|-----------|-------|
| Bool | BOOLEAN | Alteryx True/False from strings: "T", "Y", "1" rules |
| Byte, Int16/32/64 | NUMBER(38,0) | overflow behavior |
| FixedDecimal(p,s) | NUMBER(p,s) | Alteryx rounds on conversion; scale must match |
| Float / Double | FLOAT | tolerance-compared only |
| String(n) | VARCHAR(n) | Alteryx truncates silently; Snowflake errors → `LEFT()` |
| WString(n) / V_String / V_WString | VARCHAR | encoding; max size |
| Date / Time / DateTime | DATE / TIME / TIMESTAMP_NTZ | Alteryx has no tz; decide NTZ + session TIMEZONE |
| Blob / SpatialObj | BINARY / GEOGRAPHY | T2/T3 |

## Formula function map (excerpt — extend as encountered)

| Alteryx | Snowflake |
|---------|-----------|
| `ToString(x)` / `ToNumber(x)` | `TO_VARCHAR(x)` / `TRY_TO_NUMBER(x)` (warn-and-null semantics) |
| `IIF(c,a,b)` / `IF … THEN … ELSEIF … ENDIF` | `IFF` / `CASE` |
| `Contains(s,t)` / `StartsWith` / `EndsWith` | `CONTAINS` / `STARTSWITH` / `ENDSWITH` (case-sensitivity flags!) |
| `PadLeft(s,n,c)` | `LPAD` |
| `Trim` / `TrimLeft` / `TrimRight` | `TRIM` / `LTRIM` / `RTRIM` |
| `Round(x, m)` | `ROUND(x / m) * m` — verify half-even vs half-away behavior per build |
| `DateTimeParse(s, fmt)` / `DateTimeFormat` | `TRY_TO_TIMESTAMP(s, fmt')` / `TO_CHAR` with translated format tokens |
| `DateTimeAdd(d, n, 'days')` / `DateTimeDiff` | `DATEADD` / `DATEDIFF` (units, sign convention) |
| `DateTimeNow()` / `DateTimeToday()` | `CURRENT_TIMESTAMP` / `CURRENT_DATE` under the agreed `TIMEZONE` |
| `REGEX_Replace` / `REGEX_Match` | `REGEXP_REPLACE` / `REGEXP_LIKE` (dialect) |
| `IsNull` / `IsEmpty` | `IS NULL` / `NULLIF(TRIM(x),'') IS NULL` |
| `Substring(s, start, len)` | `SUBSTR(s, start+1, len)` (0- vs 1-based) |
| `Length` / `Uppercase` / `Lowercase` / `TitleCase` | `LENGTH` / `UPPER` / `LOWER` / `INITCAP` |
| `Mod` / `Ceil` / `Floor` / `Abs` / `Pow` | same names, check integer vs float promotion |
| `Random()` / `RandInt` | `UNIFORM(…, RANDOM(seed))` — never parity; flag |

The three tables above start from program spec §8.2–§8.4 (`docs/spec/00-README.md`); the rows that
named a procedure (app parameters, the iterative macro's loop, statement ordering) are adjusted to the
build file.
