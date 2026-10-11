"""Tests for the delivery-turn timeout in tools/bot_mode_dm.py.

The F8 fix adds a bounded timeout to _run_local_turn so a wedged child returns
instead of blocking forever (and releasing the target profile's turn lock).
This test exercises the timeout path with a short configured bound.
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.bot_mode_dm import _run_local_turn


def _make_sleeper_script(seconds: float, tmp_path: Path) -> Path:
    """Create a Python script that sleeps for the given seconds."""
    script = tmp_path / "sleeper.py"
    script.write_text(
        f"""#!/usr/bin/env python3
import time, sys
time.sleep({seconds})
print("sleeper done")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _write_config(home: Path, content: str):
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(content, encoding="utf-8")


def test_run_local_turn_times_out_with_short_bound(tmp_path, capsys, monkeypatch):
    """A delivery turn that exceeds the configured timeout returns rc=1 with a timeout marker."""
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("test message", encoding="utf-8")

    sleeper = _make_sleeper_script(5.0, tmp_path)

    home = tmp_path / "hermes-home"
    _write_config(home, "bot_mode:\n  turn_max_seconds: 1\n")

    # Set HERMES_HOME in the process environment so load_config() picks it up
    monkeypatch.setenv("HERMES_HOME", str(home))

    argv = [sys.executable, str(sleeper)]
    rc = _run_local_turn(argv, str(dm_file), env=os.environ.copy())

    captured = capsys.readouterr()

    assert rc == 1, f"Expected rc=1, got {rc}: stdout={captured.out} stderr={captured.err}"
    assert "hermes-delivery-timeout: turn exceeded" in captured.err
    assert "1s" in captured.err or "1 s" in captured.err


def test_run_local_turn_succeeds_within_timeout(tmp_path, capsys, monkeypatch):
    """A delivery turn that completes within the timeout returns rc=0."""
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("test message", encoding="utf-8")

    sleeper = _make_sleeper_script(0.1, tmp_path)

    home = tmp_path / "hermes-home"
    _write_config(home, "bot_mode:\n  turn_max_seconds: 10\n")

    monkeypatch.setenv("HERMES_HOME", str(home))

    argv = [sys.executable, str(sleeper)]
    rc = _run_local_turn(argv, str(dm_file), env=os.environ.copy())

    captured = capsys.readouterr()

    assert rc == 0, f"Expected rc=0, got {rc}: stderr={captured.err}"
    assert "sleeper done" in captured.out
    assert "hermes-delivery-timeout" not in captured.err


def test_run_local_turn_bad_config_falls_back_to_default(tmp_path, capsys, monkeypatch):
    """An invalid/negative config value falls back to the default (fail-closed)."""
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("test message", encoding="utf-8")

    sleeper = _make_sleeper_script(0.1, tmp_path)

    home = tmp_path / "hermes-home"
    _write_config(home, "bot_mode:\n  turn_max_seconds: -5\n")

    monkeypatch.setenv("HERMES_HOME", str(home))

    argv = [sys.executable, str(sleeper)]
    rc = _run_local_turn(argv, str(dm_file), env=os.environ.copy())

    captured = capsys.readouterr()

    assert rc == 0, f"Expected rc=0, got {rc}: stderr={captured.err}"
    assert "sleeper done" in captured.out


def test_run_local_turn_missing_config_uses_default(tmp_path, capsys, monkeypatch):
    """Missing config uses the default timeout (30 min)."""
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("test message", encoding="utf-8")

    sleeper = _make_sleeper_script(0.1, tmp_path)

    home = tmp_path / "hermes-home"
    _write_config(home, "")

    monkeypatch.setenv("HERMES_HOME", str(home))

    argv = [sys.executable, str(sleeper)]
    rc = _run_local_turn(argv, str(dm_file), env=os.environ.copy())

    captured = capsys.readouterr()

    assert rc == 0, f"Expected rc=0, got {rc}: stderr={captured.err}"
    assert "sleeper done" in captured.out


def test_run_local_turn_zero_config_uses_default(tmp_path, capsys, monkeypatch):
    """Config value of 0 uses the default timeout (fail-closed, bound can never be disabled)."""
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("test message", encoding="utf-8")

    sleeper = _make_sleeper_script(0.1, tmp_path)

    home = tmp_path / "hermes-home"
    _write_config(home, "bot_mode:\n  turn_max_seconds: 0\n")

    monkeypatch.setenv("HERMES_HOME", str(home))

    argv = [sys.executable, str(sleeper)]
    rc = _run_local_turn(argv, str(dm_file), env=os.environ.copy())

    captured = capsys.readouterr()

    assert rc == 0, f"Expected rc=0, got {rc}: stderr={captured.err}"
    assert "sleeper done" in captured.out
