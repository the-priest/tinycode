import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tinycode.config import Config
from tinycode.ollama import Ollama

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux only")
TESTS = Path(__file__).parent


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    exe = b / "ollama"
    exe.write_text(f"#!{sys.executable}\nimport runpy, sys\nsys.path.insert(0, {str(TESTS)!r})\n"
                   f"runpy.run_path({str(TESTS / 'fake_ollama.py')!r}, run_name='__main__')\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    return exe


def test_spawn_and_stop(fake_bin):
    port = free_port()
    cfg = Config(host=f"127.0.0.1:{port}", model="fake:latest")
    o = Ollama(cfg)
    assert o.begin_server() is True
    o.wait_up(timeout=15)
    assert o.alive() and o.spawned and o.owned
    o.stop_server()
    time.sleep(0.3)
    assert not port_open(port)


def test_server_dies_with_tinycode_on_sigkill(fake_bin):
    port = free_port()
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
        from tinycode.config import Config
        from tinycode.ollama import Ollama
        o = Ollama(Config(host="127.0.0.1:{port}"))
        o.begin_server(); o.wait_up(timeout=15)
        print("up", flush=True)
        time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "up"
    assert port_open(port)
    p.kill()           # SIGKILL: no cleanup code can run
    p.wait()
    for _ in range(50):
        if not port_open(port):
            break
        time.sleep(0.1)
    assert not port_open(port), "server outlived tinycode"


def test_adopts_running_server_without_killing_foreign(fake_bin):
    from fake_ollama import FakeOllama
    with FakeOllama() as fake:
        o = Ollama(Config(host=fake.host))
        assert o.begin_server() is False
        assert not o.spawned
        o.stop_server()          # nothing of ours to stop
        assert o.alive()
