import pytest

from tinycode.edits import EditError, apply_edit, diff_stats, unified_diff

SRC = "def f():\n    x = 1\n    if x:\n        return 2\n    return 3\n"


def test_exact():
    out, n, how = apply_edit(SRC, "x = 1", "x = 10")
    assert "x = 10" in out and n == 1 and how == "exact"


def test_ambiguous_exact_needs_context():
    with pytest.raises(EditError, match="2 times"):
        apply_edit("a\na\n", "a", "b")


def test_replace_all():
    out, n, _ = apply_edit("a\na\n", "a", "b", replace_all=True)
    assert out == "b\nb\n" and n == 2


def test_line_numbers_copied_from_read_file():
    old = "     2\t    x = 1\n     3\t    if x:"
    out, _, how = apply_edit(SRC, old, "     2\t    x = 5\n     3\t    if x:")
    assert "    x = 5\n    if x:" in out
    assert how == "line-numbers-removed"


def test_wrong_indentation_is_reindented():
    old = "if x:\n    return 2"          # model lost the outer indent
    new = "if x:\n    return 42"
    out, _, how = apply_edit(SRC, old, new)
    assert "    if x:\n        return 42\n" in out
    assert how == "whitespace-trimmed"


def test_whitespace_normalized():
    out, _, how = apply_edit(SRC, "x  =   1", "x = 7")
    assert "    x = 7" in out


def test_not_found_gives_closest_hint():
    with pytest.raises(EditError) as e:
        apply_edit(SRC, "y = 1\n    if y:", "z")
    assert "most similar region" in str(e.value)


def test_crlf_preserved():
    src = "a = 1\r\nb = 2\r\n"
    out, _, _ = apply_edit(src, "b = 2", "b = 3")
    assert out == "a = 1\r\nb = 3\r\n"


def test_block_anchor():
    src = "def g():\n    a = 1\n    b = 2\n    c = 3\n    return a\n"
    old = "def g():\n    a = 1\n    b = 22\n    c = 3\n    return a"
    out, _, how = apply_edit(src, old, "def g():\n    return 0")
    assert out == "def g():\n    return 0\n" and how == "block-anchor"


def test_identical_rejected():
    with pytest.raises(EditError):
        apply_edit(SRC, "x = 1", "x = 1")


def test_diff_stats():
    d = unified_diff("a\nb\n", "a\nc\nd\n", "f")
    assert diff_stats(d) == (2, 1)
