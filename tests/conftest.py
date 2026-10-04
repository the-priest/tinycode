import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep sessions/config/logs out of the real home directory."""
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    import tinycode.config as c
    monkeypatch.setattr(c, "CONFIG_DIR", tmp_path / "cfg" / "tinycode")
    monkeypatch.setattr(c, "CONFIG_PATH", tmp_path / "cfg" / "tinycode" / "config.toml")
    monkeypatch.setattr(c, "DATA_DIR", tmp_path / "data" / "tinycode")
    monkeypatch.setattr(c, "CACHE_DIR", tmp_path / "cache" / "tinycode")
    yield


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    print(add(2, 3))\n")
    (d / "README.md").write_text("# calc\nA tiny calculator.\n")
    return d
