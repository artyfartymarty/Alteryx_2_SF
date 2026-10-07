# The objective: mechanical fields, judgment fields, and the checker

A workflow's **objective** is what the workflow is for and what each of its outputs must contain
(objective-first design, `docs/superpowers/specs/2026-10-06-objective-first-design.md` §4.2). The analyzer
writes it as `workflows/<id>/objective.json` and `objective.md`; the translator writes ONE `build.sql` (or
`build.py`) from it; the reviewer checks the file against it; `compare.py` reads the per-output comparison
settings through `lib/objective.py: contract_view`, in the shape it has always read, so the comparison
engine is unchanged. It replaces the per-segment `contract.json` of the segment-by-segment pipeline, which no longer
exists.

Four files and four scripts belong to it:

| file | written by | read by |
|---|---|---|
| `digest.md` | `scripts/digest.py <id>` | the analyzer and the translator |
| `objective.json`, `objective.md` | the analyzer; the mechanical fields by `scripts/objective_scaffold.py` | the translator, the reviewer, `compare.py` (through `contract_view`), `render_mapping.py`, the documenter |
| `intake/objective_questions.md` | `scripts/objective_questions.py --write` | a person |

| script | what it does | exit codes |
|---|---|---|
| `digest.py <id>` | writes `digest.md`, prints its path | 0; 2 no `parsed/dag.json` |
| `objective_scaffold.py <id> --prefill` / `--apply` | writes the skeleton / re-applies the mechanical fields | 0; 1 (`--apply`) no or unreadable file; 2 usage |
| `objective_check.py <id> [--ready]` | every rule below, one problem per line on stderr | 0 passes; 1 problems; 2 usage |
| `objective_questions.py <id> --write` / `--status` / `--answers` | renders the questions file / says whether all are answered / prints the resolved answers as JSON | 0; 1 (`--status`) open questions; 2 usage |

Every script takes `--root PATH` (default `.`). Nothing here has run against a real Snowflake account or
Alteryx: the scripts read and write JSON, YAML and Markdown the pipeline produced.

## Who owns which field

| Field | Owner | Derived from |
|---|---|---|
| `workflow` | scaffold | the workflow id |
| each input's `tool_id`, `logical`, `columns[].name`, `columns[].type` | scaffold | every Input tool, plus any tool `intake/mappings.yaml` maps as a source; `logical` is the mapping's, **`null` until intake has named it**; the producing anchor's RecordInfo |
| each output's `tool_id`, `logical`, `write_mode`, `pre_sql`, `post_sql`, `columns[].name`, `columns[].type` | scaffold | every Output tool, the mapping's `logical` and `mode`, the Output tool's PreSQL and PostSQL, the anchor that feeds it |
| an output's `keys` **when its write mode is `update_insert`** | scaffold | the mapping's `keys`, else the Output tool's update keys, upper-cased |
| `title`, `purpose`, `tier`, `language`, `language_reason` | analyzer | the digest, the cookbook |
| every column's `nullable`; an input's `keys`; an output's `keys` unless it is an `update_insert` | analyzer | pre-filled with the neutral `true` / `[]` |
| an output's `grain`, `row_relation`, `ordering`, `tolerances`, `normalizations`; every output column's `meaning` | analyzer | never pre-filled (`""`, `null`, `{}`, `[]`) |
| `steps`, `no_effect`, `rules`, `parity_risks`, `questions`, `answered` | analyzer | never pre-filled |

The derivation (`scripts/objective_scaffold.py`, which uses `lib/mechanical.py` -- the same derivations the
per-segment contract scaffold made, moved, not rewritten):

- **Inputs and outputs** are in tool-id order. An Input tool is an input; so is any tool the mapping names under
  `sources`. An Output tool is an output.
- **Columns** are the producing anchor's DAG meta: names upper-cased (every consumer compares them upper-cased),
  types from `lib.types_map.alteryx_to_snowflake`, except that a variable-length string (`V_String`, `V_WString`)
  keeps its DAG size, `VARCHAR(<size>)`. The type map's own unsized `VARCHAR` is accepted as the same type; any
  other type or length is not. A fixed-width `String(n)` is always `VARCHAR(n)`. An Output tool nothing feeds
  uses its own field list.
- **`write_mode`** is the mapping's `mode` in the Output tool's own vocabulary: `overwrite`, `append`, and
  `merge` as `update_insert`; without a mapping mode, the Output tool's own (`overwrite`, `append`,
  `update_insert`, `truncate_append`). `pre_sql` / `post_sql` are the Output tool's, `null` when it has none.
- **Target-only columns.** A target that keeps its existing rows (`append`, `update_insert`) may list, after the
  stream's own columns, columns only the existing table has (`wf_0003`'s `LOADED_FLAG`, filled by its PostSQL).
  An overwrite target may not.
- **Gaps.** A field the scaffold cannot derive -- a type the type map does not cover, an anchor with no field
  list, an Output tool with no write mode, an `update_insert` with no keys anywhere -- is reported as a `note:`
  line on stderr and left as it is: it is the analyzer's to declare, and the checker then checks only its shape
  (a declared column type: a Snowflake type name with an optional `(precision[, scale])`, what `compile_check.py`
  can build a table from).
- **`intake/mappings.yaml` may be absent.** Analyze starts before intake is READY, so every `logical` is then
  `null`; `--apply` fills the names once intake has named them (golden does this first).
- **`--prefill`** writes the skeleton only where `objective.json` does not exist -- a resumed run keeps the
  analyzer's own file. **`--apply`** re-applies the mechanical fields over an existing file and touches nothing
  else: every judgment field, and any other key the analyzer added, stays exactly as written. An entry the
  analyzer dropped is restored from the skeleton and one it invented is removed; a column it dropped is restored
  and one it added is removed (the judgment fields of a surviving column are kept, matched by name, whatever its
  case). The file is rewritten only when something changed. An entry whose `tool_id` the analyzer lost is found by
  its `logical` name.

## Steps and tool accounting

`steps[]` is the workflow as a person would describe it, typically two to six steps, not one per tool. A step has
an `id` (`s<N>`: `s1`, `s2`, ...; matched with `fullmatch` against `lib.objective.STEP_ID`), a `name`, `does` (what
it does, in a sentence), `tools` (the Alteryx tool ids it covers) and `feeds` (the steps that read its rows, and
`out:<output tool id>` for an output it writes). An Output tool lives in the step that produces its rows.

**Every tool of `parsed/dag.json` is in exactly one step's `tools` or in `no_effect`**, with these rules:

- Tools inside a macro belong to the macro tool, which is itself a node: list the macro, not its inner tools.
- **Nodes of a data-less type -- containers, comments (TextBox), interface tools, actions and Browse tools
  (`lib.vocab.DATA_LESS_TYPES`) -- are exempt.** They carry no data, so a step need not cover them and they need
  not be in `no_effect`. Listing one in `no_effect` is allowed (`wf_0002` lists its Browse tool, with the reason
  "changes nothing"); listing one in two places is not.
- `no_effect[]` is `{tool_id, reason}`: a data-carrying tool the analyzer judges to change nothing. An Input tool is
  never one: its rows are read, so it belongs to the step that reads them.
- Every output's tool is inside some step.
- A step's `feeds` never names the step itself.

## The vocabularies of the judgment fields

These are the words the segment contracts used, unchanged, because `compare.py` reads them unchanged (they live
in `lib/judgment.py`, which `objective_check.py` holds the analyzer to):

- **`row_relation`**: `1:1`, `filter`, `aggregate`, `expand`.
- **`ordering`**: exactly `{"keys": [...], "alteryx_deterministic": true|false, "order_dependent_columns": [...]}`;
  `keys` are any column the workflow carries (an ordering key may be dropped before the output: `wf_0003` orders
  by `POSTED_DT`, which its Summarize drops), `order_dependent_columns` are columns of this output.
- **`tolerances`**: keyed by output column, each `{"float_abs": <number >= 0>, "float_rel": <number >= 0>}`
  (either or both; `compare.py`'s per-column override).
- **`normalizations`**: `"trim:<COLUMN>"` / `"upper:<COLUMN>"` on output columns (`compare.py`'s directives).
- **`parity_risks`**: exactly `{tool_id, class, note}` -- a tool of the workflow (or `<macro>/<tool>` for a tool
  its macro's own sub-DAG has), a class of `lib.vocab.DIFF_CLASSES` (`ROUNDING`, `ORDERING`, `NULL_SEMANTICS`,
  `TRUNCATION`, `TYPE`, `LOGIC`, `GOLDEN_DATA`, `UNKNOWN`) and a non-empty note saying the Alteryx behaviour to
  keep, in words.
- **`language`** is `sql` or `python`. `python` needs a non-empty `language_reason` (why SQL cannot express the
  logic); `sql` takes `null` (an empty string counts as none). A workflow containing a node that
  `parsers.plugin_map.node_class` classes `snowpark` (the Python tool; a tool inside a macro counts) must be
  `python`. A node classed `manual` (`run_command`, ...) makes the tier `T3`.
- **`tier`**: `T1` (everything translates as SQL), `T2` (it translates, but not as SQL alone), `T3` (it cannot be
  translated: the translate stage ends at `MANUAL`).
- **`write_mode`**: `overwrite`, `append`, `truncate_append`, `update_insert` (`lib.objective.WRITE_MODES`).

## Questions

`questions[]` are the analyzer's unsure points: `{id: "O<N>", tools: [...], question, assumption}`, each stating the
assumption the analyzer would make. The analyzer asks only what the golden data cannot settle. A person answers
them in `intake/objective_questions.md`, in the box format intake uses:

```
# Objective questions - wf_0001
- [ ] O1 - (tools 3) Is excluding the WEST region deliberate, or a leftover test filter?
      Assumed if confirmed: deliberate; WEST orders go to EXCLUDED_ORDERS.
      Answer:
```

An answer is the text after `Answer:` on a ticked item, or `manifest.answers["O1"]`; the file wins when both exist
(intake's precedence). A ticked item with no text accepts the assumption. `--write` keeps what is typed, fills a
blank `Answer:` from the manifest and ticks it, and removes the file when there is nothing to ask; `--status` is
0 when every question has an answer; `--answers` prints them as JSON (`{"O1": "<answer>"}`, a bare tick giving the
assumption's text). The revision session applies the answers to the objective and moves each answered question to
`answered` (`{id, question, answer}`); the orchestrator hands it the `--answers` output as a fenced data block, so
it applies what the gate counted. Question ids are unique across both lists.

## When the orchestrator applies it

1. **Before the analyzer:** `digest.py <wf>` writes the digest the analyzer reads; `objective_scaffold.py <wf>
   --prefill` writes the skeleton (only where none exists).
2. **After the analyzer:** `objective_scaffold.py <wf> --apply`, then `objective_check.py <wf>` -- a refusal is the
   analyze park reason, the first problem line; then `objective_questions.py <wf> --write`.
3. **Resumed, once every `O...` question is answered:** one analyzer revision session applies the answers to the
   objective (it is given `objective_questions.py <wf> --answers` as data; same checks), then `done`.
4. **Golden:** `objective_scaffold.py --apply` then `objective_check.py --ready` first -- `--ready` additionally
   requires every `logical` to be non-null (intake has named it) and `questions` empty.

The analyzer may run `python scripts/objective_check.py <wf>` itself and fixes what it reports before it finishes.

## The checker's rules

`objective_check.py <wf> [--ready]` prints one line per problem to stderr as `<field>: <problem>; expected
<what>` -- for example `steps: tool 12 is in no step and not in no_effect; expected every tool id of
parsed/dag.json exactly once` -- and exits 1; 0 when the objective passes, 2 on a usage error (no
`parsed/dag.json`, a workflow id that is not one). It writes nothing.

- **Mechanical agreement** with a fresh derivation (in memory): `workflow`, every entry's `tool_id` and
  `logical`, every output's `write_mode`, `pre_sql`, `post_sql`, column names in order and types (with the two
  accepted spellings above), an `update_insert` output's `keys`; an input or output the DAG does not show and a
  missing one are both named, and so is a wrong order.
- **Shape of what scripts read:** `title`, `purpose` and every output's `grain` non-empty; `tier` and `language`
  in their vocabularies (never null); the `language_reason` and forced-language / forced-tier rules above; every
  step's `id`, `name`, `does`, non-empty `tools` (each a top-level tool of the DAG) and `feeds` (each a step or
  `out:<output tool id>`); the tool accounting above; every column's `nullable` a boolean and every output
  column's `meaning` non-empty; every entry's `keys` a list of its own columns.
- **Judgment well-formed:** per output, `row_relation`, `ordering`, `tolerances` and `normalizations` as above;
  `parity_risks` exactly `{tool_id, class, note}`; `rules` non-empty sentences; question ids `O<N>` (matched
  with `fullmatch` against `lib.objective.QUESTION_ID`), unique across `questions` and `answered`, each question
  naming tools of the DAG and carrying its text and assumption.
- **`--ready`:** every `logical` non-null; `questions` empty.
- **`objective.md`**, when it exists: its first heading is `# <id> — <title>` and it has the fixed headings
  `## Purpose`, `## Inputs`, `## Outputs` (one `###` subsection per output), `## Steps`, `## Business rules`,
  `## Alteryx behaviour to keep`, `## Questions`, in that order. A missing `objective.md` is the orchestrator's
  missing-output check, not this script's.

## An output's `keys` that repeat in the golden data

The checker cannot see the data, so it cannot tell whether an output's declared `keys` identify a row; `compare.py`
can, and does, per golden set (live hardening, Task L11). When the declared keys repeat in the **expected**
(golden) rows of an output, that output is compared exactly as one with `"keys": []` -- the row multiset with
nearest-match pairing, the same tolerances, checks and approvals -- and its report carries the advisory
`keys_not_unique`. The advisory is never a failure and never `needs_human` by itself. An output whose real rows
may repeat an id should declare `"keys": []`, or a key that is unique (`wf_0001`'s `EXCLUDED_ORDERS` declares none:
its `edge` golden set holds two identical rows).

## The digest

`digest.py <wf>` writes `digest.md`, never by a model: the same `parsed/dag.json` gives byte-identical text. A
header (workflow id, tool count, output count), the workflow's constants, then one section per output (an Output
tool, or any other tool nothing reads) listing the tools upstream of it in topological order; a tool that feeds
several outputs is listed once, in a leading "Shared" section, and each output's section names the shared tools
it uses by id. One line per tool:

```
T3 Filter  [REGION] != "WEST"  ← T2   (T → T4, F → T8)  — Exclude WEST
```

The line carries the tool id, its type, what decides its behaviour (the filter expression, each formula field and
its expression, join keys, summarize group-bys and actions, sort keys, select renames and retypes, union mode,
unique keys, sample settings, multi-row expressions, cross-tab and transpose keys, input and output names with
their write mode, PreSQL and PostSQL, a macro's name and answers, a Python tool's script) kept whole -- the
expression is the logic --, its inbound tools (`T3.T` is anchor T of tool 3), where each anchor of a tool with
several goes (`none` for an anchor a Filter, Unique or Join leaves unwired: those rows are dropped) and its
annotation. A macro's inner tools follow it, indented and numbered `T<macro>/<tool>`. Containers, comments and
Browse tools carry no data and are counted in the header, not listed.

Nothing here has run on Snowflake or Alteryx: the scripts compare JSON the analyzer wrote with JSON the pipeline
derived.
