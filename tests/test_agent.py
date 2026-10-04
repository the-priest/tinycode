import asyncio

import pytest

from fake_ollama import FakeOllama
from tinycode.agent import Agent, AgentUI, Decision
from tinycode.config import Config
from tinycode.ollama import Ollama
from tinycode.parsing import is_tool_result


class RecUI(AgentUI):
    def __init__(self, allow=True):
        self.events = []
        self.allow = allow

    async def tool_start(self, call_id, name, args):
        self.events.append(("tool", name))

    async def tool_end(self, call_id, name, args, result, seconds):
        self.events.append(("result", name, result.ok))

    async def notice(self, text, level="info"):
        self.events.append(("notice", text))

    async def approve(self, name, args, diff, warning):
        self.events.append(("approve", name, bool(diff)))
        return Decision(self.allow, feedback="" if self.allow else "use a different approach")


def make(project, fake, ui=None, **cfg):
    c = Config(model=fake.model, host=fake.host, **cfg)
    return Agent(c, Ollama(c), project, ui or RecUI())


async def test_fix_bug_end_to_end(project):
    replies = [
        {"thinking": "Read the file first.", "tool_calls": [
            {"name": "read_file", "arguments": {"path": "calc.py"}}]},
        # sloppy edit: wrong indentation + alias arg names
        {"tool_calls": [{"name": "str_replace", "arguments": {
            "file_path": "calc.py", "old": "return a - b", "new": "return a + b"}}]},
        {"tool_calls": [{"name": "run_bash", "arguments": {"cmd": "python3 calc.py"}}]},
        {"content": "Fixed `add`: it now returns **5** for `add(2, 3)`."},
    ]
    with FakeOllama(replies) as fake:
        ui = RecUI()
        agent = make(project, fake, ui)
        answer = await agent.run("fix the add function")
    assert "Fixed" in answer
    assert "return a + b" in (project / "calc.py").read_text()
    assert ("approve", "edit_file", True) in ui.events      # showed a diff
    assert ("result", "bash", True) in ui.events
    kinds = ["result" if is_tool_result(m) else m["role"] for m in agent.messages]
    assert kinds == ["system", "user", "assistant", "result", "assistant", "result",
                     "assistant", "result", "assistant"]
    bash_out = agent.messages[7]["content"]
    assert "5" in bash_out and "exit code 0" in bash_out
    # undo restores the original
    assert agent.undo() == ["calc.py"]
    assert "a - b" in (project / "calc.py").read_text()


async def test_text_tool_calls_recovered(project):
    replies = [
        {"content": 'I will read it.\n<tool_call>{"name": "read", "arguments": {"file": "README.md"}}</tool_call>'},
        {"content": "It is a tiny calculator."},
    ]
    with FakeOllama(replies) as fake:
        agent = make(project, fake)
        answer = await agent.run("what is this project?")
    assert answer == "It is a tiny calculator."
    assert "A tiny calculator" in agent.messages[3]["content"]


async def test_denied_with_feedback(project):
    replies = [
        {"tool_calls": [{"name": "bash", "arguments": {"command": "make deploy"}}]},
        {"content": "OK, I won't deploy."},
    ]
    with FakeOllama(replies) as fake:
        ui = RecUI(allow=False)
        agent = make(project, fake, ui)
        await agent.run("deploy")
    tool_msg = agent.messages[3]["content"]
    assert "DENIED" in tool_msg and "different approach" in tool_msg


async def test_repetition_is_cut_and_retried(project):
    replies = [
        {"thinking": "hmm let me think again. " * 300, "chunk": 40},
        {"content": "The answer is 42."},
    ]
    with FakeOllama(replies) as fake:
        ui = RecUI()
        agent = make(project, fake, ui)
        answer = await agent.run("question")
        # the retry runs with reasoning off
        assert fake.requests[1].get("think") is False
    assert answer == "The answer is 42."
    assert any(e[0] == "notice" and "repeating" in e[1] for e in ui.events)


async def test_bad_tool_and_missing_args(project):
    replies = [
        {"tool_calls": [{"name": "teleport", "arguments": {}},
                        {"name": "edit_file", "arguments": {"path": "calc.py"}}]},
        {"content": "done"},
    ]
    with FakeOllama(replies) as fake:
        agent = make(project, fake, mode="yolo", tool_mode="native")
        await agent.run("x")
    tool_msgs = [m["content"] for m in agent.messages if is_tool_result(m)]
    assert any("unknown tool" in t for t in tool_msgs)
    assert any("missing required" in t for t in tool_msgs)


async def test_cancel_mid_stream(project):
    with FakeOllama([{"content": "word " * 2000, "chunk": 5}], delay=0.01) as fake:
        agent = make(project, fake)
        task = asyncio.create_task(agent.run("long answer please"))
        await asyncio.sleep(0.5)
        agent.cancel()
        await asyncio.wait_for(task, 5)
    assert not agent.busy
    assert agent.messages[-1]["content"].endswith("[interrupted]")


async def test_loop_nudge_on_repeated_calls(project):
    call = {"tool_calls": [{"name": "glob", "arguments": {"pattern": "*.py"}}]}
    with FakeOllama([call, call, call, {"content": "ok"}]) as fake:
        agent = make(project, fake)
        await agent.run("x")
    assert "exact glob call 3 times" in agent.messages[-2]["content"]


async def test_safe_command_needs_no_approval(project):
    replies = [{"tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}]},
               {"content": "listed"}]
    with FakeOllama(replies) as fake:
        ui = RecUI(allow=False)
        agent = make(project, fake, ui)
        await agent.run("list")
    assert not any(e[0] == "approve" for e in ui.events)
    assert "calc.py" in agent.messages[3]["content"]


async def test_silent_tool_call_generation_reports_progress(project):
    class UI(RecUI):
        async def generating(self, seconds, est_tokens, started):
            self.events.append(("generating", started, est_tokens))

    replies = [{"thinking": "Let me build this. " * 3, "pause": 3.5, "chunk": 4, "native": True,
                "tool_calls": [{"name": "write_file",
                                "arguments": {"path": "a.html", "content": "<html></html>"}}]},
               {"content": "done"}]
    with FakeOllama(replies) as fake:
        ui = UI()
        agent = make(project, fake, ui, mode="yolo", tool_mode="native")
        await agent.run("make a page")
    gen = [e for e in ui.events if e[0] == "generating"]
    assert gen and all(e[1] for e in gen) and gen[-1][2] > 0
    assert (project / "a.html").exists()


async def test_length_cutoff_retries_with_parts_hint(project):
    replies = [{"content": "<!DOCTYPE html><html> ... half a file", "done_reason": "length"},
               {"content": "ok"}]
    with FakeOllama(replies) as fake:
        agent = make(project, fake)
        await agent.run("make a big page")
        hint = fake.requests[1]["messages"][-1]["content"]
    assert "length limit" in hint


def test_write_file_append(project):
    from tinycode.tools import Tools
    t = Tools(project, Config())
    t.t_write_file("big.txt", "part 1")
    r = t.t_write_file("big.txt", "part 2\n", append=True)
    assert (project / "big.txt").read_text() == "part 1\npart 2\n"
    assert "appended" in r.output
    diff, err = t.preview("write_file", {"path": "big.txt", "content": "3", "append": True})
    assert err is None and "+3" in diff and "-part 1" not in diff


def test_untouched_old_config_is_upgraded():
    import tinycode.config as c
    c.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    c.CONFIG_PATH.write_text(c._V200_TEMPLATE)
    cfg = c.load_config()
    assert cfg.num_ctx == 32768 and cfg.num_predict == 8192
    c.CONFIG_PATH.write_text("[model]\nnum_ctx = 12000\n")   # user's own choice kept
    assert c.load_config().num_ctx == 12000


async def test_native_mode_end_to_end(project):
    replies = [{"tool_calls": [{"name": "edit_file", "arguments": {
                   "path": "calc.py", "old_string": "a - b", "new_string": "a + b"}}]},
               {"content": "fixed"}]
    with FakeOllama(replies) as fake:
        agent = make(project, fake, mode="yolo", tool_mode="native")
        assert await agent.run("fix") == "fixed"
        assert fake.requests[0].get("tools")
    assert [m["role"] for m in agent.messages] == ["system", "user", "assistant", "tool",
                                                   "assistant"]
    assert "a + b" in (project / "calc.py").read_text()


async def test_stream_mode_shows_draft_and_stops_after_call(project):
    import json

    class UI(RecUI):
        async def tool_draft(self, name, args):
            self.events.append(("draft", name, len(args.get("content", ""))))

    html = "<html>\n" + "".join(f"<p>line {i}</p>\n" for i in range(200)) + "</html>"
    call = json.dumps({"name": "write_file", "arguments": {"path": "p.html", "content": html}})
    replies = [{"content": "Writing the page.\n<tool_call>\n" + call +
                "\n</tool_call>\n<tool_response>I made this up</tool_response>", "chunk": 20},
               {"content": "done"}]
    with FakeOllama(replies, delay=0.004) as fake:
        ui = UI()
        agent = make(project, fake, ui, mode="yolo", tool_mode="stream")
        await agent.run("page")
        assert "tools" not in fake.requests[0]
        assert "</tool_call>" in fake.requests[0]["options"]["stop"]
    drafts = [e for e in ui.events if e[0] == "draft"]
    assert len(drafts) > 3 and drafts[-1][1] == "write_file"
    assert drafts[-1][2] == len(html)                      # streamed the whole file
    assert (project / "p.html").read_text() == html
    assistant = agent.messages[2]["content"]
    assert assistant.startswith("Writing the page.") and "made this up" not in assistant


async def test_stream_mode_unreadable_call_falls_back_to_native(project):
    replies = [{"content": '<tool_call>\n%%%% garbled %%%%\n</tool_call>'},
               {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}],
                "native": True},
               {"content": "ok"}]
    with FakeOllama(replies) as fake:
        agent = make(project, fake, tool_mode="stream")
        assert await agent.run("x") == "ok"
        assert "tools" not in fake.requests[0] and fake.requests[1].get("tools")
    assert agent.cfg.tool_mode == "native"
    results = [m["content"] for m in agent.messages if is_tool_result(m)]
    assert len(results) == 1 and "tiny calculator" in results[0]



async def test_stream_mode_unknown_tool_reported(project):
    replies = [{"content": '<tool_call>\n{"name": "teleport", "arguments": {}}\n</tool_call>'},
               {"content": "ok"}]
    with FakeOllama(replies) as fake:
        agent = make(project, fake, tool_mode="stream")
        await agent.run("x")
    results = [m["content"] for m in agent.messages if is_tool_result(m)]
    assert "unknown tool 'teleport'" in results[0]
