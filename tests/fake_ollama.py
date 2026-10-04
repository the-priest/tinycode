"""A tiny scripted stand-in for the Ollama HTTP API, for tests and demos.

Each /api/chat request pops the next scripted reply. A reply is a dict:
  {"thinking": str, "content": str, "tool_calls": [...], "chunk": int,
   "done_reason": "stop"}
or a callable(messages) -> such a dict.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Union

Reply = Union[dict, Callable[[list], dict]]


class FakeOllama:
    def __init__(self, replies: list[Reply] | None = None, model: str = "fake:latest",
                 delay: float = 0.0, installed: bool = True, port: int = 0):
        self.replies = list(replies or [])
        self.model = model
        self.delay = delay
        self.installed = installed
        self.requests: list[dict] = []
        self.loaded = False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _json(self, obj: Any, code: int = 200) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/api/version":
                    self._json({"version": "0.99.0-fake"})
                elif self.path == "/api/tags":
                    self._json({"models": [{"name": outer.model, "size": 5_300_000_000}]
                                if outer.installed else []})
                elif self.path == "/api/ps":
                    self._json({"models": [{"name": outer.model, "size": 5_100_000_000,
                                            "size_vram": 0}] if outer.loaded else []})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/generate":
                    outer.loaded = payload.get("keep_alive") != 0
                    self._json({"done": True})
                elif self.path == "/api/show":
                    self._json({"capabilities": ["completion", "tools", "thinking"]})
                elif self.path == "/api/pull":
                    self.send_response(200)
                    self.end_headers()
                    for i in range(0, 101, 25):
                        self.wfile.write(json.dumps({"status": "pulling", "total": 100,
                                                     "completed": i}).encode() + b"\n")
                    self.wfile.write(b'{"status":"success"}\n')
                    outer.installed = True
                elif self.path == "/api/chat":
                    outer.requests.append(payload)
                    self._chat(payload)
                else:
                    self._json({"error": "not found"}, 404)

            def _chat(self, payload: dict) -> None:
                reply: Any = outer.replies.pop(0) if outer.replies else {"content": "Done."}
                if callable(reply):
                    reply = reply(payload["messages"])
                reply = dict(reply)
                if reply.get("tool_calls") and not payload.get("tools") \
                        and not reply.get("native"):
                    # no native tools requested: the model writes the call as text
                    text = "".join(
                        "\n<tool_call>\n" + json.dumps(tc) + "\n</tool_call>"
                        for tc in reply.pop("tool_calls"))
                    reply["content"] = (reply.get("content") or "") + text
                    reply["pause"] = 0
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                size = reply.get("chunk", 12)

                def send(msg: dict, done: bool = False, **extra: Any) -> bool:
                    obj = {"model": outer.model, "message": {"role": "assistant", **msg},
                           "done": done, **extra}
                    try:
                        self.wfile.write(json.dumps(obj).encode() + b"\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return False
                    if outer.delay:
                        time.sleep(outer.delay)
                    return True

                for key in ("thinking", "content"):
                    text = reply.get(key) or ""
                    for i in range(0, len(text), size):
                        if not send({"content": "", key: text[i:i + size]}
                                    if key == "thinking" else {"content": text[i:i + size]}):
                            return
                if reply.get("error"):
                    send({"content": ""})
                    try:
                        self.wfile.write(json.dumps({"error": reply["error"]}).encode() + b"\n")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if reply.get("pause"):
                    time.sleep(reply["pause"])   # silent generation (tool-call args)
                if reply.get("tool_calls"):
                    if not send({"content": "", "tool_calls": [
                            {"function": tc} for tc in reply["tool_calls"]]}):
                        return
                send({"content": ""}, True, done_reason=reply.get("done_reason", "stop"),
                     prompt_eval_count=1200, eval_count=80, eval_duration=4_000_000_000)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def __enter__(self) -> "FakeOllama":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":  # used as a fake `ollama serve` binary in tests
    import os
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        port = int(os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").rsplit(":", 1)[-1])
        f = FakeOllama(port=port)
        f.server.serve_forever()
