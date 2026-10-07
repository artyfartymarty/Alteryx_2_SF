"""`objective_questions.py`: the analyzer's unsure points, asked in the same sitting as intake's table
questions (objective-first design §4.3).

`--write` renders `intake/objective_questions.md` from `objective.json`'s `questions`, in the box format
intake uses so a person answers both files the same way; it keeps an answer already typed in the file and
fills a blank `Answer:` from `manifest.answers["O<n>"]`, and removes the file once there is nothing to
ask. `--status` says whether every question has an answer. An answer is the text after `Answer:` on a
ticked item, or the manifest's; the file wins when both exist (intake's precedence), and a ticked item with
no text confirms the assumption the question states.

No canned objective has a question (the analyzer asks only what the golden data cannot settle), so these
tests use `tests/objective_fixtures/wf_0001_open_question.json`: wf_0001's objective with one open question.

Nothing here has run on Snowflake or Alteryx.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import objective_check
import objective_questions as oq
from lib.io import load_manifest, read_json, save_manifest, write_json
from lib.paths import Repo
from tests.helpers import prepare_objective_workflow

WF = "wf_0001"
FIXTURE = Path(__file__).parent / "objective_fixtures" / "wf_0001_open_question.json"

ONE_QUESTION = """\
# Objective questions - wf_0001
- [ ] O1 - (tools 3) Is excluding the WEST region deliberate, or a leftover test filter?
      Assumed if confirmed: deliberate; WEST orders go to EXCLUDED_ORDERS.
      Answer:
"""


@pytest.fixture
def repo(tmp_path) -> Repo:
    repo = prepare_objective_workflow(tmp_path, WF, canned=False)
    shutil.copy(FIXTURE, repo.wf(WF, "objective.json"))
    return repo


def questions_file(repo: Repo) -> Path:
    return repo.wf(WF, "intake", "objective_questions.md")


def second_question() -> dict:
    return {"id": "O2", "tools": ["4", "5"], "question": "Is the 10-unit discount threshold current?",
            "assumption": "current"}


def set_questions(repo: Repo, questions: list[dict]) -> None:
    objective = read_json(repo.wf(WF, "objective.json"))
    objective["questions"] = questions
    write_json(repo.wf(WF, "objective.json"), objective)


def set_manifest_answers(repo: Repo, answers: dict) -> None:
    manifest = load_manifest(repo, WF)
    manifest.setdefault("answers", {}).update(answers)
    save_manifest(repo, manifest)


def run(repo: Repo, mode: str) -> int:
    return oq.main([WF, mode, "--root", str(repo.root)])


def test_the_fixture_is_a_valid_objective_with_one_open_question(repo):
    assert objective_check.check(repo, WF) == []
    assert [q["id"] for q in read_json(repo.wf(WF, "objective.json"))["questions"]] == ["O1"]
    assert objective_check.check(repo, WF, ready=True) != [], "an open question blocks --ready"


# --- --write -------------------------------------------------------------------------------------------

def test_write_renders_the_file_in_intakes_box_format(repo, capsys):
    assert run(repo, "--write") == 0
    assert questions_file(repo).read_text(encoding="utf-8") == ONE_QUESTION
    assert capsys.readouterr().out.strip() == str(questions_file(repo))


def test_the_file_is_utf8_lf_and_identical_on_a_second_write(repo):
    run(repo, "--write")
    first = questions_file(repo).read_bytes()
    assert b"\r" not in first
    run(repo, "--write")
    assert questions_file(repo).read_bytes() == first


def test_several_questions_are_listed_in_order_and_a_question_without_tools_has_no_parenthesis(repo):
    set_questions(repo, [read_json(repo.wf(WF, "objective.json"))["questions"][0], second_question(),
                         {"id": "O3", "tools": [], "question": "Why?", "assumption": "Because."}])
    run(repo, "--write")
    lines = questions_file(repo).read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.startswith("- [")] == [
        "- [ ] O1 - (tools 3) Is excluding the WEST region deliberate, or a leftover test filter?",
        "- [ ] O2 - (tools 4, 5) Is the 10-unit discount threshold current?",
        "- [ ] O3 - Why?"]
    assert lines.count("      Answer:") == 3


def test_a_question_that_spans_lines_is_one_line_in_the_file(repo):
    set_questions(repo, [{"id": "O1", "tools": ["3"], "question": "Is it\n  deliberate?",
                          "assumption": "Yes,\nit is."}])
    run(repo, "--write")
    text = questions_file(repo).read_text(encoding="utf-8")
    assert "- [ ] O1 - (tools 3) Is it deliberate?\n      Assumed if confirmed: Yes, it is.\n" in text


def test_write_keeps_an_answer_already_typed_in_the_file(repo):
    run(repo, "--write")
    typed = ONE_QUESTION.replace("- [ ]", "- [x]").replace("Answer:", "Answer: yes, it is deliberate")
    questions_file(repo).write_text(typed, encoding="utf-8")
    set_questions(repo, [read_json(repo.wf(WF, "objective.json"))["questions"][0], second_question()])
    run(repo, "--write")
    lines = questions_file(repo).read_text(encoding="utf-8").splitlines()
    assert lines[1] == "- [x] O1 - (tools 3) Is excluding the WEST region deliberate, or a leftover test filter?"
    assert lines[3] == "      Answer: yes, it is deliberate"
    assert "- [ ] O2 - (tools 4, 5) Is the 10-unit discount threshold current?" in lines
    assert oq.answers(repo, WF) == {"O1": "yes, it is deliberate"}


def test_write_keeps_text_typed_without_ticking_the_box_but_it_is_not_an_answer_yet(repo):
    run(repo, "--write")
    questions_file(repo).write_text(ONE_QUESTION.replace("Answer:", "Answer: probably"), encoding="utf-8")
    run(repo, "--write")
    assert "      Answer: probably" in questions_file(repo).read_text(encoding="utf-8")
    assert oq.answers(repo, WF) == {}


def test_a_blank_answer_is_filled_from_the_manifest_and_the_box_is_ticked(repo):
    set_manifest_answers(repo, {"O1": "it is a deliberate rule"})
    run(repo, "--write")
    lines = questions_file(repo).read_text(encoding="utf-8").splitlines()
    assert lines[1].startswith("- [x] O1 - ")
    assert lines[3] == "      Answer: it is a deliberate rule"
    assert oq.answers(repo, WF) == {"O1": "it is a deliberate rule"}


def test_a_ticked_blank_answer_is_filled_from_the_manifest_too(repo):
    run(repo, "--write")
    questions_file(repo).write_text(ONE_QUESTION.replace("- [ ]", "- [x]"), encoding="utf-8")
    set_manifest_answers(repo, {"O1": "from the manifest"})
    run(repo, "--write")
    assert "      Answer: from the manifest" in questions_file(repo).read_text(encoding="utf-8")


def test_the_file_wins_over_the_manifest_when_both_have_an_answer(repo):
    run(repo, "--write")
    questions_file(repo).write_text(
        ONE_QUESTION.replace("- [ ]", "- [x]").replace("Answer:", "Answer: from the file"), encoding="utf-8")
    set_manifest_answers(repo, {"O1": "from the manifest"})
    assert oq.answers(repo, WF) == {"O1": "from the file"}
    run(repo, "--write")
    assert "      Answer: from the file" in questions_file(repo).read_text(encoding="utf-8")


def test_a_ticked_item_with_no_text_confirms_the_assumption(repo):
    run(repo, "--write")
    questions_file(repo).write_text(ONE_QUESTION.replace("- [ ]", "- [x]"), encoding="utf-8")
    assert oq.answers(repo, WF) == {"O1": "deliberate; WEST orders go to EXCLUDED_ORDERS."}
    run(repo, "--write")
    assert questions_file(repo).read_text(encoding="utf-8") == ONE_QUESTION.replace("- [ ]", "- [x]"), \
        "the file keeps what the person typed; it does not copy the assumption in"


def test_an_answer_on_several_lines_is_read_as_one(repo):
    run(repo, "--write")
    questions_file(repo).write_text(
        ONE_QUESTION.replace("- [ ]", "- [x]").replace("Answer:", "Answer: yes,\n      deliberate since 2019"),
        encoding="utf-8")
    assert oq.answers(repo, WF) == {"O1": "yes, deliberate since 2019"}


def test_a_question_that_left_objective_json_leaves_the_file(repo):
    set_questions(repo, [read_json(repo.wf(WF, "objective.json"))["questions"][0], second_question()])
    run(repo, "--write")
    set_questions(repo, [second_question()])
    run(repo, "--write")
    lines = questions_file(repo).read_text(encoding="utf-8").splitlines()
    assert [line.split()[3] for line in lines if line.startswith("- [")] == ["O2"]


def test_with_no_questions_the_file_is_removed_and_the_exit_is_0(repo, capsys):
    run(repo, "--write")
    assert questions_file(repo).exists()
    set_questions(repo, [])
    capsys.readouterr()
    assert run(repo, "--write") == 0
    assert not questions_file(repo).exists()
    assert "no objective questions" in capsys.readouterr().out


def test_with_no_questions_and_no_file_nothing_is_created(repo):
    set_questions(repo, [])
    assert run(repo, "--write") == 0
    assert not questions_file(repo).exists()


def test_an_answered_question_in_the_answered_list_is_not_asked_again(repo):
    objective = read_json(repo.wf(WF, "objective.json"))
    objective["answered"] = objective["questions"]
    objective["answered"][0]["answer"] = "deliberate"
    del objective["answered"][0]["tools"], objective["answered"][0]["assumption"]
    objective["questions"] = []
    write_json(repo.wf(WF, "objective.json"), objective)
    run(repo, "--write")
    assert not questions_file(repo).exists()


# --- --status --------------------------------------------------------------------------------------------

def test_status_is_1_and_names_the_open_ids_while_a_question_has_no_answer(repo, capsys):
    set_questions(repo, [read_json(repo.wf(WF, "objective.json"))["questions"][0], second_question()])
    assert run(repo, "--status") == 1
    assert capsys.readouterr().out.strip() == "wf_0001: 2 open objective questions: O1, O2"
    run(repo, "--write")
    set_manifest_answers(repo, {"O2": "yes"})
    assert run(repo, "--status") == 1
    assert capsys.readouterr().out.strip().endswith("1 open objective question: O1")


def test_status_is_0_once_every_question_is_answered_whether_in_the_file_or_the_manifest(repo, capsys):
    set_questions(repo, [read_json(repo.wf(WF, "objective.json"))["questions"][0], second_question()])
    run(repo, "--write")
    set_manifest_answers(repo, {"O2": "yes"})
    text = questions_file(repo).read_text(encoding="utf-8").replace("- [ ] O1", "- [x] O1")
    questions_file(repo).write_text(text.replace("Answer:\n", "Answer: deliberate\n", 1), encoding="utf-8")
    capsys.readouterr()
    assert run(repo, "--status") == 0
    assert capsys.readouterr().out.strip() == "wf_0001: every objective question is answered (2)"


def test_status_is_0_when_there_are_no_questions(repo, capsys):
    set_questions(repo, [])
    assert run(repo, "--status") == 0
    assert capsys.readouterr().out.strip() == "wf_0001: no objective questions"


def test_status_does_not_need_the_file_to_exist_when_the_manifest_has_the_answers(repo):
    set_manifest_answers(repo, {"O1": "yes"})
    assert not questions_file(repo).exists()
    assert run(repo, "--status") == 0


def test_status_notes_text_typed_without_ticking_the_box(repo, capsys):
    run(repo, "--write")
    questions_file(repo).write_text(ONE_QUESTION.replace("Answer:", "Answer: probably"), encoding="utf-8")
    capsys.readouterr()
    assert run(repo, "--status") == 1
    assert "O1 (an answer is typed but the box is not ticked)" in capsys.readouterr().out


def test_status_and_write_write_nothing_they_should_not(repo):
    before = read_json(repo.wf(WF, "objective.json"))
    run(repo, "--status")
    assert not questions_file(repo).exists(), "--status never writes the file"
    run(repo, "--write")
    assert read_json(repo.wf(WF, "objective.json")) == before, "objective.json is never changed"


# --- --answers ------------------------------------------------------------------------------------------

def test_answers_prints_the_resolved_answers_as_json_in_the_questions_order(repo, capsys):
    set_questions(repo, [*read_json(repo.wf(WF, "objective.json"))["questions"], second_question()])
    run(repo, "--write")
    questions_file(repo).write_text(
        ONE_QUESTION.replace("- [ ]", "- [x]")
        + "- [ ] O2 - (tools 4, 5) Is the 10-unit discount threshold current?\n"
          "      Assumed if confirmed: current\n      Answer:\n", encoding="utf-8")
    set_manifest_answers(repo, {"O2": "yes, current as of 2026"})
    capsys.readouterr()
    assert run(repo, "--answers") == 0
    printed = json.loads(capsys.readouterr().out)
    assert list(printed) == ["O1", "O2"]
    assert printed["O1"] == "deliberate; WEST orders go to EXCLUDED_ORDERS.", "a ticked box with no text accepts the assumption"
    assert printed["O2"] == "yes, current as of 2026", "the manifest's answer stands when the box is not ticked"
    assert printed == oq.answers(repo, WF)


def test_answers_is_an_empty_object_while_nothing_is_answered_and_writes_nothing(repo, capsys):
    capsys.readouterr()
    assert run(repo, "--answers") == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert not questions_file(repo).exists()


def test_a_typed_answer_that_is_not_ascii_is_printed_as_it_is(repo, capsys):
    run(repo, "--write")
    questions_file(repo).write_text(
        ONE_QUESTION.replace("- [ ]", "- [x]").replace("Answer:", "Answer: oui — c'est voulu (é)"), encoding="utf-8")
    capsys.readouterr()
    run(repo, "--answers")
    out = capsys.readouterr().out
    assert "oui — c'est voulu (é)" in out and "\\u" not in out


# --- usage ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["--write", "--status", "--answers"])
def test_without_an_objective_it_is_exit_2(tmp_path, mode, capsys):
    prepare_objective_workflow(tmp_path, WF, canned=False)
    assert oq.main([WF, mode, "--root", str(tmp_path)]) == 2
    assert "objective.json" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["--write", "--status", "--answers"])
def test_an_objective_that_is_not_a_json_object_is_exit_2(repo, mode, capsys):
    repo.wf(WF, "objective.json").write_text("[1]\n", encoding="utf-8")
    assert run(repo, mode) == 2
    assert "objective.json" in capsys.readouterr().err


def test_a_question_without_an_id_is_exit_2_naming_it(repo, capsys):
    set_questions(repo, [{"question": "no id"}])
    assert run(repo, "--status") == 2
    assert "questions[0]" in capsys.readouterr().err


def test_a_mode_is_required_and_only_one(repo):
    with pytest.raises(SystemExit) as none_given:
        oq.main([WF, "--root", str(repo.root)])
    assert none_given.value.code == 2
    with pytest.raises(SystemExit) as both:
        oq.main([WF, "--write", "--status", "--root", str(repo.root)])
    assert both.value.code == 2


def test_a_workflow_id_that_is_not_one_is_exit_2(tmp_path):
    assert oq.main(["../x", "--status", "--root", str(tmp_path)]) == 2
