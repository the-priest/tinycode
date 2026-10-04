import shutil
from pathlib import Path

import pytest

from tinycode.webcheck import collect, pages_for, run_page, static_findings

APPS = Path(__file__).parent / "apps"
need_node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def copy_app(tmp_path, name):
    dst = tmp_path / name
    shutil.copytree(APPS / name, dst)
    return dst


@need_node
def test_working_calculator_passes_all_probes(tmp_path):
    r = run_page(copy_app(tmp_path, "calc_ok") / "index.html")
    assert r.ran and r.ok, r.errors
    assert len(r.probes) == 5 and all(p["ok"] for p in r.probes)
    assert r.clicks >= 15


@need_node
def test_string_concatenation_bug_is_caught(tmp_path):
    d = copy_app(tmp_path, "calc_ok")
    js = (d / "script.js").read_text().replace(
        "case '+': result = prev + curr; break;",
        "case '+': result = previousOperand + currentOperand; break;")
    (d / "script.js").write_text(js)
    r = run_page(d / "index.html")
    assert any('shows "23"' in e and "expected 5" in e for e in r.errors), r.errors


@need_node
def test_wrong_id_is_caught_with_root_cause(tmp_path):
    d = copy_app(tmp_path, "calc_ok")
    js = (d / "script.js").read_text().replace("getElementById('display')",
                                              "getElementById('calc-display')")
    (d / "script.js").write_text(js)
    r = run_page(d / "index.html")
    text = "\n".join(r.errors)
    assert 'looks up id "calc-display"' in text
    assert "TypeError" in text and "script.js line 7" in text
    assert "getElementById('calc-display') returned null" in text
    assert "WrongResult" not in text          # consequences of the crash are not repeated


@need_node
def test_undefined_inline_handler(tmp_path):
    d = copy_app(tmp_path, "calc_ok")
    html = (d / "index.html").read_text().replace('onclick="calculate()"',
                                                  'onclick="computeResult()"')
    (d / "index.html").write_text(html)
    r = run_page(d / "index.html")
    assert any("computeResult is not defined" in e and "index.html line" in e for e in r.errors)


@need_node
def test_canvas_game_with_loop_and_keys_has_no_false_alarms(tmp_path):
    r = run_page(copy_app(tmp_path, "game") / "index.html")
    assert r.ran and r.ok, r.errors


@need_node
def test_todo_probe_and_crash(tmp_path):
    d = copy_app(tmp_path, "todo_ok")
    ok = run_page(d / "index.html")
    assert ok.ok and ok.probes and ok.probes[0]["ok"]
    html = (d / "index.html").read_text().replace("todos.push({ text: v });",
                                                  "todos.push({ txt: v });").replace(
        "${t.text}", "${t.text.toUpperCase()}")
    (d / "index.html").write_text(html)
    bad = run_page(d / "index.html")
    assert len(bad.errors) == 1 and "toUpperCase" in bad.errors[0]


@need_node
def test_model_written_scenario(tmp_path):
    d = copy_app(tmp_path, "calc_ok")
    steps = [{"click": "7"}, {"click": "×"}, {"click": "6"}, {"click": "="},
             {"expect": ["#display", "42"]}, {"click": "C"}, {"expect": ["#display", "1"]}]
    r = run_page(d / "index.html", steps=steps)
    assert [e["ok"] for e in r.expects] == [True, False]
    assert 'shows "0"' in r.expects[1]["message"]


@need_node
def test_browser_apis_and_libraries_dont_cause_false_alarms(tmp_path):
    html = """<!DOCTYPE html><html><head>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head><body>
<canvas id="c"></canvas><button id="b">Go</button><div id="out"></div>
<script>
const chart = new Chart(document.getElementById('c'), {type: 'bar', data: {}});
const obs = new IntersectionObserver(() => {}); obs.observe(document.body);
document.getElementById('b').addEventListener('click', async () => {
  const r = await fetch('/api'); const j = await r.json();
  document.getElementById('out').textContent = 'loaded';
  document.querySelector('#out').scrollIntoView({behavior: 'smooth'});
  navigator.clipboard.writeText('x'); localStorage.setItem('k', '1');
  const a = new Audio('x.mp3'); a.play();
  requestAnimationFrame(() => { document.body.classList.add('ready'); });
});
</script></body></html>"""
    p = tmp_path / "index.html"
    p.write_text(html)
    r = run_page(p)
    assert r.ran and r.ok, r.errors


def test_static_missing_files(tmp_path):
    (tmp_path / "index.html").write_text(
        '<html><head><link rel="stylesheet" href="style.css"></head><body>'
        '<script src="app.js"></script></body></html>')
    page = collect(tmp_path / "index.html")
    found = static_findings(page)
    assert len(found) == 2 and all("doesn't exist" in f for f in found)


def test_templates_are_not_run(tmp_path):
    (tmp_path / "t.html").write_text("<script>const x = {{ data|tojson }};</script>")
    r = run_page(tmp_path / "t.html")
    assert not r.errors and "template" in r.skipped


def test_pages_for_finds_page_that_loads_changed_script(tmp_path):
    d = copy_app(tmp_path, "calc_ok")
    assert pages_for([d / "script.js"], tmp_path) == [d / "index.html"]
