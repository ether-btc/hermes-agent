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


def test_run_local_turn_non_finite_config_falls_back_to_default(tmp_path, capsys, monkeypatch):
    """
    A non-finite config value (.inf / .nan) falls back to the default bound.
    Monkeypatch the default to a SMALL value so the bound is observable:
    a sleeper longer than the patched default MUST be killed iff the fallback occurred.
    """
    import tools.bot_mode_dm as bmd

    # Patch the module-level default to a SMALL observable value (0.3s)
    monkeypatch.setattr(bmd, "_TURN_MAX_SECONDS_DEFAULT", 0.3)

    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("test message", encoding="utf-8")

    # Sleeper exceeds the patched default (0.3s) — must be killed if fallback happened
    sleeper = _make_sleeper_script(2.0, tmp_path)

    home = tmp_path / "hermes-home"
    # Test both .inf and .nan as non-finite values
    for non_finite in (".inf", ".nan"):
        _write_config(home, f"bot_mode:\n  turn_max_seconds: {non_finite}\n")

        monkeypatch.setenv("HERMES_HOME", str(home))

        argv = [sys.executable, str(sleeper)]
        rc = _run_local_turn(argv, str(dm_file), env=os.environ.copy())

        captured = capsys.readouterr()

        # Child must be killed by the fallback bound
        assert rc != 0, f"Expected non-zero rc for {non_finite}, got {rc}: stderr={captured.err}"
        # Exact timeout marker from production code (line 463 in bot_mode_dm.py)
        assert "hermes-delivery-timeout: turn exceeded 0s" in captured.err, (
            f"Missing timeout marker for {non_finite}: stderr={captured.err}"
        )
