"""Render and read the objective's open questions (objective-first design §4.3).

    objective_questions.py <wf_id> (--write | --status | --answers) [--root .]

The analyzer's unsure points -- a filter or constant whose purpose is unclear, an output nobody seems to
read, an expression that looks like a bug -- are `objective.json`'s `questions[]`, each with the tools it is
about, its text and the assumption the analyzer would make. A person answers them in the same sitting as
intake's table-mapping questions, so they are written in the box format intake uses
(`intake/open_questions.md`):

    # Objective questions - wf_0001
    - [ ] O1 - (tools 3) Is excluding the WEST region deliberate, or a leftover test filter?
          Assumed if confirmed: deliberate; WEST orders go to EXCLUDED_ORDERS.
          Answer:

An answer is the text after `Answer:` on a ticked item, or `manifest.answers["O1"]`; the file wins when both
exist (the same precedence intake uses). A ticked item with no text accepts the assumption the question
states, which is then its answer. Text typed on an item whose box is not ticked is kept but is not an answer
yet.

  --write    render `intake/objective_questions.md` from `objective.json`, keeping every answer already
             typed in the file and filling a blank `Answer:` from the manifest (and ticking the box); a
             question that has left `objective.json` leaves the file, and with no questions at all the file
             is removed. Exit 0.
  --status   exit 0 when every question has an answer (or there are none), 1 otherwise, printing the open
             ids. Writes nothing.
  --answers  print `answers()` as JSON, `{"O1": "<answer>", ...}` in the questions' order (`{}` when none is
             answered), for the orchestrator's analyzer revision session. Writes nothing; exit 0.

Exit codes: 0 as above; 1 (`--status`) open questions; 2 usage -- no `objective.json`, one that is not a JSON
object or whose questions are malformed, a workflow id that is not one.

`answers(repo, wf_id)` gives every answered question's text, for the revision session (`--answers`) and for tests.

Nothing here has run on Snowflake or Alteryx: it reads and writes Markdown and JSON.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Sequence

from lib.io import load_manifest, write_text
from lib.objective import QUESTION_ID
from lib.paths import Repo, add_root_arg
from objective_scaffold import ObjectiveUnreadable, read_objective

_ITEM = re.compile(r"^-\s\[([ xX])\]\s+(O[1-9][0-9]*)\b")
_HEADING = re.compile(r"^#{1,6}\s")
_ANSWER = re.compile(r"^\s*answer\s*:\s*(.*)$", re.IGNORECASE)


def questions_path(repo: Repo, wf_id: str) -> Path:
    """`workflows/<id>/intake/objective_questions.md` (whether or not it exists)."""
    return repo.wf(wf_id, "intake", "objective_questions.md")


def _questions(repo: Repo, wf_id: str) -> list[dict]:
    """`objective.json`'s open questions. ObjectiveUnreadable / ValueError (naming the file) when it is missing,
    not a JSON object, or its questions are not `{id: O<N>, question, ...}` objects."""
    questions = read_objective(repo, wf_id).get("questions") or []
    if not isinstance(questions, list):
        raise ValueError("objective.json: questions is not a list; expected a list of open questions "
                         "(run scripts/objective_check.py)")
    for index, entry in enumerate(questions):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not QUESTION_ID.fullmatch(entry["id"]):
            raise ValueError(f"objective.json: questions[{index}] has no valid id; expected {{id: O<N>, tools, "
                             f"question, assumption}} (run scripts/objective_check.py)")
    return questions


def parse(text: str) -> dict[str, tuple[bool, str]]:
    """`{question id: (ticked, typed answer)}` for every `- [ ] O<N>` item of the file; the first item of an id
    wins. The answer is the text after `Answer:` and any lines that continue it, joined by single spaces."""
    items: dict[str, tuple[bool, str]] = {}
    current: tuple[str, bool, list[str]] | None = None

    def finish() -> None:
        if current is None or current[0] in items:
            return
        qid, ticked, lines = current
        answer = ""
        for at, line in enumerate(lines):
            match = _ANSWER.match(line)
            if match:
                answer = " ".join([match.group(1).strip(), *(rest.strip() for rest in lines[at + 1:])]).strip()
                break
        items[qid] = (ticked, " ".join(answer.split()))

    for raw in text.splitlines():
        line = raw.rstrip()
        item = _ITEM.match(line)
        if item:
            finish()
            current = (item.group(2), item.group(1).strip() != "", [])
        elif _HEADING.match(line):
            finish()
            current = None
        elif current is not None:
            current[2].append(line)
    finish()
    return items


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _manifest_answers(repo: Repo, wf_id: str) -> dict[str, str]:
    given = load_manifest(repo, wf_id).get("answers") or {}
    return {str(qid): str(value).strip() for qid, value in given.items() if value is not None and str(value).strip()}


def _typed(repo: Repo, wf_id: str) -> dict[str, tuple[bool, str]]:
    path = questions_path(repo, wf_id)
    return parse(path.read_text(encoding="utf-8")) if path.is_file() else {}


def answers(repo: Repo, wf_id: str) -> dict[str, str]:
    """`{question id: answer}` for every open question of `objective.json` that has one: the text typed after
    `Answer:` on a ticked item (a ticked item with no text confirms the assumption, so its answer is the
    assumption's text), else `manifest.answers[<id>]`. The file wins when both exist."""
    return _resolve(repo, wf_id, _questions(repo, wf_id))[0]


def _resolve(repo: Repo, wf_id: str, questions: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """(answers, hints): the answered questions' text, and for each open question with text typed but its box
    not ticked, a note saying so."""
    typed, manifest = _typed(repo, wf_id), _manifest_answers(repo, wf_id)
    found: dict[str, str] = {}
    hints: dict[str, str] = {}
    for question in questions:
        qid = question["id"]
        ticked, text = typed.get(qid, (False, ""))
        if ticked:
            found[qid] = text or manifest.get(qid) or _one_line(question.get("assumption"))
        elif qid in manifest:
            found[qid] = manifest[qid]
        elif text:
            hints[qid] = "an answer is typed but the box is not ticked"
    return found, hints


def render(wf_id: str, questions: list[dict], typed: dict[str, tuple[bool, str]], manifest: dict[str, str]) -> str:
    """The file's text. A blank answer takes the manifest's (and the box is ticked); an answer typed in the file
    is kept as it is."""
    out = [f"# Objective questions - {wf_id}"]
    for question in questions:
        qid = question["id"]
        ticked, text = typed.get(qid, (False, ""))
        if not text and manifest.get(qid):
            ticked, text = True, manifest[qid]
        tools = ", ".join(str(t) for t in question.get("tools") or [])
        out.append(f"- [{'x' if ticked else ' '}] {qid} - " + (f"(tools {tools}) " if tools else "")
                   + _one_line(question.get("question")))
        out.append(f"      Assumed if confirmed: {_one_line(question.get('assumption'))}")
        out.append("      Answer:" + (f" {text}" if text else ""))
    return "\n".join(out) + "\n"


def write(repo: Repo, wf_id: str) -> Path | None:
    """Writes `intake/objective_questions.md`; returns its path, or None when there are no questions (the file,
    if there was one, is removed)."""
    questions = _questions(repo, wf_id)
    path = questions_path(repo, wf_id)
    if not questions:
        if path.exists():
            path.unlink()
        return None
    write_text(path, render(wf_id, questions, _typed(repo, wf_id), _manifest_answers(repo, wf_id)))
    return path


def status(repo: Repo, wf_id: str) -> tuple[list[str], list[dict], dict[str, str]]:
    """(open ids, every question, hints for the open ones)."""
    questions = _questions(repo, wf_id)
    found, hints = _resolve(repo, wf_id, questions)
    return [q["id"] for q in questions if q["id"] not in found], questions, hints


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wf_id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="render intake/objective_questions.md from objective.json")
    mode.add_argument("--status", action="store_true", help="exit 0 when every question has an answer, 1 otherwise")
    mode.add_argument("--answers", action="store_true", help="print every answered question's answer as JSON")
    add_root_arg(parser)
    args = parser.parse_args(argv)
    repo = Repo(args.root)
    try:
        if args.write:
            path = write(repo, args.wf_id)
            print(path if path is not None else f"{args.wf_id}: no objective questions; no objective_questions.md")
            return 0
        if args.answers:
            print(json.dumps(answers(repo, args.wf_id), ensure_ascii=False, indent=2))
            return 0
        open_ids, questions, hints = status(repo, args.wf_id)
    except (ObjectiveUnreadable, FileNotFoundError, ValueError) as exc:  # exit 2: nothing to read, or not readable
        print(f"objective_questions: {exc}", file=sys.stderr)
        return 2
    except Exception:                                       # exit 2: a crash, never a verdict
        traceback.print_exc()
        return 2
    if not questions:
        print(f"{args.wf_id}: no objective questions")
        return 0
    if not open_ids:
        print(f"{args.wf_id}: every objective question is answered ({len(questions)})")
        return 0
    many = len(open_ids) != 1
    listed = ", ".join(f"{qid} ({hints[qid]})" if qid in hints else qid for qid in open_ids)
    print(f"{args.wf_id}: {len(open_ids)} open objective question{'s' if many else ''}: {listed}")
    return 1


if __name__ == "__main__":
    from lib.console import utf8_console
    utf8_console()
    sys.exit(main())
