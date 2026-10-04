"""Make a small model's tool calls robust.

* native `tool_calls` are normalized (names + argument aliases + types)
* when the model writes a tool call as text instead (`<tool_call>{...}`,
  a ```json block, `{"name": ..., "arguments": ...}`), we recover it
* repeated / looping output is detected so we can stop it early
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

from .tools import TOOL_NAMES, TOOL_SCHEMAS

NAME_ALIASES = {
    "read": "read_file", "readfile": "read_file", "open_file": "read_file",
    "view": "read_file", "cat": "read_file", "view_file": "read_file",
    "write": "write_file", "create_file": "write_file", "writefile": "write_file",
    "save_file": "write_file",
    "edit": "edit_file", "replace": "edit_file", "str_replace": "edit_file",
    "replace_in_file": "edit_file", "update_file": "edit_file",
    "str_replace_editor": "edit_file", "modify_file": "edit_file",
    "run_bash": "bash", "shell": "bash", "run": "bash", "execute": "bash",
    "run_command": "bash", "exec": "bash", "terminal": "bash", "cmd": "bash",
    "run_shell": "bash", "execute_command": "bash", "command": "bash",
    "ls": "list_dir", "list": "list_dir", "list_files": "list_dir",
    "list_directory": "list_dir", "tree": "list_dir", "dir": "list_dir",
    "find": "glob", "find_files": "glob", "search_files": "glob", "file_search": "glob",
    "search": "grep", "search_code": "grep", "rg": "grep", "ripgrep": "grep",
    "grep_search": "grep", "code_search": "grep",
    "todo": "todowrite", "todo_write": "todowrite", "todos": "todowrite",
    "update_todos": "todowrite", "plan": "todowrite", "write_todos": "todowrite",
    "fetch": "fetch_url", "web_fetch": "fetch_url", "http_get": "fetch_url",
    "curl": "fetch_url", "webfetch": "fetch_url",
    "lint": "check", "check_syntax": "check", "syntax_check": "check", "diagnostics": "check",
    "validate": "check", "check_file": "check", "verify": "check", "check_code": "check",
    "test_page": "test_app", "run_app": "test_app", "test_web": "test_app", "test": "test_app",
    "browser_test": "test_app", "preview": "test_app", "open_in_browser": "test_app",
}

ARG_ALIASES = {
    "path": ["file_path", "filepath", "file", "filename", "file_name", "target",
             "directory", "dir", "folder", "target_file", "abs_path", "relative_path"],
    "content": ["contents", "text", "data", "body", "code", "file_content", "new_content"],
    "old_string": ["old", "old_str", "old_text", "search", "find", "original",
                   "target_text", "from", "before"],
    "new_string": ["new", "new_str", "new_text", "replacement", "replace", "to",
                   "after", "updated"],
    "command": ["cmd", "bash", "shell", "script", "commands", "command_line"],
    "pattern": ["regex", "query", "glob", "search", "expr", "expression", "term"],
    "include": ["glob_filter", "file_pattern", "files", "type", "filter"],
    "url": ["link", "uri", "href", "address"],
    "todos": ["items", "tasks", "todo", "list", "plan"],
    "offset": ["start", "start_line", "line", "from_line"],
    "limit": ["count", "lines", "num_lines", "max_lines"],
    "replace_all": ["all", "global", "replaceall"],
    "ignore_case": ["case_insensitive", "i", "nocase"],
}

_PROPS = {t["function"]["name"]: t["function"]["parameters"]["properties"]
          for t in TOOL_SCHEMAS}


def canonical_name(name: str) -> Optional[str]:
    if not name:
        return None
    n = name.strip().lower().replace("-", "_").replace(" ", "_")
    n = n.split(".")[-1]  # functions.read_file
    if n in TOOL_NAMES:
        return n
    return NAME_ALIASES.get(n)


def _coerce(value: Any, spec: dict) -> Any:
    typ = spec.get("type")
    try:
        if typ == "integer":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, str):
                m = re.search(r"-?\d+", value)
                return int(m.group()) if m else None
            return int(value)
        if typ == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "y")
            return bool(value)
        if typ == "string":
            if isinstance(value, (dict, list)):
                return json.dumps(value)
            return "" if value is None else str(value)
        if typ == "array":
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    return parsed if isinstance(parsed, list) else value
                except ValueError:
                    return value
    except (TypeError, ValueError):
        return None
    return value


def normalize_args(name: str, args: Any) -> dict:
    if isinstance(args, str):
        args = loads_lenient(args)
        if not isinstance(args, dict):
            args = {}
    if not isinstance(args, dict):
        return {}
    props = _PROPS.get(name, {})
    out: dict = {}
    # direct hits first
    for k, v in args.items():
        if k in props:
            out[k] = v
    for canon, alts in ARG_ALIASES.items():
        if canon in props and canon not in out:
            for a in alts:
                if a in args and a not in props:
                    out[canon] = args[a]
                    break
    # single-required-arg tools: accept whatever single value was given
    req = [r for r in (t["function"]["parameters"]["required"]
                       for t in TOOL_SCHEMAS if t["function"]["name"] == name)][0] \
        if name in _PROPS else []
    if len(req) == 1 and req[0] not in out and len(args) == 1:
        out[req[0]] = next(iter(args.values()))
    clean = {}
    for k, v in out.items():
        cv = _coerce(v, props.get(k, {}))
        if cv is not None:
            clean[k] = cv
    return clean


def loads_lenient(s: str) -> Any:
    s = s.strip()
    if not s:
        return {}
    try:
        return json.loads(s, strict=False)
    except ValueError:
        pass
    # common small-model mistakes: trailing commas, single quotes, python bools
    fixed = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(fixed, strict=False)
    except ValueError:
        pass
    try:
        import ast
        obj = ast.literal_eval(fixed)
        return obj
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def _balanced_objects(text: str) -> list[tuple[int, int]]:
    """Spans of top-level {...} objects (string-aware)."""
    spans, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append((start, i + 1))
    return spans


def _as_call(obj: Any) -> Optional[dict]:
    if not isinstance(obj, dict):
        return None
    if "function" in obj and isinstance(obj["function"], dict):
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name") or obj.get("action") \
        or (obj.get("function") if isinstance(obj.get("function"), str) else None)
    if not isinstance(name, str):
        return None
    canon = canonical_name(name)
    if not canon:
        return None
    args = obj.get("arguments", obj.get("parameters", obj.get("args",
                   obj.get("input", obj.get("action_input")))))
    if args is None:
        args = {k: v for k, v in obj.items()
                if k not in ("name", "tool", "tool_name", "action", "type", "function")}
    return {"id": "call_" + uuid.uuid4().hex[:8],
            "function": {"name": canon, "arguments": normalize_args(canon, args)}}


_TAGGED = re.compile(
    r"<(tool_call|function_call|tool|invoke|tool_use)\b[^>]*>\s*(.*?)\s*</\1>", re.S | re.I)
_FENCED = re.compile(r"```(?:json|tool_call|tool)?\s*\n(.*?)```", re.S)
_XML_FUNC = re.compile(r"<function=([\w.\-]+)>(.*?)(?:</function>|$)", re.S)
_XML_PARAM = re.compile(r"<parameter=([\w\-]+)>\n?(.*?)\n?</parameter>", re.S)
_INVOKE = re.compile(r"<invoke\s+name=[\"']([\w.\-]+)[\"']\s*>(.*?)(?:</invoke>|$)", re.S)
_INVOKE_PARAM = re.compile(r"<parameter\s+name=[\"']([\w\-]+)[\"']\s*>(.*?)</parameter>", re.S)
_ARG_PAIR = re.compile(r"<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>(.*?)</arg_value>", re.S)


def _mk(name: str, args: Any) -> Optional[dict]:
    canon = canonical_name(name or "")
    if not canon:
        return None
    return {"id": "call_" + uuid.uuid4().hex[:8],
            "function": {"name": canon, "arguments": normalize_args(canon, args)}}


def _xmlish_value(v: str) -> Any:
    v2 = v.strip()
    if v2.startswith(("[", "{")):
        parsed = loads_lenient(v2)
        if parsed is not None:
            return parsed
    return v[1:] if v.startswith("\n") else v


def _decode_loose(raw: str) -> str:
    """Decode JSON escapes in a string body without stopping at bare quotes."""
    out, i, n = [], 0, len(raw)
    esc = {"n": "\n", "t": "\t", "r": "", '"': '"', "\\": "\\", "/": "/", "b": "", "f": ""}
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nx = raw[i + 1]
            if nx == "u" and i + 6 <= n:
                try:
                    out.append(chr(int(raw[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            out.append(esc.get(nx, "\\" + nx))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def salvage_json_call(text: str) -> Optional[dict]:
    """Recover a JSON tool call that json can't parse, typically because a
    file's contents contain unescaped quotes."""
    m = re.search(r'"(?:name|tool|tool_name|function)"\s*:\s*"([\w.\-]+)"', text)
    if not m:
        return None
    canon = canonical_name(m.group(1))
    if not canon:
        return None
    props = _PROPS.get(canon, {})
    keys = set(props)
    for canon_key, alts in ARG_ALIASES.items():
        if canon_key in props:
            keys.update(alts)
    starts = []
    for k in keys:
        for fm in re.finditer(r'"%s"\s*:\s*' % re.escape(k), text):
            starts.append((fm.start(), fm.end(), k))
    starts.sort()
    args: dict = {}
    for i, (s0, s1, k) in enumerate(starts):
        nxt = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        chunk = text[s1:nxt]
        if chunk.startswith('"'):
            body = chunk[1:]
            if i + 1 < len(starts):
                body = re.sub(r'"\s*,\s*$', "", body.rstrip())
            else:
                body = re.sub(r'"\s*}\s*}?\s*(?:</tool_call>)?\s*$', "", body.rstrip())
            args[k] = _decode_loose(body)
        else:
            vm = re.match(r"\s*(true|false|-?\d+)", chunk)
            if vm:
                args[k] = vm.group(1)
    if not args:
        return None
    return _mk(canon, args)


def parse_call_text(body: str) -> Optional[dict]:
    """Parse one tool call written in any common text format."""
    body = body.strip()
    if not body:
        return None
    # 1. JSON object(s)
    for s, e in _balanced_objects(body):
        obj = loads_lenient(body[s:e])
        c = _as_call(obj)
        if c:
            return c
    # 2. <function=name><parameter=k>v</parameter></function>
    fm = _XML_FUNC.search(body)
    if fm:
        args = {k: _xmlish_value(v) for k, v in _XML_PARAM.findall(fm.group(2))}
        return _mk(fm.group(1), args)
    # 3. <invoke name="x"><parameter name="k">v</parameter></invoke>
    im = _INVOKE.search(body)
    if im:
        args = {k: _xmlish_value(v) for k, v in _INVOKE_PARAM.findall(im.group(2))}
        return _mk(im.group(1), args)
    # 4. name on the first line, then <arg_key>/<arg_value> pairs or JSON args
    first, _, rest = body.partition("\n")
    head = re.match(r"\s*([\w.\-]+)\s*(\(|$|\{)", first)
    if head and canonical_name(head.group(1)):
        pairs = _ARG_PAIR.findall(body)
        if pairs:
            return _mk(head.group(1), {k: _xmlish_value(v) for k, v in pairs})
        tail = body[head.end(1):].strip().lstrip("(").rstrip(")")
        for s, e in _balanced_objects(tail):
            obj = loads_lenient(tail[s:e])
            if isinstance(obj, dict):
                return _mk(head.group(1), obj)
    # 5. broken JSON
    if "{" in body:
        return salvage_json_call(body)
    return None


def extract_text_tool_calls(content: str) -> tuple[list[dict], str]:
    """Recover tool calls the model wrote as text. Returns (calls, remaining_text)."""
    if not content or not re.search(r"[{<]", content):
        return [], content
    if content.count("<tool_call>") > content.count("</tool_call>"):
        content = content.rstrip() + "\n</tool_call>"
    calls: list[dict] = []
    remaining = content

    for m in list(_TAGGED.finditer(content)):
        c = parse_call_text(m.group(2) if m.group(1).lower() != "invoke" else m.group(0))
        if c:
            calls.append(c)
            remaining = remaining.replace(m.group(0), "")
    if calls:
        return calls, remaining.strip()

    for rx in (_XML_FUNC, _INVOKE):
        for m in list(rx.finditer(content)):
            c = parse_call_text(m.group(0))
            if c:
                calls.append(c)
                remaining = remaining.replace(m.group(0), "")
        if calls:
            return calls, remaining.strip()

    for m in list(_FENCED.finditer(content)):
        body = m.group(1)
        obj = loads_lenient(body)
        objs = obj if isinstance(obj, list) else [obj]
        got = [c for c in (_as_call(o) for o in objs) if c]
        if got:
            calls.extend(got)
            remaining = remaining.replace(m.group(0), "")
    if calls:
        return calls, remaining.strip()

    # bare JSON object(s) that look like a tool call
    stripped = content.strip()
    for s, e in _balanced_objects(stripped):
        frag = stripped[s:e]
        if '"name"' not in frag and "'name'" not in frag and '"tool"' not in frag:
            continue
        c = _as_call(loads_lenient(frag))
        if c:
            calls.append(c)
            remaining = remaining.replace(frag, "")
    return calls, remaining.strip()


def normalize_native_calls(raw: list[dict]) -> tuple[list[dict], list[str]]:
    """Normalize Ollama tool_calls. Returns (valid_calls, error_messages)."""
    calls, errors = [], []
    for c in raw or []:
        fn = (c or {}).get("function") or {}
        name = fn.get("name") or ""
        canon = canonical_name(name)
        if not canon:
            errors.append(f"unknown tool '{name}'. Available tools: {', '.join(TOOL_NAMES)}")
            continue
        calls.append({"id": c.get("id") or "call_" + uuid.uuid4().hex[:8],
                      "function": {"name": canon,
                                   "arguments": normalize_args(canon, fn.get("arguments"))}})
    return calls, errors


def missing_required(name: str, args: dict) -> list[str]:
    for t in TOOL_SCHEMAS:
        if t["function"]["name"] == name:
            return [r for r in t["function"]["parameters"]["required"] if r not in args]
    return []


class RepetitionGuard:
    """Detects degenerate looping in streamed text (a common failure mode of
    small models): the same chunk repeated many times at the tail."""

    def __init__(self, window: int = 3000):
        self.buf = ""
        self.window = window
        self._since = 0

    def feed(self, text: str) -> bool:
        self.buf = (self.buf + text)[-self.window:]
        self._since += len(text)
        if self._since < 120 or len(self.buf) < 600:
            return False
        self._since = 0
        return looks_repetitive(self.buf)


def looks_repetitive(buf: str) -> bool:
    """True when the tail of buf is the same chunk repeated back-to-back."""
    tail = buf[-2400:]
    n = len(tail)
    for period in range(8, 401):
        reps = max(4, -(-600 // period))  # cover at least ~600 chars
        if period * reps > n:
            break
        unit = tail[-period:]
        if not unit.strip():
            continue
        if tail.endswith(unit * reps):
            return True
    lines = [l.strip() for l in tail.splitlines() if l.strip()]
    if len(lines) >= 10 and len(set(lines[-10:])) == 1 and len(lines[-1]) > 6:
        return True
    return False


# ----------------------------------------------------------- streaming mode
# In "stream" tool mode the model writes calls as text, so we can show them
# while they are being generated (Ollama holds native tool calls back).

TOOL_RESPONSE_TAG = "<tool_response"


def is_user_turn(m: dict) -> bool:
    """A real user request (not a tool result or a tinycode-injected note)."""
    if m.get("role") != "user" or is_tool_result(m):
        return False
    c = str(m.get("content", ""))
    return not c.startswith(("[automatic check]", "[system note]", "[I ran a shell command",
                             "[The user reverted", "[Context note", "[Summary of our"))


def is_tool_result(m: dict) -> bool:
    return m.get("role") == "tool" or (
        m.get("role") == "user" and str(m.get("content", "")).startswith(TOOL_RESPONSE_TAG))


_CALL_START = re.compile(
    r"<tool_call>|<function=[\w.\-]+>|<invoke\s+name=|"
    r"^\s*(?:```(?:json)?\s*)?\{\s*\"(?:name|tool|function)\"\s*:", re.M)
_CALL_END = re.compile(r"</tool_call>|</function>|</invoke>")


def tool_call_start(text: str) -> int:
    """Index where a text tool call begins, or -1."""
    m = _CALL_START.search(text)
    return m.start() if m else -1


def call_complete(text: str, start: int) -> bool:
    """True once the tool call that begins at `start` is finished."""
    body = text[start:]
    if _CALL_END.search(body):
        return True
    brace = body.find("{")
    if brace < 0:
        return False
    spans = _balanced_objects(body[brace:])
    if not spans or spans[0][0] != 0:
        return False
    # only trust a closed brace if it really parses (unescaped quotes in a
    # file's contents can fool the brace counter)
    try:
        json.loads(body[brace:brace + spans[0][1]], strict=False)
        return True
    except ValueError:
        return False


def _decode_partial(raw: str) -> str:
    """Decode a JSON string body that may be cut off mid-way."""
    out = []
    i = 0
    n = len(raw)
    esc = {"n": "\n", "t": "\t", "r": "", '"': '"', "\\": "\\", "/": "/", "b": "", "f": ""}
    while i < n:
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= n:
                break
            nx = raw[i + 1]
            if nx == "u":
                if i + 6 > n:
                    break
                try:
                    out.append(chr(int(raw[i + 2:i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            out.append(esc.get(nx, nx))
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    return "".join(out)


_STR_FIELD = r'"%s"\s*:\s*"'


def partial_call(text: str) -> tuple[str, dict]:
    """Best-effort (name, args) from an unfinished text tool call."""
    start = tool_call_start(text)
    if start < 0:
        return "", {}
    body = text[start:]
    name = ""
    for rx in (r'"(?:name|tool|tool_name|function)"\s*:\s*"([^"]+)"', r"<function=([\w.\-]+)>",
               r"<invoke\s+name=[\"']([\w.\-]+)", r"<tool_call>\s*([\w.\-]+)\s*(?:\n|\(|\{|<)"):
        m = re.search(rx, body)
        if m:
            name = m.group(1)
            break
    name = canonical_name(name) or name
    args: dict = {}
    fields = (("path", ("path", "file_path", "file", "filename")),
              ("command", ("command", "cmd")),
              ("pattern", ("pattern",)), ("url", ("url",)),
              ("content", ("content", "contents", "text")),
              ("old_string", ("old_string", "old")),
              ("new_string", ("new_string", "new")))
    for key, aliases in fields:
        for a in aliases:
            fm = re.search(_STR_FIELD % re.escape(a), body)
            if fm:
                args[key] = _decode_partial(body[fm.end():])
                break
            xm = re.search(r"<(?:parameter=%s|parameter\s+name=[\"']%s[\"']|arg_value)>\n?"
                           % (re.escape(a), re.escape(a)), body) if a != "text" else None
            if xm and (a in xm.group(0)):
                val = body[xm.end():]
                end = val.find("</parameter>")
                args[key] = val if end < 0 else val[:end]
                break
    return name or "", args


def unknown_tool_name(text: str) -> str:
    """The name in a well-formed text tool call that isn't a known tool."""
    m = re.search(r"<function=([\w.\-]+)>|<invoke\s+name=[\"']([\w.\-]+)", text)
    if m:
        n = m.group(1) or m.group(2)
        return "" if canonical_name(n) else n
    for a, b in _balanced_objects(text):
        obj = loads_lenient(text[a:b])
        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            return "" if canonical_name(obj["name"]) else obj["name"]
    return ""
