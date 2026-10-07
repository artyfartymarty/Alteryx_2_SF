- This repo migrates Alteryx workflows to Snowflake. Never deploy, never write outside the current workflow's folder.
- Sandbox schemas: MIG_WORK (the target schema while a build file is validated), MIG_GOLDEN (golden data). Never reference production schemas by name in SQL.
- Sources come only from the objective's `inputs[].logical` (workflows/<id>/objective.json, named by workflows/<id>/intake/mappings.yaml or mappings/global.yaml).
- Numbers about data (counts, diffs, tolerances) come only from scripts/compare.py output, which scripts/validate_build.py calls. Never estimate.
- The pipeline is objective-first (docs/reference/objective.md). The analyzer writes the workflow's objective: what it is for, what each output contains, the business steps (citing Alteryx tool ids), the rules, the Alteryx behaviour to keep and the points it is unsure about. The translator then writes ONE minimal file, workflows/<id>/build.sql (or build.py, only when objective.json's `language` is `python`): one write statement per output, a CTE or temporary table only where it removes repetition or helps reading, never one CTE per Alteryx tool.
- A build file follows docs/reference/build-file.md: inputs only as `{{src}}.<LOGICAL>`, outputs only as `{{tgt}}.<LOGICAL>`, no procedures, no `LET`/`IDENTIFIER`, and a `-- step s1` (`# step s1` in Python) marker for every step of the objective. scripts/compile_check.py checks it and lints it for minimality: fix a flag or justify it in translation_notes.md as `lint <kind> <name>: kept because <reason>`. There is no validator agent: scripts/validate_build.py compares the build file's outputs with the golden data, and scripts/render_mapping.py draws docs/mapping.md from the step markers.
- If you are unsure what a tool does, say so as a question in the objective (the analyzer) or in your notes; do not guess semantics.
- Run every Python script as `python scripts/<name>.py …`, never through a `.venv/…` path: the orchestrator puts the
  project's interpreter first on PATH for every agent session, so `python` is that interpreter (a run root has no
  `.venv` of its own).
- Session rules (the orchestrator appends the same seven to every agent task):
  1. In every tool call use a path relative to the repository root (workflows/<id>/…, where <id> is the workflow
     your task names); never type an absolute path. The one exception is a temporary file that a tool result
     itself says holds that result's full output: read it with view or grep, exactly as named.
  2. List files with glob, read them with view and search them with grep; the shell only runs the commands your
     agent file names, its scripts as `python scripts/<name>.py …`, one command per shell call: `;`, `&&`, `|`,
     redirection and `$` are refused, so read a script's report file with view afterwards. `python` is already the
     project's interpreter: never check it.
  3. Among the workflows, only workflows/<id>/ is yours; never open another workflow's folder.
  4. A refused tool call is final: do not retry it in another form or through another tool. Continue with what is
     allowed, and if you cannot finish without it, say so in your notes (or, if your role keeps none, in the file
     you were asked to write) and stop.
  5. Do not read this pipeline's own scripts/ or orchestrator/ source to debug a difference: the validation report,
     the objective, the cookbook and docs/reference/ are the evidence.
  6. Never read the golden data in bulk: the objective describes every column. When an example helps, read at most one
     golden set's inputs (e.g. normal); validate_build.py compares the rest.
  7. There is no network in these sessions: every page you need is in the repository (`cookbook/`, `docs/reference/`).
