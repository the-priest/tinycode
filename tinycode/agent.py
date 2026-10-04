"""The agent loop. UI-agnostic: the TUI and the headless runner both drive it
through the AgentUI callbacks."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import Config, log
from .context import (COMPACT_PROMPT, build_system_prompt, estimate_tokens, prune,
                      transcript_for_summary)
from .ollama import ChatStream, Ollama, OllamaError
from .parsing import (RepetitionGuard, call_complete, extract_text_tool_calls,
                      missing_required, normalize_native_calls, partial_call,
                      tool_call_start, unknown_tool_name)
from .tools import EDIT_TOOLS, TOOL_NAMES, TOOL_SCHEMAS, ToolResult, Tools



@dataclass
class Decision:
    allow: bool
    always: bool = False
    feedback: str = ""


class AgentUI:
    """Override what you need. All async methods run on the event loop."""

    async def step_start(self, step: int) -> None: ...
    async def thinking_delta(self, text: str) -> None: ...
    async def thinking_end(self, seconds: float) -> None: ...
    async def content_delta(self, text: str) -> None: ...
    async def content_end(self, final_text: str) -> None: ...
    async def tool_start(self, call_id: str, name: str, args: dict) -> None: ...
    def tool_output(self, call_id: str, text: str) -> None:  # any thread
        ...
    async def tool_end(self, call_id: str, name: str, args: dict,
                       result: ToolResult, seconds: float) -> None: ...
    async def approve(self, name: str, args: dict, diff: str, warning: str) -> Decision:
        return Decision(False, feedback="no interactive approval available")
    async def notice(self, text: str, level: str = "info") -> None: ...
    async def stats(self, info: dict) -> None: ...
    async def todos(self, todos: list[dict]) -> None: ...
    async def status(self, text: str) -> None: ...
    async def step_discarded(self) -> None:
        """The current step's output is being thrown away and redone."""

    async def tool_draft(self, name: str, args: dict) -> None:
        """A tool call is being written (stream mode): partial name/args so far."""

    async def generating(self, seconds: float, est_tokens: int, started: bool) -> None:
        """Nothing has streamed for a while. Either the model is still reading
        its context (started=False), or it is producing output we can't see
        yet: Ollama holds back a tool call until it is complete (e.g. a whole
        file for write_file)."""


@dataclass
class StepResult:
    content: str = ""
    thinking: str = ""
    raw_calls: list = field(default_factory=list)
    final: dict = field(default_factory=dict)
    aborted: str = ""        # "", cancel, repetition, think_budget, length
    call_started: bool = False   # stream mode: a text tool call was begun
    think_seconds: float = 0.0


class Agent:
    def __init__(self, cfg: Config, client: Ollama, workdir: Path, ui: AgentUI):
        self.cfg = cfg
        self.client = client
        self.workdir = workdir
        self.ui = ui
        self.tools = Tools(workdir, cfg, on_todos=self._on_todos)
        self.system_prompt, self.memory_files = build_system_prompt(workdir, cfg.tool_mode)
        self.messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        self.mode = cfg.mode
        self.think = cfg.think
        self.always: set[str] = set()
        self.undo_stack: list[dict] = []
        self._step_note = ""
        self.busy = False
        self._cancel = False
        self._stream: Optional[ChatStream] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.last_prompt_tokens = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.calib = 1.0
        self.turn_started = 0.0
        self.rate = 10.0          # measured output speed, tokens/s
        self.stream_tools = cfg.tool_mode == "stream"

    # ----------------------------------------------------------- utilities
    def _on_todos(self, todos: list[dict]) -> None:
        loop = self._loop
        if loop is not None:
            asyncio.run_coroutine_threadsafe(self.ui.todos(todos), loop)

    def reset(self) -> None:
        self.system_prompt, self.memory_files = build_system_prompt(self.workdir, self.cfg.tool_mode)
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.tools.todos = []
        self.tools.read_files.clear()
        self.tools.seen.clear()
        self.undo_stack.clear()
        self.last_prompt_tokens = 0

    def refresh_system_prompt(self) -> None:
        self.system_prompt, self.memory_files = build_system_prompt(self.workdir, self.cfg.tool_mode)
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": self.system_prompt}

    def context_used(self) -> int:
        est = int(estimate_tokens(self.messages) * self.calib)
        return max(est, self.last_prompt_tokens) if self.last_prompt_tokens else est

    def cancel(self) -> None:
        self._cancel = True
        self.tools.cancel.set()
        if self._stream is not None:
            self._stream.close()

    # ------------------------------------------------------------ the loop
    async def run(self, user_text: str, display_text: Optional[str] = None) -> str:
        """Run one user turn to completion. Returns the final answer text."""
        self._loop = asyncio.get_running_loop()
        self.busy = True
        self._cancel = False
        self.tools.cancel.clear()
        self.turn_started = time.monotonic()
        self.messages.append({"role": "user", "content": user_text})
        if self.tools.todos_auto or not self.tools.todos:
            self.tools.todos = []
            self.tools.todos_auto = True
        self._step_note = ""
        final_text = ""
        recent: dict[str, int] = {}
        retries = 0
        last_abort = ""
        gate_rounds = 0
        step = 0
        try:
            while step < self.cfg.max_steps:
                step += 1
                if self._cancel:
                    break
                await self.ui.step_start(step)
                await self._fit_context()
                think = self.think and retries == 0
                nudge = None
                if last_abort == "broken_call":
                    nudge = ("Your previous tool call was cut off before it was complete, so "
                             "nothing was done. If you were writing a big file, write it in "
                             "parts: write_file with the first part, then write_file with "
                             "append=true for each next part. Keep each call reasonably short.")
                elif last_abort == "length":
                    nudge = ("Your previous reply hit the length limit before it finished. "
                             "Try again. If a file is very large you can write it in "
                             "several calls using write_file with append=true.")
                elif retries >= 2:
                    nudge = ("Your previous attempt failed (it got stuck or was empty). "
                             "Respond now: either call ONE tool, or give the final answer "
                             "in a few sentences.")
                try:
                    res = await self._model_step(think, nudge)
                except OllamaError as exc:
                    # Ollama rejects a tool call it can't parse (usually one that was
                    # cut off by the length limit). Recover instead of ending the turn.
                    if "tool call" not in str(exc).lower() or retries >= 2 or self._cancel:
                        raise
                    log(f"broken tool call, retrying: {exc}")
                    retries += 1
                    last_abort = "broken_call"
                    await self.ui.content_end("")
                    await self.ui.notice(
                        "the model's tool call came out broken (probably too long) — "
                        "retrying", "warn")
                    step -= 1
                    continue

                if res.aborted == "cancel":
                    if res.content.strip():
                        self.messages.append({"role": "assistant",
                                              "content": res.content.strip() + "\n[interrupted]"})
                    break

                calls, errors = normalize_native_calls(res.raw_calls)
                content = res.content
                if not calls and not errors:
                    calls, content = extract_text_tool_calls(content)
                    if self.stream_tools:
                        calls = calls[:1]   # one call per step; the rest is guesswork
                    if res.call_started and not calls and not self._cancel:
                        start = tool_call_start(res.content)
                        bad = unknown_tool_name(res.content[start:] if start >= 0 else "")
                        if bad:
                            content = res.content[:start]
                            errors = [f"unknown tool '{bad}'. Available tools: "
                                      + ", ".join(TOOL_NAMES)]
                        elif self.stream_tools:
                            # A format we can't read. Don't make the model pay
                            # for it: switch to Ollama's own tool-call parser
                            # (it knows the model's native format) and redo the step.
                            log("unparsed text tool call, switching to native tool calling:\n"
                                + res.content[-4000:])
                            await self.ui.content_end(res.content[:start].strip() if start >= 0
                                                      else "")
                            await self.ui.step_discarded()
                            self.set_tool_mode("native")
                            await self.ui.notice(
                                "the model used a tool-call format tinycode can't show live — "
                                "switched to Ollama's built-in tool calling for this session",
                                "dim")
                            step -= 1
                            continue
                await self.ui.content_end(content.strip())

                last_abort = res.aborted if not calls else ""
                if res.aborted and not calls:
                    retries += 1
                    why = {"repetition": "the model started repeating itself",
                           "think_budget": "the model over-thought",
                           "length": "the reply hit the length limit"}.get(res.aborted, res.aborted)
                    if retries <= 3:
                        await self.ui.notice(f"{why} — retrying with a focused prompt", "warn")
                        step -= 1 if retries <= 2 else 0
                        continue
                    await self.ui.notice(f"{why}; stopping. Try rephrasing the request.", "error")
                    break

                if calls or errors:
                    retries = 0
                    self._step_note = content.strip()
                    self._record_assistant(content.strip(), calls, res.content)
                    for err in errors:
                        self._add_result("", f"ERROR: {err}")
                        await self.ui.notice(err.split(".")[0], "warn")
                    for i, call in enumerate(calls):
                        if self._cancel:
                            # keep history valid: every call gets a result
                            for c in calls[i:]:
                                self._add_result(c["function"]["name"], "interrupted by the user")
                            break
                        await self._run_call(call, recent)
                    continue

                text = content.strip()
                if not text:
                    retries += 1
                    if retries <= 2:
                        step -= 1
                        continue
                    text = "(no answer)"
                self.messages.append({"role": "assistant", "content": text})
                # finish gate: don't let the turn end with broken code
                if self.cfg.auto_check and gate_rounds < self.cfg.check_rounds \
                        and not self._cancel:
                    report, nbad = await asyncio.to_thread(self._final_check)
                    if report:
                        gate_rounds += 1
                        await self.ui.notice(
                            f"✗ automatic check: {nbad} file(s) still have problems — "
                            "sending them back to the model to fix", "warn")
                        self.messages.append({"role": "user", "content":
                            "[automatic check] You are not done yet. These files you changed "
                            "still have errors:\n\n" + report +
                            "\n\nFix every problem with edit_file (or write_file for an "
                            "unfinished file), then give your final summary."})
                        continue
                    if gate_rounds:
                        await self.ui.notice("✓ automatic check: all changed files pass", "ok")
                final_text = text
                break
            else:
                await self.ui.notice(
                    f"stopped after {self.cfg.max_steps} steps. Say 'continue' to keep going.",
                    "warn")
        except OllamaError as exc:
            log(f"model error: {exc}")
            await self.ui.notice(f"model error: {exc}", "error")
        finally:
            if self.tools.todos_auto and self.tools.todos:
                for t in self.tools.todos:
                    t["status"] = "completed"
                await self.ui.todos(self.tools.todos)
            cp = self.tools.take_checkpoint()
            if cp:
                self.undo_stack.append(cp)
                self.undo_stack = self.undo_stack[-20:]
            self._stream = None
            self.busy = False
            if self._cancel:
                await self.ui.notice("interrupted", "warn")
        return final_text

    async def _fit_context(self) -> None:
        reserve = min(self.cfg.num_predict, self.cfg.num_ctx // 4) + 256
        budget = int((self.cfg.num_ctx - reserve) / max(self.calib, 0.5))
        if estimate_tokens(self.messages) <= budget:
            return
        self.messages, n = prune(self.messages, budget)
        if n:
            self.tools.seen.clear()   # pruned output must be re-readable
            await self.ui.notice("context nearly full — trimmed old tool output", "dim")

    async def _model_step(self, think: bool, nudge: Optional[str]) -> StepResult:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        DONE = object()
        messages = self.messages
        if nudge:
            messages = messages + [{"role": "user", "content": f"[system note] {nudge}"}]
        est = estimate_tokens(messages)

        def producer() -> None:
            try:
                if self.stream_tools:
                    stream = self.client.chat_stream(
                        messages, None, think=think,
                        options={"stop": ["</tool_call>", "<tool_response>"]})
                else:
                    stream = self.client.chat_stream(messages, TOOL_SCHEMAS, think=think)
                self._stream = stream
                if self._cancel:
                    stream.close()
                for chunk in stream:
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, {"__error__": exc})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, DONE)

        threading.Thread(target=producer, daemon=True).start()
        res = StepResult()
        guard = RepetitionGuard()
        content: list[str] = []
        thinking: list[str] = []
        think_start = 0.0
        think_last = 0.0
        think_open = False
        suppress = False   # hide raw text tool calls while streaming

        started = time.monotonic()
        last_chunk = started
        visible_tokens = 0
        shown = 0            # chars of content already sent to the UI
        call_at = -1
        last_draft = 0.0
        call_done = False
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                idle = time.monotonic() - last_chunk
                if idle >= 2.0 and not self._cancel:
                    # silent generation (tool-call arguments): estimate progress
                    await self.ui.generating(idle, int(idle * self.rate), visible_tokens > 0)
                continue
            if chunk is DONE:
                break
            now = time.monotonic()
            m = chunk.get("message") or {}
            if m.get("thinking") or m.get("content"):
                visible_tokens += 1      # Ollama streams ~1 token per chunk
                if now - started > 3 and visible_tokens > 20:
                    self.rate = 0.8 * self.rate + 0.2 * (visible_tokens / (now - started))
            last_chunk = now
            if "__error__" in chunk:
                exc = chunk["__error__"]
                if self._cancel:
                    continue
                raise exc if isinstance(exc, OllamaError) else OllamaError(str(exc))
            msg = chunk.get("message") or {}
            t = msg.get("thinking")
            if t:
                if not think_open:
                    think_open = True
                    think_start = time.monotonic()
                think_last = time.monotonic()
                thinking.append(t)
                await self.ui.thinking_delta(t)
                if guard.feed(t):
                    res.aborted = "repetition"
            c = msg.get("content")
            if c:
                if think_open:
                    think_open = False
                    res.think_seconds = think_last - think_start
                    await self.ui.thinking_end(res.think_seconds)
                content.append(c)
                so_far = "".join(content)
                if not suppress:
                    start = tool_call_start(so_far)
                    if start < 0 and so_far.lstrip().startswith(("<function", "{'name'")):
                        start = len(so_far) - len(so_far.lstrip())
                    if start >= 0:
                        suppress = True
                        call_at = start
                        res.call_started = True
                        before = so_far[:start][shown:]
                        if before.strip():
                            await self.ui.content_delta(before)
                    else:
                        # hold back a possible partial "<tool_call" at the very end
                        safe = len(so_far)
                        lt = so_far.rfind("<", max(0, len(so_far) - 12))
                        if lt >= 0 and "<tool_call>".startswith(so_far[lt:]):
                            safe = lt
                        if safe > shown:
                            await self.ui.content_delta(so_far[shown:safe])
                            shown = safe
                if suppress:
                    if now - last_draft > 0.08:
                        last_draft = now
                        name, args = partial_call(so_far)
                        await self.ui.tool_draft(name, args)
                    if self.stream_tools and call_complete(so_far, call_at):
                        call_done = True   # stop here: one call per step
                        if self._stream is not None:
                            self._stream.close()
                elif guard.feed(c):
                    res.aborted = "repetition"
            if msg.get("tool_calls"):
                res.raw_calls.extend(msg["tool_calls"])
            if call_done:
                continue
            if chunk.get("done"):
                res.final = chunk
                if chunk.get("done_reason") == "length" and not res.raw_calls:
                    res.aborted = "length"
            if res.aborted and self._stream is not None:
                self._stream.close()
            if self._cancel:
                res.aborted = "cancel"
                if self._stream is not None:
                    self._stream.close()
        if think_open:
            res.think_seconds = think_last - think_start
            await self.ui.thinking_end(res.think_seconds)
        if self._cancel:
            res.aborted = "cancel"
        res.content = "".join(content)
        res.thinking = "".join(thinking)

        if call_done:
            name, args = partial_call(res.content)
            await self.ui.tool_draft(name, args)
        f = res.final
        if not f and visible_tokens:
            f = {"eval_count": visible_tokens,
                 "eval_duration": int((time.monotonic() - started) * 1e9)}
        if f:
            pin, pout = f.get("prompt_eval_count") or 0, f.get("eval_count") or 0
            self.tokens_in += pin
            self.tokens_out += pout
            if pin:
                self.last_prompt_tokens = pin + pout
                if est > 200 and pin > 200:
                    # learn the real chars-per-token ratio for this model
                    self.calib = max(0.6, min(1.8, 0.7 * self.calib + 0.3 * (pin / est)))
            dur = (f.get("eval_duration") or 0) / 1e9
            if dur > 1 and pout > 20:
                self.rate = pout / dur
            await self.ui.stats({
                "prompt_tokens": pin, "output_tokens": pout,
                "tok_s": (pout / dur) if dur else 0.0,
                "context": self.context_used(), "num_ctx": self.cfg.num_ctx})
        return res

    async def _run_call(self, call: dict, recent: dict[str, int]) -> None:
        fn = call["function"]
        name, args = fn["name"], fn.get("arguments") or {}
        call_id = call.get("id") or ""
        await self.ui.tool_start(call_id, name, args)
        t0 = time.monotonic()

        missing = missing_required(name, args)
        if missing:
            result = ToolResult(
                f"ERROR: missing required argument(s) for {name}: {', '.join(missing)}. "
                f"Call it again with all required arguments.", ok=False,
                summary=f"missing {', '.join(missing)}")
            await self._finish_call(call_id, name, args, result, t0)
            return

        sig = name + json.dumps(args, sort_keys=True, default=str)
        recent[sig] = recent.get(sig, 0) + 1

        needs, warning = self.tools.classify(name, args, self.mode)
        diff, preview_err = ("", None)
        if name in EDIT_TOOLS:
            diff, preview_err = await asyncio.to_thread(self.tools.preview, name, args)
        key = self._always_key(name, args)
        if needs and not preview_err and not (key in self.always and not warning):
            await self.ui.status(f"waiting for approval: {name}")
            decision = await self.ui.approve(name, args, diff, warning)
            if self._cancel:
                decision = Decision(False, feedback="interrupted")
            if not decision.allow:
                fb = decision.feedback.strip()
                out = "The user DENIED this action; it was not performed."
                if fb:
                    out += f" The user said: {fb}"
                else:
                    out += " Ask the user how to proceed or try a different approach."
                await self._finish_call(call_id, name, args,
                                        ToolResult(out, ok=False, summary="denied"), t0)
                return
            if decision.always:
                self.always.add(key)

        if name != "todowrite":
            await self._log_step(name, args)

        def on_out(text: str) -> None:
            self.ui.tool_output(call_id, text)
        self.tools.on_output = on_out
        await self.ui.status(f"running {name}…")
        try:
            result = await asyncio.to_thread(self.tools.execute, name, args)
        finally:
            self.tools.on_output = None
        if recent[sig] >= 3:
            result.output += (f"\n\nNOTE: you have made this exact {name} call "
                              f"{recent[sig]} times. Do something different, or finish.")
        await self._finish_call(call_id, name, args, result, t0)

    def _final_check(self) -> tuple[str, int]:
        """Re-check every file changed this turn. Returns (report, files_with_problems)."""
        from . import checks
        paths = [Path(p) for p in self.tools.checkpoint]
        results = self.tools.check_paths(paths)
        parts = []
        for r in results:
            if not r.checked or not (r.errors or r.incomplete):
                continue
            p = Path(r.path)
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            parts.append(checks.for_model(r, text, self.tools.rel(p), final=True).strip())
        return "\n\n".join(parts), len(parts)

    async def _log_step(self, name: str, args: dict) -> None:
        """When the model doesn't keep a plan, show what it's doing as one."""
        if not self.tools.todos_auto:
            return
        note = self._step_note.split("\n")[0].strip()
        self._step_note = ""
        if note:
            note = note.split(". ")[0].rstrip(".:")
        else:
            from .tui.theme import TOOL_LABELS
            target = str(args.get("path") or args.get("command") or args.get("pattern")
                         or args.get("url") or "")
            target = target.replace(str(self.workdir) + "/", "")
            note = f"{TOOL_LABELS.get(name, name)} {target}".strip()
        if len(note) > 60:
            note = note[:59] + "…"
        todos = self.tools.todos
        if todos and todos[-1]["content"] == note:
            return
        for t in todos:
            t["status"] = "completed"
        todos.append({"content": note, "status": "in_progress"})
        self.tools.todos = todos[-12:]
        await self.ui.todos(self.tools.todos)

    def set_tool_mode(self, mode: str) -> None:
        self.cfg.tool_mode = mode
        self.stream_tools = mode == "stream"
        self.refresh_system_prompt()

    def _record_assistant(self, text: str, calls: list[dict], raw: str) -> None:
        if not self.stream_tools:
            self.messages.append({"role": "assistant", "content": text,
                                  "tool_calls": [{"function": c["function"]} for c in calls]})
            return
        # stream mode: keep the history in the exact text format we ask for
        parts = [text] if text else []
        for c in calls:
            parts.append("<tool_call>\n" + json.dumps(
                {"name": c["function"]["name"], "arguments": c["function"]["arguments"]},
                ensure_ascii=False) + "\n</tool_call>")
        if not calls and raw.strip():
            parts = [raw.strip()]
        self.messages.append({"role": "assistant", "content": "\n".join(parts)})

    def _add_result(self, name: str, output: str) -> None:
        if self.stream_tools:
            tag = f'<tool_response name="{name}">' if name else "<tool_response>"
            self.messages.append({"role": "user",
                                  "content": f"{tag}\n{output}\n</tool_response>"})
        else:
            msg = {"role": "tool", "content": output}
            if name:
                msg["tool_name"] = name
            self.messages.append(msg)

    async def _finish_call(self, call_id: str, name: str, args: dict,
                           result: ToolResult, t0: float) -> None:
        self._add_result(name, result.output)
        await self.ui.tool_end(call_id, name, args, result, time.monotonic() - t0)

    @staticmethod
    def _always_key(name: str, args: dict) -> str:
        if name in EDIT_TOOLS:
            return "edit"
        if name == "bash":
            words = str(args.get("command", "")).split()
            return "bash:" + (words[0] if words else "")
        return name

    # ------------------------------------------------------------ commands
    def undo(self) -> list[str]:
        if not self.undo_stack:
            return []
        cp = self.undo_stack.pop()
        restored = self.tools.restore(cp)
        if restored:
            self.messages.append({"role": "user", "content":
                                  "[The user reverted your file changes to: "
                                  + ", ".join(restored) + ". They are back to their "
                                  "previous state.]"})
            self.messages.append({"role": "assistant", "content": "Noted — those changes were reverted."})
        return restored

    async def compact(self) -> str:
        """Summarize the conversation with the model and restart from the summary."""
        if len(self.messages) < 3:
            return ""
        transcript = transcript_for_summary(self.messages)
        msgs = [{"role": "system", "content": "You summarize coding sessions precisely."},
                {"role": "user", "content": f"{COMPACT_PROMPT}\n\n<conversation>\n{transcript}\n</conversation>"}]
        summary = await asyncio.to_thread(self.client.complete, msgs, False, 1200)
        self.tools.seen.clear()
        if not summary:
            raise OllamaError("the model returned an empty summary")
        self.messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": "[Summary of our conversation so far]\n" + summary},
            {"role": "assistant", "content": "Got it. I have the context and will continue from here."},
        ]
        self.last_prompt_tokens = 0
        return summary

    def load_messages(self, messages: list[dict]) -> None:
        body = [m for m in messages if m.get("role") != "system"]
        self.messages = [{"role": "system", "content": self.system_prompt}] + body
