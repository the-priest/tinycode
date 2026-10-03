import asyncio

from fake_ollama import FakeOllama
from tinycode.config import Config
from tinycode.tui.app import TinyCodeApp
from tinycode.tui.widgets import ApprovalScreen, ToolCallView


async def wait_for(pred, pilot, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await pilot.pause(0.05)
    raise AssertionError("condition not met")


async def test_tui_full_turn(project, tmp_path):
    replies = [
        {"thinking": "I need to read calc.py and fix the subtraction.",
         "tool_calls": [{"name": "todowrite", "arguments": {"todos": [
             {"content": "Read calc.py", "status": "in_progress"},
             {"content": "Fix add()", "status": "pending"},
             {"content": "Run it", "status": "pending"}]}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
        {"tool_calls": [{"name": "edit_file", "arguments": {
            "path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}}]},
        {"tool_calls": [{"name": "bash", "arguments": {"command": "python3 calc.py"}}]},
        {"content": "Fixed **`add()`** — it subtracted instead of adding.\n\n"
                    "```\n$ python3 calc.py\n5\n```"},
    ]
    with FakeOllama(replies) as fake:
        cfg = Config(model=fake.model, host=fake.host)
        app = TinyCodeApp(cfg, project)
        async with app.run_test(size=(140, 45)) as pilot:
            await wait_for(lambda: app.ready, pilot)
            app.prompt.load_text("fix the bug in @calc.py")
            await pilot.press("enter")
            await wait_for(lambda: isinstance(app.screen, ApprovalScreen), pilot)
            await pilot.press("1")                # approve edit
            await wait_for(lambda: isinstance(app.screen, ApprovalScreen), pilot)
            await pilot.press("2")                # approve + always for python3
            await wait_for(lambda: not app.agent.busy and len(fake.requests) == 5, pilot)
            await pilot.pause(0.3)
            app.save_screenshot(str(tmp_path / "shot.svg"))
            views = list(app.query(ToolCallView))
            assert [v.tool for v in views] == ["todowrite", "read_file", "edit_file", "bash"]
            assert all(v.state == "ok" for v in views)
            assert "return a + b" in (project / "calc.py").read_text()
            assert "bash:python3" in app.agent.always
            # the @mention attached the file
            assert '<file path="calc.py">' in fake.requests[0]["messages"][1]["content"]
            # slash command: undo
            app.prompt.load_text("/undo")
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert "a - b" in (project / "calc.py").read_text()
            # completion menu for commands
            app.prompt.load_text("/he")
            await pilot.pause(0.1)
            assert app.menu.display
            await pilot.press("escape")
            assert not app.menu.display
        # session saved
        assert app.session.path.exists()


async def test_tui_boot_failure_is_reported(project):
    cfg = Config(host="127.0.0.1:9", manage_server=False)
    app = TinyCodeApp(cfg, project)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        text = " ".join(str(w.render()) for w in app.query(".notice"))
        assert "manage_server is off" in text


async def test_message_typed_during_boot_is_sent(project):
    with FakeOllama([{"content": "hello there"}], delay=0.0) as fake:
        app = TinyCodeApp(Config(model=fake.model, host=fake.host), project)
        real_load = app.client.load

        def slow_load():
            import time
            time.sleep(0.8)
            return real_load()
        app.client.load = slow_load
        async with app.run_test(size=(120, 40)) as pilot:
            assert not app.ready
            await app._submit("hi")          # before ready → queued
            assert app.queue == ["hi"]
            await wait_for(lambda: len(fake.requests) == 1 and not app.agent.busy, pilot)
            assert app.agent.messages[-1]["content"] == "hello there"
