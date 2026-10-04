import shutil

import pytest

from tinycode.checks import check_file, for_model, summary

need_node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
need_gcc = pytest.mark.skipif(not shutil.which("gcc"), reason="gcc not installed")


def chk(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return check_file(p, text)


def test_python_syntax_error_and_ok(tmp_path):
    r = chk(tmp_path, "a.py", "def f(x):\n    return x +\n")
    assert r.errors and r.errors[0].line == 2 and "SyntaxError" in r.errors[0].message
    assert chk(tmp_path, "b.py", "def f(x):\n    return x\n").ok


def test_python_unfinished_is_not_an_error(tmp_path):
    r = chk(tmp_path, "a.py", "def f(x:\n")
    assert not r.errors and r.incomplete


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_python_undefined_name(tmp_path):
    r = chk(tmp_path, "a.py", "def f(x):\n    return y\n")
    assert r.errors and "y" in r.errors[0].message


@need_node
def test_js(tmp_path):
    r = chk(tmp_path, "a.js", "function a() {\n  let x = ;\n}\n")
    assert r.errors[0].line == 2
    assert chk(tmp_path, "b.js", "export const x = 1;\n").ok
    inc = chk(tmp_path, "c.js", "function a() {\n  return 1;\n")
    assert not inc.errors and inc.incomplete


@need_node
def test_html_inline_script_error_has_file_line_numbers(tmp_path):
    html = ("<!DOCTYPE html>\n<html>\n<body>\n<canvas></canvas>\n<script>\n"
            "const b = [];\nfunction drop() {\n  if (b.length > 0 {\n    return;\n  }\n}\n"
            "</script>\n</body>\n</html>\n")
    r = chk(tmp_path, "g.html", html)
    assert len(r.errors) == 1 and r.errors[0].line == 8
    assert "inline <script>" in r.errors[0].where


def test_html_valid_with_optional_tags_and_svg(tmp_path):
    html = ("<!DOCTYPE html><html><head><meta charset=utf-8><title>x</title>\n"
            "<body><p>a<br>b<p>c<ul><li>1<li>2</ul><table><tr><td>1<td>2</table>\n"
            "<svg><path d='M0'/><circle r='1'></circle></svg><img src=x>\n"
            "<script src=a.js></script><script type=application/ld+json>{\"a\":1}</script>\n"
            "<script>const s = '</div>'; if (1 < 2) {}</script></body></html>\n")
    r = chk(tmp_path, "ok.html", html)
    assert not r.errors and not r.incomplete, r.problems


def test_html_mismatch_and_stray(tmp_path):
    r = chk(tmp_path, "m.html", "<html><body>\n<div>\n<span>x</div>\n</section>\n</body></html>\n")
    msgs = " ".join(p.message for p in r.errors)
    assert "<span>" in msgs and "</section>" in msgs


def test_html_unfinished_chunk(tmp_path):
    r = chk(tmp_path, "u.html", "<html><body>\n<div>\n<script>\nlet a = 1;\n")
    assert not r.errors and r.incomplete


def test_json_toml_css(tmp_path):
    assert chk(tmp_path, "a.json", '{"a": 1,}\n').errors
    assert chk(tmp_path, "b.json", '{"a": [1,\n').incomplete
    assert chk(tmp_path, "c.json", '{"a": [1]}\n').ok
    assert chk(tmp_path, "a.toml", "a = [1\n").errors
    assert chk(tmp_path, "a.css", "a { color: red; }\n}\n").errors
    assert chk(tmp_path, "b.css", "a { color: red; }\n").ok


def test_shell(tmp_path):
    r = chk(tmp_path, "a.sh", "echo $(( 1 + ))\nfi\n")
    assert len(r.errors) == 1
    assert chk(tmp_path, "b.sh", "if true; then\n echo hi\n").incomplete


@need_gcc
def test_c(tmp_path):
    r = chk(tmp_path, "a.c", '#include <stdio.h>\nint main(){ printf("x") return 0; }\n')
    assert r.errors and r.errors[0].line == 2


def test_unknown_type_is_skipped(tmp_path):
    r = chk(tmp_path, "notes.txt", "hello")
    assert not r.checked and summary(r) == ""


def test_for_model_text(tmp_path):
    text = "def f(x):\n    return x +\n"
    r = chk(tmp_path, "a.py", text)
    msg = for_model(r, text, "a.py")
    assert "line 2" in msg and "return x +" in msg and "Fix them now" in msg
    good = chk(tmp_path, "b.py", "x = 1\n")
    assert "no problems" in for_model(good, "x = 1\n", "b.py")
