import asyncio

import pytest

from fake_ollama import FakeOllama
from tinycode.agent import Agent, AgentUI, Decision
from tinycode.config import Config
from tinycode.ollama import Ollama


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
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool",
                     "assistant", "tool", "assistant"]
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
        agent = make(project, fake, mode="yolo")
        await agent.run("x")
    tool_msgs = [m["content"] for m in agent.messages if m["role"] == "tool"]
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
