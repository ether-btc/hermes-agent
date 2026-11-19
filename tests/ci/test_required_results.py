"""Behavior tests for the strict CI aggregate gate (scripts/ci/required_results.py)."""

import importlib.util
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "required_results.py"

_spec = importlib.util.spec_from_file_location("required_results", SCRIPT)
required_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(required_results)

evaluate_gate = required_results.evaluate_gate


import pytest


@pytest.mark.parametrize("release", [False, True])
@pytest.mark.parametrize("job", ["tests", "osv-scanner", "history-check", "e2e-desktop"])
@pytest.mark.parametrize("result,ordinary,excluded", [
    ("success", True, True), ("skipped", False, True), ("failure", False, False),
    ("cancelled", False, False), ("with-status", False, False),
    ("action_required", False, False), ("unknown", False, False), (None, False, False),
])
def test_status_policy(job, result, ordinary, excluded, release):
    expected = excluded if job in ("history-check", "e2e-desktop") else ordinary
    if result == "skipped" and not release:
        expected = True
    verdict = evaluate_gate({job: {} if result is None else {"result": result}}, release=release)
    assert verdict == {"ok": expected, "failed": [] if expected else [job],
                       "allowed_skips": [job] if expected and result == "skipped" else [],
                       "unallocated": []}


def test_release_exclusion_policy():
    assert required_results.EXCLUDED_JOBS == {
        "history-check", "lockfile-diff", "supply-chain", "e2e-desktop",
    }


@pytest.mark.parametrize("needs", [{}, None])
@pytest.mark.parametrize("release", [False, True])
def test_empty_needs_fails_closed(needs, release):
    assert evaluate_gate(needs, release=release) == {
        "ok": False, "failed": ["<no-needs>"], "allowed_skips": [], "unallocated": [],
    }
    assert required_results.compact_results(needs) == {}


@pytest.mark.parametrize("release,results,code,report", [
    (True, {"detect": "success", "e2e-desktop": "skipped"}, 0, "All checks passed"),
    (True, {"tests": "skipped", "history-check": "skipped", "lint": "failure"},
     1, "::error::2 job(s) failed: lint, tests"),
    (False, {"tests": "failure"}, 1, "::error::1 job(s) failed: tests"),
])
def test_cli_stdin_exit_codes_and_output(tmp_path, release, results, code, report):
    output = tmp_path / "github-output.txt"
    needs = {name: {"result": result, "outputs": {"detail": "kept"}}
             for name, result in results.items()}
    child = subprocess.run(
        [sys.executable, str(SCRIPT), *(["--release"] if release else [])],
        input=json.dumps(needs), capture_output=True, text=True, timeout=30,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
    )
    assert child.returncode == code, child.stderr
    assert report in child.stdout
    key, value = output.read_text(encoding="utf-8").strip().split("=", 1)
    assert key == "needs-json"
    assert json.loads(value) == results
    assert f"{key}={value}" in child.stdout
    for name, result in results.items():
        assert f"{name}: {result}" in child.stdout


# ── --allow-unallocated: "never ran" is not "failed" ──────────────────────────
# Measured 2026-11-19: lanes pin GitHub larger hosted runners this account cannot
# allocate, so GitHub queues them for its 24 h maximum and cancels them with
# steps == [] and runner_name == "". Those jobs never executed.

_UNALLOC_NEEDS = {
    "detect": {"result": "success", "outputs": {}},
    "tests": {"result": "cancelled"},
    "tests-os": {"result": "cancelled"},
    "lint": {"result": "success"},
}


def _job(conclusion="cancelled", *, runner="", steps=(), labels=("ubuntu-latest-32-core",)):
    return {"name": "job", "conclusion": conclusion, "runner_name": runner,
            "steps": list(steps), "labels": list(labels)}


def _jobs_fetch(jobs, total=None):
    calls = []

    def fetch(path):
        calls.append(path)
        if not path.startswith("repos/o/r/actions/runs/1/jobs"):
            raise AssertionError(f"unexpected API call {path}")
        params = dict(urllib.parse.parse_qsl(path.partition("?")[2]))
        size, page = int(params["per_page"]), int(params["page"])
        return {"total_count": len(jobs) if total is None else total,
                "jobs": jobs[(page - 1) * size:page * size]}
    return fetch, calls


def _allocated_jobs(cancelled=2, executed=2, **over):
    jobs = [_job(conclusion="success", runner="GitHub Actions 1", steps=[{"name": "run"}],
                 labels=("ubuntu-latest",)) for _ in range(executed)]
    jobs += [_job(**over) for _ in range(cancelled)]
    return jobs


def _run(monkeypatch, capsys, needs, fetch, argv=("--allow-unallocated",)):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(needs)))
    code = required_results.main(list(argv), fetch=fetch)
    return code, capsys.readouterr().out


def test_t1_unallocated_cancels_are_non_blocking_with_evidence(monkeypatch, capsys):
    fetch, calls = _jobs_fetch(_allocated_jobs())
    code, out = _run(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 0, out
    assert "::warning::Unallocated jobs (no runner assigned, non-blocking): tests, tests-os" in out
    assert "🕳️ tests: unallocated (no runner assigned) — non-blocking" in out
    assert "::error::" not in out
    assert len(calls) == 1


def test_t1b_negative_control_without_the_flag_still_fails(monkeypatch, capsys):
    fetch, calls = _jobs_fetch(_allocated_jobs())
    code, out = _run(monkeypatch, capsys, _UNALLOC_NEEDS, fetch, argv=())
    assert code == 1
    assert "::error::2 job(s) failed: tests, tests-os" in out
    assert calls == []


def test_t2_release_refuses_the_flag(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, capsys, _UNALLOC_NEEDS, lambda path: None,
             argv=("--release", "--allow-unallocated"))
    assert exc.value.code == 2


def test_t4_real_failures_never_consult_the_api(monkeypatch, capsys):
    needs = {**_UNALLOC_NEEDS, "tests": {"result": "failure"}}
    code, out = _run(monkeypatch, capsys, needs, _raise)
    assert code == 1
    assert "::error::2 job(s) failed: tests, tests-os" in out


def _raise(path):
    raise AssertionError(f"unexpected API call {path}")


@pytest.mark.parametrize("fetch", [
    lambda path: (_ for _ in ()).throw(OSError("api down")),
    lambda path: None,
    lambda path: [],
    lambda path: {"total_count": 4},
    lambda path: {"total_count": 99, "jobs": []},
])
def test_t5_lookup_faults_fail_closed(monkeypatch, capsys, fetch):
    code, out = _run(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "keeping the failure" in out or "Not downgrading" in out
    assert "::error::" in out


def test_t6_one_executed_cancel_blocks_the_whole_downgrade(monkeypatch, capsys):
    jobs = _allocated_jobs() + [_job(runner="GitHub Actions 9", steps=[{"name": "run"}])]
    fetch, _ = _jobs_fetch(jobs)
    code, out = _run(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "Not downgrading to unallocated" in out


def test_t10_zero_execution_run_is_not_waived(monkeypatch, capsys):
    fetch, _ = _jobs_fetch([_job() for _ in range(4)])
    code, out = _run(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "executed=0" in out


def test_t11_incomplete_job_list_is_not_evidence(monkeypatch, capsys):
    fetch, _ = _jobs_fetch(_allocated_jobs(), total=99)
    code, out = _run(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "truncated=True" in out


def test_t12_label_outside_the_pinned_set_blocks_the_downgrade(monkeypatch, capsys):
    fetch, _ = _jobs_fetch(_allocated_jobs(labels=("macos-latest",)))
    code, out = _run(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "labels_ok=False" in out


def test_t13_extra_labels_block_the_downgrade(monkeypatch, capsys):
    fetch, _ = _jobs_fetch(_allocated_jobs(labels=("self-hosted", "ubuntu-latest-32-core")))
    code, _ = _run(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1


def test_t14_later_page_is_read_and_counted(monkeypatch, capsys):
    fetch, calls = _jobs_fetch(_allocated_jobs(cancelled=150))
    code, out = _run(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 0, out
    assert len(calls) == 2
    assert "150/150 cancelled jobs never ran" in out


def test_t15_successful_gate_never_calls_the_api(monkeypatch, capsys):
    needs = {**_UNALLOC_NEEDS, "tests": {"result": "success"}, "tests-os": {"result": "success"}}
    code, out = _run(monkeypatch, capsys, needs, _raise)
    assert code == 0
    assert "All checks passed" in out


def test_t16_no_waiver_when_detect_never_ran(monkeypatch, capsys):
    needs = {**_UNALLOC_NEEDS, "detect": {"result": "cancelled", "outputs": {}}}
    fetch, calls = _jobs_fetch(_allocated_jobs())
    code, out = _run(monkeypatch, capsys, needs, fetch)
    assert code == 1
    assert calls == []


@pytest.mark.parametrize("env", ["GITHUB_RUN_ID", "GITHUB_REPOSITORY"])
def test_t19_missing_run_identity_fails_closed(monkeypatch, capsys, env):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(_UNALLOC_NEEDS)))
    code = required_results.main(["--allow-unallocated"], fetch=_raise)
    out = capsys.readouterr().out
    assert code == 1
    assert "keeping the failure" in out or "Not downgrading" in out
