from tinycode.parsing import (RepetitionGuard, canonical_name, extract_text_tool_calls,
                              looks_repetitive, normalize_args, normalize_native_calls)


def test_aliases():
    assert canonical_name("run_bash") == "bash"
    assert canonical_name("functions.read_file") == "read_file"
    assert canonical_name("str_replace") == "edit_file"
    assert canonical_name("nonsense") is None


def test_arg_aliases_and_types():
    a = normalize_args("read_file", {"file_path": "x.py", "offset": "10", "limit": 5})
    assert a == {"path": "x.py", "offset": 10, "limit": 5}
    a = normalize_args("edit_file", '{"file": "a", "old": "x", "new": "y", "replace_all": "true"}')
    assert a == {"path": "a", "old_string": "x", "new_string": "y", "replace_all": True}
    a = normalize_args("bash", {"cmd": "ls"})
    assert a == {"command": "ls"}
    # single required arg given under an odd name
    assert normalize_args("fetch_url", {"website": "x.com"}) == {"url": "x.com"}


def test_native_calls():
    calls, errs = normalize_native_calls([
        {"function": {"name": "read", "arguments": {"filename": "a.py"}}},
        {"function": {"name": "teleport", "arguments": {}}},
    ])
    assert calls[0]["function"] == {"name": "read_file", "arguments": {"path": "a.py"}}
    assert len(errs) == 1 and "teleport" in errs[0]


def test_text_tool_call_tagged():
    calls, rest = extract_text_tool_calls(
        'Let me look.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>')
    assert calls[0]["function"]["name"] == "read_file"
    assert rest == "Let me look."


def test_text_tool_call_fenced_and_lenient():
    calls, _ = extract_text_tool_calls(
        "```json\n{'name': 'bash', 'arguments': {'command': 'pytest -q',}}\n```")
    assert calls[0]["function"]["arguments"] == {"command": "pytest -q"}


def test_plain_json_not_a_tool_is_kept():
    text = 'Here is config: {"name": "myapp", "version": 2}'
    calls, rest = extract_text_tool_calls(text)
    assert calls == [] and rest == text


def test_repetition():
    assert looks_repetitive("I should check the file. " * 40)
    assert not looks_repetitive("def a():\n    return 1\n" + "".join(
        f"x{i} = compute({i})\n" for i in range(80)))
    g = RepetitionGuard()
    hit = False
    for _ in range(200):
        hit = hit or g.feed("wait, let me re-check. ")
    assert hit
