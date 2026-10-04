import sys

from fake_ollama import FakeOllama
from tinycode.cli import build_config, main, parse_args, run_headless


def test_headless_print(project, capsys):
    replies = [{"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
               {"content": "A tiny calculator."}]
    with FakeOllama(replies) as fake:
        cfg = build_config(parse_args(["--model", fake.model]))
        cfg.host = fake.host
        rc = run_headless(cfg, project, "what is this?", cont=False)
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "A tiny calculator."
    assert "read_file" in out.err


def test_headless_pulls_missing_model(project, capsys):
    with FakeOllama([{"content": "hi"}], installed=False) as fake:
        cfg = build_config(parse_args(["--model", fake.model, "--no-think"]))
        cfg.host = fake.host
        assert run_headless(cfg, project, "hello", cont=False) == 0
        assert fake.installed
        assert fake.requests[0].get("think") is False
    assert "downloading" in capsys.readouterr().err


def test_flags():
    a = parse_args(["--yolo", "--ctx", "8192", "proj"])
    cfg = build_config(a)
    assert cfg.mode == "yolo" and cfg.num_ctx == 8192 and a.directory == "proj"
    assert cfg.sandbox is True                       # confined by default
    assert build_config(parse_args(["--no-sandbox"])).sandbox is False


def test_theme_flag():
    assert build_config(parse_args(["--theme", "gruvbox"])).theme == "gruvbox-dark"
    assert build_config(parse_args(["--theme", "tokyo-night"])).theme == "tokyo-night"


def test_config_subcommand(capsys):
    try:
        main(["config"])
    except SystemExit as e:
        assert e.code == 0
    assert "config.toml" in capsys.readouterr().out
