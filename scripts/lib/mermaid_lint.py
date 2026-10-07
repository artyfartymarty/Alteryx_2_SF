"""The structural lint for Mermaid diagrams, shared by the README's diagrams (`tests/test_readme_diagrams.py`)
and the generated `docs/mapping.md` (`scripts/render_mapping.py`, objective-first design §4.9).

It catches what has actually broken a diagram on GitHub -- a tab, a bracket or quote left open, a
`subgraph`/`loop` without its `end`, a `;` in a sequence-diagram line (Mermaid reads it as a statement
separator, found by the Mermaid 11 render check on 2026-09-24) -- by counting, not by parsing. A clean
result is therefore not proof the diagram renders: rendering is checked by a person in a browser with
Mermaid 11, and that has not been automated here.

`escape_label` is for text that goes inside a node's `["..."]` label: it writes the characters Mermaid
would otherwise read as syntax -- or that this lint counts, the six bracket characters -- as its `#name;` /
`#number;` entity codes.
"""
from __future__ import annotations

import re

_BLOCK = re.compile(r"```mermaid[ \t]*\r?\n(.*?)```", re.DOTALL)
_TYPES = ("flowchart ", "sequenceDiagram", "stateDiagram-v2")
_OPENER = re.compile(r"\s*(subgraph|loop|alt|opt|par|critical|rect)\b")
_BRACKETS = (("[", "]", "square"), ("(", ")", "round"), ("{", "}", "curly"))
#: Brackets are escaped too: `lint_mermaid` counts them over the whole block, so a label with an unbalanced
#: `[`, `(` or `{` (an Alteryx expression such as `IIF([QTY]>=10,(1`, cut at a column limit) would fail it.
_ESCAPES = str.maketrans({'"': "#quot;", "#": "#35;", ";": "#59;", "<": "#lt;", ">": "#gt;",
                          "[": "#91;", "]": "#93;", "(": "#40;", ")": "#41;", "{": "#123;", "}": "#125;"})
_BREAK = "<br/>"


def mermaid_blocks(markdown: str) -> list[str]:
    """The text of every ```` ```mermaid ```` fence in `markdown`, in order (without the fence lines)."""
    return _BLOCK.findall(markdown)


def lint_mermaid(block: str) -> list[str]:
    """One message per problem in the diagram text `block`, empty when it is clean. Each is
    `<rule>: <what is wrong>; expected <what>`, with the block's own line numbers (line 1 is the line
    that names the diagram type)."""
    problems: list[str] = []
    lines = block.split("\n")

    if not block.lstrip().startswith(_TYPES):
        found = block.lstrip()[:30].split("\n")[0]
        problems.append(f"type: the diagram starts with {found!r}; expected one of "
                        f"{', '.join(kind.strip() for kind in _TYPES)}")
    for number, line in enumerate(lines, 1):
        if "\t" in line:
            problems.append(f"tab: line {number} holds a tab character; expected spaces only")
            break
    for opening, closing, name in _BRACKETS:
        opened, closed = block.count(opening), block.count(closing)
        if opened != closed:
            problems.append(f"brackets: {opened} {name} '{opening}' against {closed} '{closing}'; "
                            f"expected the same number of each")
    if block.count('"') % 2:
        problems.append(f"quotes: {block.count(chr(34))} double quotes; expected an even number "
                        f"(write a quote inside a label as #quot;)")

    openers = [(match.group(1), number) for number, line in enumerate(lines, 1)
               if (match := _OPENER.match(line))]
    ends = sum(1 for line in lines if line.strip() == "end")
    if ends != len(openers):
        named = ", ".join(f"{kind} on line {number}" for kind, number in openers) or "none"
        problems.append(f"blocks: {len(openers)} subgraph/loop/alt/opt/par/critical/rect opener(s) ({named}) "
                        f"against {ends} 'end' line(s); expected one 'end' for each opener")

    if block.lstrip().startswith("sequenceDiagram"):
        for number, line in enumerate(lines, 1):
            if ";" in line and not line.strip().startswith("%%"):
                problems.append(f"semicolon: line {number} holds ';', which Mermaid reads as a statement "
                                f"separator in a sequence diagram; expected none (write it as #59;)")
    return problems


def escape_label(text: str) -> str:
    """`text` made safe inside a node label: `"` becomes `#quot;`, `#` `#35;`, `;` `#59;`, `<` and `>`
    `#lt;` and `#gt;`, `[` `]` `(` `)` `{` `}` `#91;` `#93;` `#40;` `#41;` `#123;` `#125;` -- except a
    literal `<br/>`, the line break Mermaid labels use, which is kept."""
    return _BREAK.join(part.translate(_ESCAPES) for part in text.split(_BREAK))
