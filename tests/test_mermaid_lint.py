"""`lib/mermaid_lint.py`: the structural lint the README's diagrams and the generated `docs/mapping.md`
diagram share (design §4.9). Rendering itself is checked in a browser with Mermaid 11, never here.
"""
from __future__ import annotations

import pytest

from lib.mermaid_lint import escape_label, lint_mermaid, mermaid_blocks

FLOWCHART = """flowchart LR
  subgraph AX["Alteryx · sales_summary.yxmd"]
    s1["s1 · Read and clean orders<br/>T1 Input · T2 Select"]
  end
  subgraph CODE["build.sql"]
    r1["lines 5-9 · TEMPORARY TABLE orders"]
  end
  s1 -.-> r1
"""
SEQUENCE = """sequenceDiagram
  participant A
  participant B
  loop each segment
    A->>B: validate
    alt ok
      B-->>A: PASS
    else not ok
      B-->>A: FAIL
    end
  end
"""
STATE = """stateDiagram-v2
  [*] --> PENDING
  PENDING --> DONE
"""


@pytest.mark.parametrize("block", [FLOWCHART, SEQUENCE, STATE])
def test_a_well_formed_diagram_has_no_problems(block):
    assert lint_mermaid(block) == []


def test_leading_blank_lines_before_the_diagram_type_are_fine():
    assert lint_mermaid("\n\n" + FLOWCHART) == []


@pytest.mark.parametrize("block", ["graph TD\n  a --> b\n", "pie\n  \"a\": 1\n", "flowchartLR\n", "", "  \n"])
def test_an_unknown_diagram_type_is_one_problem_naming_the_expected_types(block):
    problems = lint_mermaid(block)
    assert len(problems) == 1
    assert problems[0].startswith("type:") and "flowchart" in problems[0] and "sequenceDiagram" in problems[0]


def test_a_tab_is_a_problem_with_its_line():
    problems = lint_mermaid("flowchart LR\n  a --> b\n\tb --> c\n")
    assert len(problems) == 1 and problems[0].startswith("tab:") and "line 3" in problems[0]


@pytest.mark.parametrize("opening, closing, name", [("[", "]", "square"), ("(", ")", "round"), ("{", "}", "curly")])
def test_each_unbalanced_bracket_kind_is_its_own_problem(opening, closing, name):
    block = f"flowchart LR\n  a{opening}x{closing} --> b{opening}y\n"
    problems = lint_mermaid(block)
    assert len(problems) == 1 and problems[0].startswith("brackets:") and name in problems[0]
    assert opening in problems[0] and closing in problems[0]


def test_several_unbalanced_bracket_kinds_give_several_problems():
    problems = lint_mermaid("flowchart LR\n  a[x --> b(y\n")
    assert [p.split(":")[0] for p in problems] == ["brackets", "brackets"]


def test_an_odd_number_of_double_quotes_is_a_problem():
    problems = lint_mermaid('flowchart LR\n  a["x] --> b\n')
    assert len(problems) == 1 and problems[0].startswith("quotes:")


def test_a_subgraph_without_end_and_an_end_without_subgraph_are_problems():
    unclosed = lint_mermaid("flowchart LR\n  subgraph A\n    a --> b\n")
    assert len(unclosed) == 1 and unclosed[0].startswith("blocks:")
    stray = lint_mermaid("flowchart LR\n  a --> b\n  end\n")
    assert len(stray) == 1 and stray[0].startswith("blocks:")


@pytest.mark.parametrize("keyword", ["loop", "alt", "opt", "par", "critical", "rect"])
def test_sequence_blocks_must_each_have_an_end(keyword):
    assert lint_mermaid(f"sequenceDiagram\n  {keyword} x\n    A->>B: hi\n  end\n") == []
    problems = lint_mermaid(f"sequenceDiagram\n  {keyword} x\n    A->>B: hi\n")
    assert len(problems) == 1 and problems[0].startswith("blocks:") and keyword in problems[0]


def test_an_end_inside_a_word_or_a_label_is_not_a_block_end():
    assert lint_mermaid('flowchart LR\n  a["the end"] --> b[endpoint]\n') == []


def test_a_semicolon_in_a_sequence_diagram_line_is_a_problem_naming_the_line():
    problems = lint_mermaid("sequenceDiagram\n  A->>B: one; two\n  B-->>A: ok\n")
    assert len(problems) == 1
    assert problems[0].startswith("semicolon:") and "line 2" in problems[0]


def test_a_semicolon_in_a_sequence_diagram_comment_is_fine():
    assert lint_mermaid("sequenceDiagram\n  %% one; two\n  A->>B: ok\n") == []


def test_a_semicolon_in_a_flowchart_is_not_this_lints_business():
    assert lint_mermaid('flowchart LR\n  a["x; y"] --> b\n') == []


def test_every_problem_of_a_block_is_reported_once():
    block = "graph TD\n\ta[x --> b\n  subgraph S\n"
    assert sorted(p.split(":")[0] for p in lint_mermaid(block)) == ["blocks", "brackets", "tab", "type"]


# --- mermaid_blocks ---------------------------------------------------------------------------

def test_mermaid_blocks_extracts_only_mermaid_fences_in_order():
    markdown = ("# Title\n\n```mermaid\nflowchart LR\n  a --> b\n```\n\ntext\n\n```python\nprint(1)\n```\n\n"
                "```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n")
    assert mermaid_blocks(markdown) == ["flowchart LR\n  a --> b\n", "sequenceDiagram\n  A->>B: hi\n"]


def test_mermaid_blocks_of_a_text_without_any_is_empty():
    assert mermaid_blocks("no diagrams\n```sql\nSELECT 1;\n```\n") == []
    assert mermaid_blocks("") == []


def test_mermaid_blocks_reads_windows_line_endings_too():
    (block,) = mermaid_blocks("```mermaid\r\nflowchart LR\r\n  a --> b\r\n```\r\n")
    assert lint_mermaid(block) == []


# --- escape_label -----------------------------------------------------------------------------

@pytest.mark.parametrize("text, escaped", [
    ('say "hi"', "say #quot;hi#quot;"),
    ("T#3", "T#35;3"),
    ("a; b", "a#59; b"),
    ("a < b > c", "a #lt; b #gt; c"),
    ("plain label 12", "plain label 12"),
    ("", ""),
])
def test_escape_label(text, escaped):
    assert escape_label(text) == escaped


def test_escape_label_keeps_a_literal_br_and_escapes_everything_around_it():
    assert escape_label("s1 · Read<br/>T1 Input<br/>T2 \"Select\"") == "s1 · Read<br/>T1 Input<br/>T2 #quot;Select#quot;"
    assert escape_label("<br/>") == "<br/>"
    assert escape_label("<b>x</b><br/>") == "#lt;b#gt;x#lt;/b#gt;<br/>"


def test_escape_label_does_not_escape_its_own_entities_a_second_time():
    assert escape_label('"#;<>') == "#quot;#35;#59;#lt;#gt;"


def test_a_label_that_breaks_the_lint_unescaped_passes_it_escaped():
    label = '5" pipe; # note'
    broken = f'flowchart LR\n  a["{label}"] --> b\n'
    assert lint_mermaid(broken)                                  # the raw quotes are odd-numbered
    fixed = f'flowchart LR\n  a["{escape_label(label)}"] --> b\n'
    assert lint_mermaid(fixed) == []


@pytest.mark.parametrize("text, escaped", [
    ("[REGION] != 1", "#91;REGION#93; != 1"),
    ("Round(x, 2)", "Round#40;x, 2#41;"),
    ("{{src}}.ORDERS", "#123;#123;src#125;#125;.ORDERS"),
    ("a[1](2){3}", "a#91;1#93;#40;2#41;#123;3#125;"),
])
def test_escape_label_writes_every_bracket_as_its_entity(text, escaped):
    assert escape_label(text) == escaped


@pytest.mark.parametrize("label", ["[REGION", "x)", "f(a[0]", "{", "}}", "IIF([QTY]>=10,(1", "a]b)c}d"])
def test_a_label_with_unbalanced_brackets_passes_the_lint_once_escaped(label):
    assert lint_mermaid(f'flowchart LR\n  a["{label}"] --> b\n')       # raw, each one breaks the count
    assert lint_mermaid(f'flowchart LR\n  a["{escape_label(label)}"] --> b\n') == []


def test_escape_label_keeps_br_literal_next_to_brackets():
    assert escape_label("T1 [Input]<br/>T2 (Select)") == "T1 #91;Input#93;<br/>T2 #40;Select#41;"
