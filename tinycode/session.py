"""Saving and resuming conversations."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import log, sessions_dir


@dataclass
class Session:
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    cwd: str = ""
    title: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    messages: list[dict] = field(default_factory=list)
    todos: list[dict] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def path(self) -> Path:
        return sessions_dir() / f"{self.id}.json"

    def save(self) -> None:
        if not any(m.get("role") == "user" for m in self.messages):
            return
        self.updated = time.time()
        if not self.title:
            first = next((m.get("content", "") for m in self.messages
                          if m.get("role") == "user"), "")
            self.title = " ".join(str(first).split())[:70]
        data = {k: getattr(self, k) for k in
                ("id", "cwd", "title", "created", "updated", "messages", "todos",
                 "tokens_in", "tokens_out")}
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log(f"session save failed: {exc}")

    @classmethod
    def load(cls, path: Path) -> "Session":
        data = json.loads(path.read_text(encoding="utf-8"))
        s = cls()
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


def list_sessions(cwd: Optional[str] = None, limit: int = 30) -> list[dict]:
    out = []
    for p in sorted(sessions_dir().glob("*.json"), key=lambda x: x.stat().st_mtime,
                    reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if cwd and data.get("cwd") != cwd:
            continue
        n = sum(1 for m in data.get("messages", []) if m.get("role") == "user")
        out.append({"id": data.get("id"), "title": data.get("title") or "(untitled)",
                    "cwd": data.get("cwd"), "updated": data.get("updated", 0),
                    "turns": n, "path": p})
        if len(out) >= limit:
            break
    return out


def latest_session(cwd: str) -> Optional[Session]:
    items = list_sessions(cwd, limit=1)
    if not items:
        return None
    try:
        return Session.load(items[0]["path"])
    except (OSError, ValueError):
        return None


def ago(ts: float) -> str:
    d = max(0, time.time() - ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"
