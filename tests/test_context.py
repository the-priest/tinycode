from tinycode.context import (PRUNED, build_system_prompt, estimate_tokens, expand_mentions,
                              prune)


def test_system_prompt_has_env_and_memory(project):
    (project / "TINYCODE.md").write_text("Always use tabs.")
    prompt, used = build_system_prompt(project)
    assert str(project) in prompt and "calc.py" in prompt
    assert "Always use tabs." in prompt and used


def test_prune_keeps_recent_and_system():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(30):
        msgs.append({"role": "user", "content": f"q{i}"})
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]})
        msgs.append({"role": "tool", "content": "x" * 4000})
    out, n = prune(msgs, 6000)
    assert n > 0 and out[0]["content"] == "sys"
    assert estimate_tokens(out) <= 6000 or all(m["content"] == PRUNED for m in out
                                               if m["role"] == "tool")
    assert out[-1]["content"] == "x" * 4000


def test_mentions(project):
    text, att = expand_mentions("fix @calc.py please, mail me@example.com", project)
    assert att == ["calc.py"] and '<file path="calc.py">' in text
    assert "example.com" not in "".join(att)
