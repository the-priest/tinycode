from tinycode.config import Config
from tinycode.tools import Tools, is_safe_command


def mk(project):
    return Tools(project, Config())


def test_read_write_edit_undo(project):
    t = mk(project)
    r = t.t_read_file("calc.py")
    assert r.ok and "return a - b" in r.output and "     2\t" in r.output
    r = t.t_edit_file("calc.py", "return a - b", "return a + b")
    assert r.ok and r.diff and "+1 -1" in r.summary
    r = t.t_write_file("new/mod.py", "x = 1\n")
    assert r.ok and (project / "new/mod.py").exists()
    cp = t.take_checkpoint()
    restored = t.restore(cp)
    assert set(restored) == {"calc.py", "new/mod.py"}
    assert "a - b" in (project / "calc.py").read_text()
    assert not (project / "new/mod.py").exists()


def test_read_missing_suggests(project):
    (project / "src").mkdir()
    (project / "src" / "util.py").write_text("")
    r = mk(project).t_read_file("util.py")
    assert not r.ok and "src/util.py" in r.output


def test_glob_grep_list(project):
    t = mk(project)
    (project / "node_modules").mkdir()
    (project / "node_modules" / "junk.py").write_text("add")
    g = t.t_glob("*.py")
    assert "calc.py" in g.output and "junk" not in g.output
    g = t.t_glob("**/*.md")
    assert "README.md" in g.output
    r = t.t_grep("def add", include="*.py")
    assert "calc.py:1:" in r.output
    assert "no matches" in t.t_grep("zzzz_nothing").output
    l = t.t_list_dir()
    assert "calc.py" in l.output and "node_modules" not in l.output


def test_bash(project):
    t = mk(project)
    r = t.t_bash("echo hi && exit 3")
    assert "hi" in r.output and "exit code 3" in r.output and not r.ok
    r = t.t_bash("sleep 5", timeout=1)
    assert "timed out" in r.output


def test_todowrite_lenient(project):
    seen = []
    t = Tools(project, Config(), on_todos=seen.append)
    r = t.t_todowrite([{"content": "a", "status": "done"}, "b",
                       {"task": "c", "status": "in progress"}])
    assert [x["status"] for x in t.todos] == ["completed", "pending", "in_progress"]
    assert seen and "1/3" in r.output


def test_permissions(project):
    t = mk(project)
    assert t.classify("bash", {"command": "ls -la"}, "ask") == (False, "")
    assert t.classify("bash", {"command": "git status && git diff"}, "ask")[0] is False
    assert t.classify("bash", {"command": "pytest"}, "ask")[0] is True
    assert t.classify("bash", {"command": "pytest"}, "yolo")[0] is False
    needs, warn = t.classify("bash", {"command": "rm -rf ~"}, "yolo")
    assert needs and warn
    assert t.classify("edit_file", {"path": "calc.py"}, "auto-edit")[0] is False
    assert t.classify("edit_file", {"path": "/etc/passwd"}, "yolo")[0] is True


def test_safe_command():
    assert is_safe_command("cat a | grep b | wc -l")
    assert not is_safe_command("echo x > file")
    assert not is_safe_command("find . -delete")
    assert not is_safe_command("git push")
    assert not is_safe_command("git branch -D main")
    assert is_safe_command("git log --oneline -5")
    assert not is_safe_command("ls $(rm -rf x)")


def test_preview_fails_fast(project):
    t = mk(project)
    diff, err = t.preview("edit_file", {"path": "calc.py", "old_string": "nope",
                                        "new_string": "x"})
    assert err and not diff
    diff, err = t.preview("edit_file", {"path": "calc.py", "old_string": "a - b",
                                        "new_string": "a + b"})
    assert err is None and "+    return a + b" in diff
