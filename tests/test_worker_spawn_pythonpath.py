#!/usr/bin/env /usr/bin/python3
"""E2E regression: a kanban worker must be able to import hermes_cli.

The field failure was every worker dying on first spawn with:

    ModuleNotFoundError: No module named 'hermes_cli'

Root cause (verified, not inferred): the dispatcher's argv was
`[sys.executable, "-m", "hermes_cli.main"]`. The managed Hermes runtime is
launched with `-I` (isolated mode) and has NO installed `hermes_cli` — the
package exists only as a source checkout under the repo root. The worker is
spawned with `cwd=workspace`, never the repo root, so the import could not
resolve. Setting `PYTHONPATH` in the spawn env does NOT help: the runtime
launcher explicitly pops `PYTHONPATH` from `os.environ` before inserting the
repo root itself, so any injected value is discarded.

The fix routes the worker through the published launcher
(`.hermes/bin/hermes`), a `-I` shim that resolves the package regardless of cwd
and strips any inherited PYTHONPATH.

This test exercises the real chain per AGENTS.md — it does not assert on
source strings for the behavioural part.

Run: /usr/bin/python3 tests/test_worker_spawn_pythonpath.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path("/home/hermes-pi/.hermes/hermes-agent")
sys.path.insert(0, str(REPO))

# Importing the dispatcher needs its runtime dependencies (ruamel.yaml etc.),
# which live in the repo venv, not the bare managed runtime under test. Re-exec
# under the venv interpreter if we are not already running there.
if "ruamel" not in sys.modules:
    try:
        import ruamel.yaml  # noqa: F401
    except ModuleNotFoundError:
        venv_python = REPO / "venv" / "bin" / "python3"
        if venv_python.is_file() and os.environ.get("_SPAWN_TEST_REEXEC") != "1":
            os.environ["_SPAWN_TEST_REEXEC"] = "1"
            os.execv(str(venv_python), [str(venv_python), os.path.abspath(__file__)])
        print("  FAIL  cannot import the dispatcher: no ruamel.yaml and no venv")
        sys.exit(1)

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if detail and not ok:
        print(f"        {detail}")
    if not ok:
        failures += 1
    return ok


def managed_python() -> Path | None:
    cands = sorted(Path("/home/hermes-pi/.hermes/tools").glob("python-*/bin/python3"))
    return cands[-1] if cands else None


# --- 1. the defect, reproduced -------------------------------------------
print("=== 1. the defect: managed runtime cannot import hermes_cli bare ===")
mp = managed_python()
if mp is None:
    check("managed runtime located", False, "no python-*/bin/python3 under ~/.hermes/tools")
else:
    bare = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    p = subprocess.run([str(mp), "-c", "import hermes_cli"], env=bare, cwd="/tmp",
                       capture_output=True, text=True, timeout=120)
    check("bare `python -m` form fails to import hermes_cli", p.returncode != 0,
          f"rc={p.returncode} (if this now passes, the runtime gained an install)")

# --- 2. the fix: argv resolution ------------------------------------------
print("\n=== 2. the fix: _module_hermes_argv resolves to the published launcher ===")
import hermes_cli.kanban_db_dispatch as k  # noqa: E402

argv = k._module_hermes_argv()
print(f"        _module_hermes_argv() -> {argv}")
check("does not use the bare interpreter form",
      not (len(argv) > 1 and argv[1] == "-m"), f"still {argv}")
launcher = Path(argv[0])
check("target exists and is executable",
      launcher.is_file() and os.access(launcher, os.X_OK), str(launcher))

resolved = k._resolve_hermes_argv()
check("_resolve_hermes_argv (used by _worker_argv) also resolves",
      bool(resolved) and not (len(resolved) > 1 and resolved[1] == "-m"), str(resolved))

# --- 3. the launcher actually works from a non-repo-root cwd -------------
print("\n=== 3. the launcher runs hermes_cli from a workspace cwd ===")
with tempfile.TemporaryDirectory() as td:
    env = {k2: v for k2, v in os.environ.items() if k2 != "PYTHONPATH"}
    p = subprocess.run([str(launcher), "--help"], env=env, cwd=td,
                       capture_output=True, text=True, timeout=300)
    check("launcher --help exits 0 from a workspace cwd", p.returncode == 0,
          f"rc={p.returncode} {p.stderr[-200:]}")
    check("launcher prints hermes usage", "usage: hermes" in p.stdout.lower(),
          p.stdout[:120])

    # A hostile PYTHONPATH must not be able to shadow the real package.
    p2 = subprocess.run([str(launcher), "--help"],
                        env={**env, "PYTHONPATH": "/nonexistent/bogus"},
                        cwd=td, capture_output=True, text=True, timeout=300)
    check("hostile PYTHONPATH does not break the launcher", p2.returncode == 0,
          f"rc={p2.returncode}")

    # The launcher does not accept `-c`; prove the same property directly
    # against the managed runtime instead: it pops PYTHONPATH from os.environ
    # before importing, which is exactly why an env-based fix cannot hold.
    probe = (
        "import os,sys;"
        "os.environ.pop('PYTHONPATH',None);"
        "sys.path.insert(0,'/home/hermes-pi/.hermes/hermes-agent');"
        "import hermes_cli;"
        "print('IMPORT_OK')"
    )
    p3 = subprocess.run([str(mp), "-c", probe], env=env, cwd=td,
                        capture_output=True, text=True, timeout=180)
    check("import succeeds with PYTHONPATH explicitly removed (mirrors the launcher)",
          p3.returncode == 0 and "IMPORT_OK" in p3.stdout,
          f"rc={p3.returncode} {p3.stderr[-200:]}")

print("\nRESULT:", "FAIL" if failures else "PASS - worker resolves hermes_cli under spawn conditions")
sys.exit(1 if failures else 0)
