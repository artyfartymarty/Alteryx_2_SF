"""Reading a build file (`build.sql`, or `build.py`'s comments) as text: statements, placeholders and
step regions (objective-first design §4.4).

A workflow's deliverable is one file, not a stored procedure. Every script that has to look inside it --
`compile_check.py` (the named rules), `validate_build.py` (running it statement by statement),
`render_mapping.py` (step markers to line ranges), `deploy.py` (rendering the placeholders) -- reads it
through this module, so they all agree on where a statement ends and where a step's code begins.

* Two placeholders, `{{src}}` and `{{tgt}}` (spaces inside the braces allowed), stand for the source and
  target schemas. Snowflake's own templating renders them when the file is run with
  `EXECUTE IMMEDIATE FROM @<stage>/build.sql USING (src => …, tgt => …)`; `render` does the same job
  locally. That form has not run on a real Snowflake account; neither has anything else in this repo.
* A step marker is a comment line, `-- step s1` or `-- step s1, s3` in SQL and `# step s1` in Python. The
  region it starts runs to the line before the next marker (or the end of the file), trimmed to its first
  and last code lines. `step_markers` lists every comment that is, or only tries to be, a marker, so a mistyped
  one (`-- Step s1: read`) can be named rather than read as "this step has no region".
* `split_statements` is aware of `'...'` strings, `"..."` identifiers, `--` and `/* */` comments and
  `$$...$$` bodies, so a `;` or a marker-looking comment inside any of them is not a boundary.

The scanner (`next_span`, `spans`, `code_text`, `without_comments`, `split_code`, `unquote`,
`masked_code`) does not know `$$`: it is only `split_statements` and `step_regions` that do, because a build
file may carry `$$...$$` as a string literal.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import Callable, Iterator

#: The only placeholder names a build file may use.
PLACEHOLDER_NAMES = ("src", "tgt")


@dataclass(frozen=True)
class Statement:
    text: str        # the statement without its terminating ';' (leading comments kept)
    start_line: int  # 1-based line of its first code character (not a comment or blank)
    end_line: int    # 1-based line holding its terminating ';' (or its last code line)


@dataclass(frozen=True)
class Region:
    steps: tuple[str, ...]   # e.g. ("s1", "s3")
    marker_line: int         # 1-based line of the marker comment
    start_line: int          # first code line after the marker
    end_line: int            # last code line before the next marker or EOF


# --- a scanner that knows where strings and comments are --------------------------------------

def next_span(text: str, index: int) -> tuple[str, int]:
    """(kind, end) of the span starting at `index`: "code", "string" or "comment"."""
    if text.startswith("--", index):
        end = text.find("\n", index)
        return "comment", len(text) if end < 0 else end
    if text.startswith("/*", index):
        end = text.find("*/", index + 2)
        return "comment", len(text) if end < 0 else end + 2
    char = text[index]
    if char in "'\"":
        cursor = index + 1
        while cursor < len(text):
            if char == "'" and text[cursor] == "\\":      # Snowflake's backslash escapes
                cursor += 2
                continue
            if text[cursor] == char:
                if text.startswith(char * 2, cursor):     # '' and "" are escaped quotes
                    cursor += 2
                    continue
                return "string", cursor + 1
            cursor += 1
        return "string", len(text)
    cursor = index
    while cursor < len(text):
        if text[cursor] in "'\"" or text.startswith("--", cursor) or text.startswith("/*", cursor):
            break
        cursor += 1
    return "code", cursor


def spans(text: str) -> Iterator[tuple[str, int, int]]:
    index = 0
    while index < len(text):
        kind, end = next_span(text, index)
        yield kind, index, end
        index = end


def code_text(text: str) -> str:
    """The statement with strings and comments blanked out, for keyword detection only."""
    return "".join(text[start:end] if kind == "code" else " " for kind, start, end in spans(text))


def without_comments(text: str) -> str:
    """The statement with its comments blanked out and its string literals kept."""
    return "".join(text[start:end] if kind != "comment" else " " for kind, start, end in spans(text))


def split_code(text: str, separator: str) -> list[str]:
    """Splits on `separator` where it appears outside strings and comments."""
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(text):
        kind, end = next_span(text, index)
        chunk = text[index:end]
        if kind == "code" and separator in chunk:
            pieces = chunk.split(separator)
            current.append(pieces[0])
            for piece in pieces[1:]:
                parts.append("".join(current))
                current = [piece]
        else:
            current.append(chunk)
        index = end
    parts.append("".join(current))
    return parts


def unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'").replace("\\'", "'")
    return text


def masked_code(text: str, *, strings: bool = True) -> str:
    """`text` with every comment -- and, unless `strings=False`, every single-quoted string
    literal -- replaced by spaces, character for character, so an offset in the result is the same
    offset in `text`. A `"quoted identifier"` is kept: it is a name, not text (Task L4's
    `c4:write_mode` reads column names out of a MERGE's ON clause)."""
    out: list[str] = []
    for kind, start, end in spans(text):
        chunk = text[start:end]
        blank = kind == "comment" or (strings and kind == "string" and chunk.startswith("'"))
        out.append(re.sub(r"[^\n]", " ", chunk) if blank else chunk)
    return "".join(out)


# --- the scanner a build file needs: the one above plus $$ bodies, or Python's # comments -----

def _sql_span(text: str, index: int) -> tuple[str, int]:
    """`next_span`, except that a `$$...$$` body is one string span (an unclosed one runs to the end),
    so a `;`, a quote or a comment marker inside it means nothing."""
    if text.startswith("$$", index):
        close = text.find("$$", index + 2)
        return "string", len(text) if close < 0 else close + 2
    kind, end = next_span(text, index)
    if kind == "code":
        cut = text.find("$$", index, end)
        if cut > index:
            end = cut
    return kind, end


def _python_span(text: str, index: int) -> tuple[str, int]:
    """The same three kinds for Python source: `#` comments, `'`/`"` strings (triple-quoted too, a
    backslash escaping whatever follows it), everything else code."""
    char = text[index]
    if char == "#":
        end = text.find("\n", index)
        return "comment", len(text) if end < 0 else end
    if char in "'\"":
        quote = char * 3 if text.startswith(char * 3, index) else char
        cursor = index + len(quote)
        while cursor < len(text):
            if text[cursor] == "\\":
                cursor += 2
                continue
            if text.startswith(quote, cursor):
                return "string", cursor + len(quote)
            cursor += 1
        return "string", len(text)
    cursor = index
    while cursor < len(text) and text[cursor] not in "#'\"":
        cursor += 1
    return "code", cursor


def _build_spans(text: str, comment: str) -> Iterator[tuple[str, int, int]]:
    """(kind, start, end) of every span of a build file, `comment` being `--` (SQL) or `#` (Python)."""
    if comment not in ("--", "#"):
        raise ValueError(f"comment style {comment!r} is not one a build file uses; expected '--' (build.sql) "
                         f"or '#' (build.py)")
    return _walk(text, _sql_span if comment == "--" else _python_span)


def _walk(text: str, scan: Callable[[str, int], tuple[str, int]]) -> Iterator[tuple[str, int, int]]:
    index = 0
    while index < len(text):
        kind, end = scan(text, index)
        yield kind, index, end
        index = end


def _line_finder(text: str) -> Callable[[int], int]:
    """A function from a 0-based offset to its 1-based line, counting `\\n` only (so CRLF text is fine)."""
    newlines = [offset for offset, char in enumerate(text) if char == "\n"]
    return lambda offset: bisect.bisect_left(newlines, offset) + 1


# --- placeholders -----------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{([^{}\n]*)\}\}")


def placeholders(text: str) -> list[tuple[str, int]]:
    """Every `{{ name }}` in `text` as (name, 1-based line), in order, unknown names included. Comments
    and strings count: Snowflake's templating renders the whole file, not just its code."""
    line_at = _line_finder(text)
    return [(match.group(1).strip(), line_at(match.start())) for match in _PLACEHOLDER.finditer(text)]


def render(text: str, values: dict[str, str]) -> str:
    """`text` with each `{{ name }}` replaced by `values[name]`. A name that is not one of
    `PLACEHOLDER_NAMES`, a known name with no value, or a `{{` that never closes is a `ValueError`
    naming it and its line, so a rendered file never holds a `{{`."""
    for name, line in placeholders(text):
        if name not in PLACEHOLDER_NAMES:
            raise ValueError(f"unknown placeholder {{{{ {name} }}}} on line {line}; expected one of "
                             f"{', '.join('{{' + known + '}}' for known in PLACEHOLDER_NAMES)}")
        if name not in values:
            raise ValueError(f"placeholder {{{{ {name} }}}} on line {line} has no value; expected a value "
                             f"for each of {', '.join(PLACEHOLDER_NAMES)}")
    blanked = _PLACEHOLDER.sub(lambda match: " " * len(match.group(0)), text)
    stray = blanked.find("{{")
    if stray >= 0:
        raise ValueError(f"'{{{{' on line {_line_finder(text)(stray)} is never closed by '}}}}'; expected "
                         f"{{{{src}}}} or {{{{tgt}}}}")
    return _PLACEHOLDER.sub(lambda match: str(values[match.group(1).strip()]), text)


# --- statements -------------------------------------------------------------------------------

def split_statements(text: str) -> list[Statement]:
    """The statements of `text`, split on `;` outside strings, identifiers, comments and `$$` bodies.

    `Statement.text` is everything from the end of the previous statement to the one before the `;`,
    stripped: leading comments stay in it. A chunk with no code (a trailing comment, `;;`) is not a
    statement. A last statement with no `;` is returned and ends on its last code line.
    """
    line_at = _line_finder(text)
    statements: list[Statement] = []
    chunk_start = 0
    first: int | None = None      # offset of the chunk's first code character
    last = 0                      # offset of its last code character so far
    for kind, start, end in _build_spans(text, "--"):
        if kind == "comment":
            continue
        if kind == "string":
            first = start if first is None else first
            last = end - 1
            continue
        cursor = start
        while cursor < end:
            semicolon = text.find(";", cursor, end)
            stop = end if semicolon < 0 else semicolon
            piece = text[cursor:stop]
            if piece.strip():
                if first is None:
                    first = cursor + len(piece) - len(piece.lstrip())
                last = cursor + len(piece.rstrip()) - 1
            if semicolon < 0:
                break
            if first is not None:
                statements.append(Statement(text=text[chunk_start:semicolon].strip(),
                                            start_line=line_at(first), end_line=line_at(semicolon)))
            chunk_start, first = semicolon + 1, None
            cursor = semicolon + 1
    if first is not None:
        statements.append(Statement(text=text[chunk_start:last + 1].strip(),
                                    start_line=line_at(first), end_line=line_at(last)))
    return statements


# --- step markers and regions -----------------------------------------------------------------

_STEP_LIST = r"s[1-9][0-9]*(?:\s*,\s*s[1-9][0-9]*)*"


def step_regions(text: str, comment: str = "--") -> list[Region]:
    """The regions the step markers of `text` start (`comment="#"` for `build.py`).

    A marker is a comment on a line of its own, `<comment> step s1` or `<comment> step s1, s3`, nothing
    else on the line; anything that is not exactly that (`-- Step s1`, `-- step s1: read`) is an ordinary
    comment. A region runs from the first code line after its marker to the last code line before the
    next marker or the end of the file, so blank and comment-only lines at either end are left out. A
    marker with no code before the next one has no region and is not returned: the caller's check for
    "every step has a region" reports it. Code before the first marker is in no region.
    """
    marker = re.compile(re.escape(comment) + r"\s*step\s+(" + _STEP_LIST + r")\s*")
    line_at = _line_finder(text)
    markers: list[tuple[int, tuple[str, ...]]] = []
    code_lines: set[int] = set()
    for kind, start, end in _build_spans(text, comment):
        chunk = text[start:end]
        first_line = line_at(start)
        if kind == "comment":
            line_start = text.rfind("\n", 0, start) + 1
            found = marker.fullmatch(chunk) if not text[line_start:start].strip() else None
            if found is not None:
                steps = tuple(dict.fromkeys(part.strip() for part in found.group(1).split(",")))
                markers.append((first_line, steps))
        elif kind == "string":
            code_lines.update(range(first_line, first_line + chunk.count("\n") + 1))
        else:
            code_lines.update(first_line + offset for offset, piece in enumerate(chunk.split("\n"))
                              if piece.strip())
    ordered = sorted(code_lines)
    last_line = text.count("\n") + 1
    regions: list[Region] = []
    for position, (marker_line, steps) in enumerate(markers):
        limit = markers[position + 1][0] - 1 if position + 1 < len(markers) else last_line
        low = bisect.bisect_right(ordered, marker_line)
        high = bisect.bisect_right(ordered, limit)
        if low < high:
            regions.append(Region(steps=steps, marker_line=marker_line,
                                  start_line=ordered[low], end_line=ordered[high - 1]))
    return regions


@dataclass(frozen=True)
class Marker:
    line: int                  # 1-based line of the comment
    steps: tuple[str, ...]     # the step ids it names; () when it starts like a marker but is not one
    text: str                  # the comment as written, comment characters included


def step_markers(text: str, comment: str = "--") -> list[Marker]:
    """Every comment of `text` that is, or tries to be, a step marker, in order (`comment="#"` for
    `build.py`). One that parses (exactly what `step_regions` accepts) has its step ids; one that only
    *starts like* a marker has `steps == ()`: its text begins with `step` in any case and names something
    that looks like a step id (`-- Step s1: clean the orders`, `-- step s1 s2`, `-- step s01`, or a real
    marker after code on the same line). A comment such as `-- step by step, keep the rows` names no step
    id and is ordinary. `compile_check.py` reports the second kind as `steps:marker` and the ids of the
    first as `steps:unknown` when the objective has no such step, so a mistyped marker is named rather than
    silently reading as 'this step has no region'."""
    strict = re.compile(re.escape(comment) + r"\s*step\s+(" + _STEP_LIST + r")\s*")
    loose = re.compile(re.escape(comment) + r"\s*step\b", re.IGNORECASE)
    looks_like_id = re.compile(r"\bs\d+\b", re.IGNORECASE)
    line_at = _line_finder(text)
    markers: list[Marker] = []
    for kind, start, end in _build_spans(text, comment):
        if kind != "comment":
            continue
        chunk = text[start:end]
        if not loose.match(chunk) or not looks_like_id.search(chunk):
            continue
        own_line = not text[text.rfind("\n", 0, start) + 1:start].strip()
        found = strict.fullmatch(chunk) if own_line else None
        steps = tuple(dict.fromkeys(part.strip() for part in found.group(1).split(","))) if found else ()
        markers.append(Marker(line=line_at(start), steps=steps, text=chunk.strip()))
    return markers


# --- session settings -------------------------------------------------------------------------

_ALTER_SESSION_SET = re.compile(r"ALTER\s+SESSION\s+SET\s+(.+)", re.IGNORECASE | re.DOTALL)
_SETTING_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def session_settings(statement: Statement) -> dict[str, str] | None:
    """`ALTER SESSION SET a = 'x', b = 1` -> {"A": "x", "B": "1"} (names upper-cased, quotes removed);
    None when the statement is not that form. Leading comments are ignored. Whether a parameter is on
    the allow-list is `compile_check.py`'s question, not this one."""
    match = _ALTER_SESSION_SET.fullmatch(without_comments(statement.text).strip())
    if match is None:
        return None
    settings: dict[str, str] = {}
    for assignment in split_code(match.group(1), ","):
        name, separator, value = assignment.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not _SETTING_NAME.fullmatch(name) or not value:
            return None
        settings[name.upper()] = unquote(value)
    return settings
