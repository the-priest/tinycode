"""Ollama client and server/model lifecycle.

tinycode owns the Ollama lifecycle by default:
  start -> start `ollama serve` if nothing is listening (or adopt the running
           one), pull the model if missing, load it and wait until resident.
  exit  -> unload the model (free RAM) and stop the server tinycode manages.

A server tinycode spawns is tied to it with PR_SET_PDEATHSIG on Linux, so the
kernel stops it even if tinycode is SIGKILLed or its terminal is closed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .config import Config, cache_dir, log, log_path


class OllamaError(RuntimeError):
    pass


# Makes the spawned server die with us (Linux): set the parent-death signal,
# then exec the real binary so the pid we hold *is* `ollama serve`.
_PDEATHSIG_WRAPPER = (
    "import ctypes, os, signal, sys\n"
    "try:\n"
    "    ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)\n"
    "except Exception:\n"
    "    pass\n"
    "os.execvp(sys.argv[1], [sys.argv[1], 'serve'])\n"
)


class ChatStream:
    """An iterator over /api/chat NDJSON chunks that can be cancelled from
    another thread (closing the socket makes Ollama stop generating)."""

    def __init__(self, resp: Any):
        self._resp = resp
        self._closed = False
        self._lock = threading.Lock()

    def __iter__(self) -> Iterator[dict]:
        try:
            for raw in self._resp:
                if self._closed:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("error"):
                    raise OllamaError(str(obj["error"]))
                yield obj
        except (OSError, ValueError, AttributeError) as exc:
            if not self._closed:
                raise OllamaError(f"stream interrupted: {exc}") from exc
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._resp.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def closed(self) -> bool:
        return self._closed


class Ollama:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/")
        self.proc: Optional[subprocess.Popen] = None
        self._log_fh = None
        self.owned = False                # stop the server on exit?
        self.spawned = False              # did *we* start it?
        self.adopted: list[int] = []
        port = cfg.host.rsplit(":", 1)[-1] if ":" in cfg.host else "11434"
        self.pidfile = cache_dir() / f"ollama-{port}.pid"
        self.supports_think: Optional[bool] = None

    @property
    def model(self) -> str:
        return self.cfg.model

    # ------------------------------------------------------------------ http
    def _request(self, path: str, payload: Optional[dict] = None,
                 timeout: float = 30) -> tuple[bool, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                detail = str(exc)
            try:
                detail = json.loads(detail).get("error", detail)
            except (ValueError, AttributeError):
                pass
            return False, f"HTTP {exc.code}: {detail}"
        except (urllib.error.URLError, OSError) as exc:
            return False, str(getattr(exc, "reason", exc))
        if not body.strip():
            return True, {}
        try:
            return True, json.loads(body)
        except json.JSONDecodeError:
            last: Any = {}
            for chunk in body.splitlines():
                try:
                    last = json.loads(chunk)
                except json.JSONDecodeError:
                    pass
            return True, last

    def alive(self, timeout: float = 2) -> bool:
        ok, _ = self._request("/api/version", timeout=timeout)
        return ok

    def version(self) -> str:
        ok, data = self._request("/api/version", timeout=3)
        return data.get("version", "?") if ok and isinstance(data, dict) else "?"

    def _match(self, name: str) -> bool:
        want = self.model
        if name == want:
            return True
        if ":" not in want and name == want + ":latest":
            return True
        return False

    def list_models(self) -> list[dict]:
        ok, data = self._request("/api/tags", timeout=8)
        if not ok or not isinstance(data, dict):
            return []
        return data.get("models", []) or []

    def has_model(self) -> bool:
        return any(self._match(m.get("name", "")) or self._match(m.get("model", ""))
                   for m in self.list_models())

    def resident(self) -> Optional[dict]:
        ok, data = self._request("/api/ps", timeout=8)
        if not ok or not isinstance(data, dict):
            return None
        for m in data.get("models", []) or []:
            if self._match(m.get("name", "")) or self._match(m.get("model", "")):
                return m
        return None

    def show(self) -> dict:
        ok, data = self._request("/api/show", {"model": self.model}, timeout=15)
        return data if ok and isinstance(data, dict) else {}

    # ------------------------------------------------------- server lifecycle
    @staticmethod
    def binary() -> Optional[str]:
        found = shutil.which("ollama")
        if found:
            return found
        for cand in ("/usr/local/bin/ollama", "/usr/bin/ollama",
                     "/opt/homebrew/bin/ollama",
                     "/Applications/Ollama.app/Contents/Resources/ollama",
                     str(Path.home() / ".local/bin/ollama")):
            if os.path.exists(cand):
                return cand
        return None

    @staticmethod
    def _server_pids() -> list[int]:
        """`ollama serve` processes owned by the current user."""
        pids: list[int] = []
        uid = os.getuid() if hasattr(os, "getuid") else None
        proc = Path("/proc")
        if proc.is_dir():
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if uid is not None and entry.stat().st_uid != uid:
                        continue
                    raw = (entry / "cmdline").read_bytes()
                except OSError:
                    continue
                parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
                if parts and os.path.basename(parts[0]) == "ollama" and "serve" in parts[1:]:
                    pids.append(int(entry.name))
            return pids
        if shutil.which("pgrep"):  # macOS / BSD
            try:
                out = subprocess.run(["pgrep", "-U", str(uid), "-f", "ollama serve"],
                                     capture_output=True, text=True, timeout=5)
                pids = [int(x) for x in out.stdout.split() if x.isdigit()]
            except (OSError, subprocess.SubprocessError):
                pass
        return pids

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _read_pidfile(self) -> Optional[int]:
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def begin_server(self) -> bool:
        """Adopt a running server or spawn one (no waiting). Must run on the
        main thread: PDEATHSIG is bound to the spawning thread.
        Returns True when tinycode spawned the server."""
        stale = self._read_pidfile()
        if stale is not None and not self._pid_alive(stale):
            self._remove_pidfile()
            stale = None

        if self.alive(timeout=1.5):
            self.owned = self.cfg.stop_server_on_exit
            if self.owned:
                found = ([stale] if stale else []) + self._server_pids()
                self.adopted = sorted({p for p in found if p and p != os.getpid()})
                if not self.adopted:
                    # e.g. a system service under another user: leave it alone
                    self.owned = False
            log(f"ollama already running (owned={self.owned}, pids={self.adopted})")
            return False

        if not self.cfg.manage_server:
            raise OllamaError(
                f"no Ollama server at {self.base} and manage_server is off. "
                "Start it with `ollama serve`.")
        exe = self.binary()
        if not exe:
            raise OllamaError(
                "`ollama` is not installed. Run the tinycode installer again, "
                "or install it from https://ollama.com/download")
        log(f"starting `{exe} serve` (owned by tinycode)")
        env = dict(os.environ)
        env.setdefault("OLLAMA_HOST", self.cfg.host)
        try:
            self._log_fh = open(log_path(), "a", encoding="utf-8")
            if sys.platform.startswith("linux"):
                argv = [sys.executable, "-c", _PDEATHSIG_WRAPPER, exe]
            else:
                argv = [exe, "serve"]
            self.proc = subprocess.Popen(
                argv, stdout=self._log_fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        except OSError as exc:
            raise OllamaError(f"could not start ollama: {exc}") from exc
        self.owned = True
        self.spawned = True
        try:
            self.pidfile.write_text(str(self.proc.pid))
        except OSError:
            pass
        return True

    def wait_up(self, timeout: float = 45) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.alive(timeout=1.5):
                log("ollama server is up")
                return
            if self.proc is not None and self.proc.poll() is not None:
                raise OllamaError(
                    f"`ollama serve` exited immediately (code {self.proc.returncode}). "
                    f"See {log_path()}")
            time.sleep(0.3)
        raise OllamaError(f"ollama did not come up within {timeout:.0f}s")

    def _remove_pidfile(self) -> None:
        try:
            self.pidfile.unlink()
        except OSError:
            pass

    @staticmethod
    def _kill_pid(pid: int, wait: float = 3.0) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.time() + wait
        while time.time() < deadline:
            if not Ollama._pid_alive(pid):
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def stop_server(self) -> None:
        if not self.owned:
            return
        pids: set[int] = set(self.adopted)
        spawned = self.proc.pid if self.proc is not None else None
        if spawned:
            pids.add(spawned)
        pids.update(self._server_pids())
        pids.discard(os.getpid())
        pids = {p for p in pids if self._pid_alive(p)}
        if pids:
            log(f"stopping ollama (pids {sorted(pids)})")
        for pid in pids:
            if pid == spawned:
                try:  # whole group: gets the model runner children too
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except OSError:
                    pass
            self._kill_pid(pid)
        if self.proc is not None:
            try:
                self.proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None
        self.proc = None
        self.adopted = []
        self.owned = False
        self._remove_pidfile()

    # -------------------------------------------------------- model lifecycle
    def pull(self, on_progress: Callable[[str, float], None] | None = None) -> None:
        """Pull the model via the HTTP API, reporting (status, fraction)."""
        log(f"pulling {self.model}")
        req = urllib.request.Request(
            self.base + "/api/pull",
            data=json.dumps({"model": self.model, "stream": True}).encode())
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                for raw in resp:
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("error"):
                        raise OllamaError(f"pull failed: {obj['error']}")
                    total = obj.get("total") or 0
                    done = obj.get("completed") or 0
                    frac = (done / total) if total else -1.0
                    if on_progress:
                        on_progress(str(obj.get("status", "")), frac)
        except urllib.error.HTTPError as exc:
            raise OllamaError(f"pull failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise OllamaError(f"pull failed: {exc}") from exc

    def load(self) -> dict:
        log(f"loading {self.model} (num_ctx={self.cfg.num_ctx})")
        ok, data = self._request(
            "/api/generate",
            {"model": self.model, "prompt": "", "keep_alive": -1,
             "options": {"num_ctx": self.cfg.num_ctx}},
            timeout=900)
        if not ok:
            raise OllamaError(f"model load failed: {data}")
        deadline = time.time() + 120
        while time.time() < deadline:
            info = self.resident()
            if info:
                return info
            time.sleep(0.5)
        return {}

    def unload(self, timeout: float = 10) -> bool:
        log("unloading model (keep_alive=0)")
        ok, _ = self._request("/api/generate",
                              {"model": self.model, "keep_alive": 0},
                              timeout=timeout)
        return ok

    # ------------------------------------------------------------------ chat
    def options(self) -> dict:
        c = self.cfg
        opts = {"temperature": c.temperature, "top_p": c.top_p, "top_k": c.top_k,
                "num_ctx": c.num_ctx, "num_predict": c.num_predict,
                "repeat_penalty": c.repeat_penalty}
        if c.extra.get("stop"):
            opts["stop"] = list(c.extra["stop"])
        if c.extra.get("min_p") is not None:
            opts["min_p"] = c.extra["min_p"]
        return opts

    def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                    think: Optional[bool] = None,
                    options: Optional[dict] = None) -> ChatStream:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": -1,
            "options": {**self.options(), **(options or {})},
        }
        if tools:
            payload["tools"] = tools
        if think is not None and self.supports_think is not False:
            payload["think"] = think
        req = urllib.request.Request(self.base + "/api/chat",
                                     data=json.dumps(payload).encode())
        req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=1800)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except (ValueError, AttributeError):
                pass
            if "think" in payload and "think" in str(detail).lower():
                # model can't do thinking: remember and retry without it
                self.supports_think = False
                return self.chat_stream(messages, tools, None, options)
            raise OllamaError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise OllamaError(
                f"cannot reach Ollama at {self.base}: {getattr(exc, 'reason', exc)}"
            ) from exc
        return ChatStream(resp)

    def complete(self, messages: list[dict], think: bool = False,
                 num_predict: int = 1024) -> str:
        """Non-tool, blocking completion (used for summaries / titles)."""
        out: list[str] = []
        for chunk in self.chat_stream(messages, None, think,
                                      {"num_predict": num_predict}):
            out.append((chunk.get("message") or {}).get("content") or "")
        return "".join(out).strip()
