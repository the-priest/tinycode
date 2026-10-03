"""Forgiving string replacement + diffs.

Small models rarely reproduce `old_string` byte-for-byte. Instead of failing,
we try a ladder of increasingly lenient (but always *unique*) matchers:

  1. exact match
  2. exact match after stripping line-number prefixes copied from read_file
  3. line-by-line match ignoring leading/trailing whitespace
     (new_string is re-indented to fit)
  4. match with all runs of whitespace collapsed
  5. block anchor: first+last line match and the middle is >= 80% similar

Every strategy must find exactly one location, otherwise we report an error
with the closest region of the file so the model can correct itself.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Optional

_LINENO = re.compile(r"^\s*\d+\t")


class EditError(ValueError):
    pass


@dataclass
class Match:
    start: int
    end: int
    strategy: str
    new_text: Optional[str] = None  # replacement adjusted for indentation


def _strip_linenos(s: str) -> str:
    lines = s.split("\n")
    nonempty = [l for l in lines if l.strip()]
    if nonempty and all(_LINENO.match(l) for l in nonempty):
        return "\n".join(_LINENO.sub("", l, count=1) for l in lines)
    return s


def _line_offsets(text: str) -> list[int]:
    offs = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(new: str, old_first: str, file_first: str) -> str:
    """Shift new's indentation by the difference between what the model wrote
    and what the file really has."""
    have, want = _indent(old_first), _indent(file_first)
    if have == want:
        return new
    out = []
    for line in new.split("\n"):
        if not line.strip():
            out.append(line)
        elif line.startswith(have):
            out.append(want + line[len(have):])
        else:
            out.append(want + line.lstrip())
    return "\n".join(out)


def _find_exact(text: str, old: str) -> list[int]:
    hits, i = [], text.find(old)
    while i != -1:
        hits.append(i)
        if len(hits) > 50:
            break
        i = text.find(old, i + max(1, len(old)))
    return hits


def _line_window_matches(text: str, old: str, key) -> list[tuple[int, int, int]]:
    """Find windows of file lines whose key() equals old lines' key().
    Returns (start_char, end_char, first_line_index)."""
    flines = text.split("\n")
    olines = old.split("\n")
    while olines and not olines[-1].strip():
        olines.pop()
    while olines and not olines[0].strip():
        olines.pop(0)
    if not olines:
        return []
    okeys = [key(l) for l in olines]
    n = len(olines)
    offs = _line_offsets(text)
    out = []
    for i in range(len(flines) - n + 1):
        if key(flines[i]) != okeys[0]:
            continue
        if all(key(flines[i + j]) == okeys[j] for j in range(1, n)):
            start = offs[i]
            end = offs[i + n - 1] + len(flines[i + n - 1])
            out.append((start, end, i))
    return out


def find_match(text: str, old: str) -> Match:
    if not old:
        raise EditError("old_string is empty. Use write_file to create a file.")

    hits = _find_exact(text, old)
    if len(hits) == 1:
        return Match(hits[0], hits[0] + len(old), "exact")
    if len(hits) > 1:
        raise EditError(
            f"old_string appears {len(hits)} times. Include more surrounding "
            "lines so it is unique, or set replace_all=true.")

    stripped = _strip_linenos(old)
    if stripped != old:
        hits = _find_exact(text, stripped)
        if len(hits) == 1:
            return Match(hits[0], hits[0] + len(stripped), "line-numbers-removed")
        old = stripped

    flines = text.split("\n")
    olines = [l for l in old.split("\n")]
    first_old = next((l for l in olines if l.strip()), "")

    for name, key in (("whitespace-trimmed", lambda l: l.strip()),
                      ("whitespace-normalized",
                       lambda l: re.sub(r"\s+", " ", l.strip()))):
        wins = _line_window_matches(text, old, key)
        if len(wins) == 1:
            s, e, i = wins[0]
            return Match(s, e, name, new_text=None if first_old == flines[i]
                         else _reindent_marker(first_old, flines[i]))
        if len(wins) > 1:
            raise EditError(
                f"old_string matches {len(wins)} places (ignoring whitespace). "
                "Include more surrounding lines so it is unique.")

    # block anchor
    core = [l for l in olines]
    while core and not core[-1].strip():
        core.pop()
    while core and not core[0].strip():
        core.pop(0)
    if len(core) >= 3:
        a, b = core[0].strip(), core[-1].strip()
        n = len(core)
        offs = _line_offsets(text)
        cands = []
        for i in range(len(flines)):
            if flines[i].strip() != a:
                continue
            for j in range(i + 1, min(len(flines), i + n + max(3, n // 2))):
                if flines[j].strip() == b:
                    mid_f = "\n".join(x.strip() for x in flines[i:j + 1])
                    mid_o = "\n".join(x.strip() for x in core)
                    ratio = difflib.SequenceMatcher(None, mid_f, mid_o).ratio()
                    if ratio >= 0.8:
                        cands.append((ratio, i, j))
                    break
        if len(cands) == 1:
            _, i, j = cands[0]
            return Match(offs[i], offs[j] + len(flines[j]), "block-anchor",
                         new_text=None if first_old == flines[i]
                         else _reindent_marker(first_old, flines[i]))

    raise EditError("old_string not found in the file." + closest_hint(text, old))


def _reindent_marker(old_first: str, file_first: str) -> str:
    # stored as "old\0file" so apply() can reindent new_string lazily
    return old_first + "\0" + file_first


def closest_hint(text: str, old: str, context: int = 2) -> str:
    """Show the region of the file most similar to old_string."""
    flines = text.split("\n")
    olines = [l for l in old.split("\n") if l.strip()]
    if not olines or not flines:
        return ""
    probe = olines[0].strip()
    best, best_i = 0.0, -1
    for i, l in enumerate(flines):
        if not l.strip():
            continue
        r = difflib.SequenceMatcher(None, l.strip(), probe).ratio()
        if r > best:
            best, best_i = r, i
    if best < 0.5:
        return " Re-read the file with read_file and copy the text exactly."
    lo = max(0, best_i - context)
    hi = min(len(flines), best_i + len(olines) + context)
    snippet = "\n".join(f"{n + 1:>6}\t{flines[n]}" for n in range(lo, hi))
    return ("\nThe most similar region of the file is:\n" + snippet +
            "\nCopy the text exactly from there (without the line numbers).")


def apply_edit(text: str, old: str, new: str, replace_all: bool = False
               ) -> tuple[str, int, str]:
    """Return (new_text, replacements, strategy)."""
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
        old = old.replace("\r\n", "\n")
        new = new.replace("\r\n", "\n")
    if old == new:
        raise EditError("old_string and new_string are identical; nothing to change.")

    if replace_all:
        count = text.count(old)
        if count == 0:
            stripped = _strip_linenos(old)
            count = text.count(stripped)
            if count == 0:
                raise EditError("old_string not found." + closest_hint(text, old))
            old = stripped
        result, n, strategy = text.replace(old, new), count, "exact"
    else:
        m = find_match(text, old)
        repl = _strip_linenos(new) if m.strategy != "exact" else new
        if m.new_text:
            old_first, file_first = m.new_text.split("\0", 1)
            repl = _reindent(repl, old_first, file_first)
        result, n, strategy = text[:m.start] + repl + text[m.end:], 1, m.strategy

    if crlf:
        result = result.replace("\n", "\r\n")
    return result, n, strategy


def unified_diff(before: str, after: str, path: str, context: int = 3) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=context))


def diff_stats(diff: str) -> tuple[int, int]:
    add = rem = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            add += 1
        elif line.startswith("-") and not line.startswith("---"):
            rem += 1
    return add, rem
