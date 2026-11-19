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
@pytest.mark.parametrize("job", ["tests", "osv-scanner", "repo-guards", "e2e-desktop"])
@pytest.mark.parametrize("result,ordinary,excluded", [
    ("success", True, True), ("skipped", False, True), ("failure", False, False),
    ("cancelled", False, False), ("with-status", False, False),
    ("action_required", False, False), ("unknown", False, False), (None, False, False),
])
def test_status_policy(job, result, ordinary, excluded, release):
    expected = excluded if job == "e2e-desktop" else ordinary
    if result == "skipped" and not release:
        expected = True
    verdict = evaluate_gate({job: {} if result is None else {"result": result}}, release=release)
    assert verdict == {"ok": expected, "failed": [] if expected else [job],
                       "allowed_skips": [job] if expected and result == "skipped" else [],
                       "unallocated": []}


@pytest.mark.parametrize("lane,jobs", [
    ("python", ("tests", "tests-os")),
    ("e2e", ("tests", "tests-os")),
    ("e2e_upgrade", ("tests", "tests-os")),
    ("desktop_updater", ("tests-os",)),
    ("e2e_desktop_update", ("e2e-desktop-update",)),
    ("remote_backends", ("remote-backends",)),
    ("catalog", ("catalog-tests",)),
])
@pytest.mark.parametrize("result", ["skipped", "absent", "success"])
def test_selected_update_consumers_must_succeed(lane, jobs, result):
    needs = {"detect": {"result": "success", "outputs": {lane: "true"}}}
    if result != "absent":
        needs.update({job: {"result": result} for job in jobs})
    verdict = evaluate_gate(needs)
    assert verdict["ok"] is (result == "success")
    assert verdict["failed"] == ([] if result == "success" else sorted(jobs))
    assert verdict["allowed_skips"] == []
    # False string outputs must not arm a job merely because they are truthy.
    needs["detect"]["outputs"][lane] = "false"
    assert evaluate_gate(needs)["ok"]


def test_release_exclusion_policy():
    # The PR-only checks are steps of repo-guards, which runs on every event: a release run
    # where it skipped never ran them at all, so only the deferred Desktop E2E may skip.
    assert required_results.EXCLUDED_JOBS == {"e2e-desktop"}


@pytest.mark.parametrize("needs", [{}, None])
@pytest.mark.parametrize("release", [False, True])
def test_empty_needs_fails_closed(needs, release):
    assert evaluate_gate(needs, release=release) == {
        "ok": False, "failed": ["<no-needs>"], "allowed_skips": [], "unallocated": [],
    }
    assert required_results.compact_results(needs) == {}


@pytest.mark.parametrize("release,results,code,report", [
    (True, {"detect": "success", "e2e-desktop": "skipped"}, 0, "All checks passed"),
    (True, {"tests": "skipped", "repo-guards": "skipped", "lint": "failure"},
     1, "::error::3 job(s) failed: lint, repo-guards, tests"),
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
        check=False,
    )
    assert child.returncode == code, child.stderr
    assert report in child.stdout
    key, value = output.read_text(encoding="utf-8-sig").strip().split("=", 1)
    assert key == "needs-json"
    assert json.loads(value) == results
    assert f"{key}={value}" in child.stdout
    for name, result in results.items():
        assert f"{name}: {result}" in child.stdout


# --allow-superseded: a cancelled duplicate pull_request run of one push.
_OWN = {"id": 1, "run_attempt": 1, "workflow_id": 7, "head_sha": "abc", "event": "pull_request",
        "status": "in_progress", "conclusion": None, "run_started_at": "2026-11-09T11:45:30Z",
        "head_branch": "fix/x", "head_repository": {"id": 99}}
_CANCELLED_DETECT = {"detect": {"result": "cancelled", "outputs": {}}, "tests": {"result": "skipped"}}


def _fake_fetch(runs, own=_OWN, attempt1="cancelled"):
    """Serve the runs API: filter by head_sha/event and paginate like GitHub."""
    calls = []
    listing = [own, *runs]

    def fetch(path):
        calls.append(path)
        if path == f"repos/o/r/actions/runs/{own['id']}":
            return own
        if path == f"repos/o/r/actions/runs/{own['id']}/attempts/1":
            return {"conclusion": attempt1}
        route, _, query = path.partition("?")
        assert route == "repos/o/r/actions/workflows/7/runs", path
        params = dict(urllib.parse.parse_qsl(query))
        matching = [run for run in listing if run.get("workflow_id") == 7
                    and all(run.get(k) == params[k] for k in ("head_sha", "event"))]
        size, page = int(params["per_page"]), int(params["page"])
        return {"total_count": len(matching),
                "workflow_runs": matching[(page - 1) * size:page * size]}
    return fetch, calls


def _run_main(monkeypatch, capsys, needs, argv, fetch, run_id=1):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", str(run_id))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(needs)))
    code = required_results.main(argv, fetch=fetch)
    return code, capsys.readouterr().out


def _dup(**over):
    return {**_OWN, "id": 2, **over}


@pytest.mark.parametrize("other", [
    # pending: the twin waits in the concurrency group until this run's always() gate ends
    _dup(status="pending"), _dup(status="queued"), _dup(status="in_progress"),
    _dup(status="completed", conclusion="success"),
])
def test_cancelled_duplicate_passes_when_twin_run_is_live(monkeypatch, capsys, other):
    fetch, calls = _fake_fetch([other])
    code, out = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--allow-superseded"], fetch)
    assert code == 0
    assert "run 2 on the same head SHA" in out
    assert "::error::" not in out
    assert "detect: cancelled" in out
    assert len(calls) == 2


@pytest.mark.parametrize("other", [
    None,  # the cancelled run is alone (manual cancel, or superseded by a newer SHA)
    _dup(status="completed", conclusion="cancelled"),
    _dup(status="completed", conclusion="failure"),
    _dup(head_sha="def"), _dup(workflow_id=8), _dup(event="push"),
])
def test_cancelled_run_without_live_twin_still_fails(monkeypatch, capsys, other):
    fetch, _ = _fake_fetch([] if other is None else [other])
    code, out = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--allow-superseded"], fetch)
    assert code == 1
    assert "::error::1 job(s) failed: detect" in out


@pytest.mark.parametrize("other", [_dup(head_sha="def"), _dup(workflow_id=8), _dup(event="push")])
def test_find_superseding_run_ignores_a_twin_of_another_sha_workflow_or_event(other):
    # The client re-checks every key, so a listing the API failed to filter still cannot delegate.
    assert required_results.find_superseding_run(_OWN, [other]) is None


# Another PR on the same SHA (pull_requests is empty on fork runs, so the PR
# cannot be matched): its runs come from a different branch or head repository.
@pytest.mark.parametrize("own,other", [
    (_OWN, _dup(head_branch="other")),
    (_OWN, _dup(head_repository={"id": 100})),
    (_OWN, _dup(head_repository=None)),
    ({**_OWN, "head_repository": None}, _dup(head_repository=None)),
    ({**_OWN, "head_branch": None}, _dup(head_branch=None)),
])
def test_a_twin_from_another_branch_or_head_repository_cannot_carry_the_verdict(monkeypatch, capsys,
                                                                                 own, other):
    fetch, _ = _fake_fetch([other], own=own)
    code, out = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--allow-superseded"], fetch)
    assert code == 1
    assert "::error::1 job(s) failed: detect" in out


# A fork run held for approval: attempt 1 ran nothing, and once approved the
# lower-id twin can start later and cancel the higher-id one (#151184, #150842).
_APPROVED = {**_OWN, "id": 2, "run_attempt": 2}


@pytest.mark.parametrize("attempt1,other,code", [
    ("action_required", _dup(id=1, run_attempt=2, run_started_at="2026-11-09T11:45:33Z"), 0),
    # a real rerun of an attempt that ran never delegates
    ("cancelled", _dup(id=1, run_attempt=2, run_started_at="2026-11-09T11:45:33Z"), 1),
    # the twin started first, so it was the one cancelled (or cancelling nothing)
    ("action_required", _dup(id=1, run_attempt=2, run_started_at="2026-11-09T11:45:29Z"), 1),
])
def test_approved_fork_duplicate_delegates_to_the_later_starter(monkeypatch, capsys, attempt1,
                                                                 other, code):
    fetch, _ = _fake_fetch([other], own=_APPROVED, attempt1=attempt1)
    got, _ = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--allow-superseded"], fetch,
                       run_id=2)
    assert got == code


def test_a_higher_id_twin_that_started_earlier_cannot_carry_the_verdict():
    own = {**_OWN, "run_started_at": "2026-11-09T11:45:33Z"}
    assert required_results.find_superseding_run(own, [_dup()]) is None


@pytest.mark.parametrize("own,other", [
    # newer -> older: the older run cannot carry the verdict for the run that superseded it
    ({**_OWN, "id": 2}, _dup(id=1)),
    ({**_OWN, "id": 2}, _dup(id=1, status="completed", conclusion="success")),
    # a rerun of the cancelled run never delegates, even to a newer live twin
    ({**_OWN, "run_attempt": 2}, _dup()),
    ({**_OWN, "run_attempt": 3}, _dup()),
    ({**_OWN, "run_attempt": None}, _dup()),
])
def test_delegation_only_points_forward_from_a_first_attempt(monkeypatch, capsys, own, other):
    fetch, _ = _fake_fetch([other], own=own)
    code, out = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--allow-superseded"], fetch,
                          run_id=own["id"])
    assert code == 1
    assert "::error::1 job(s) failed: detect" in out


def _cancelled_twins(count, first_id=10):
    return [_dup(id=first_id + i, status="completed", conclusion="cancelled") for i in range(count)]


def test_successor_on_a_later_page_is_found(monkeypatch, capsys):
    fetch, calls = _fake_fetch([*_cancelled_twins(150), _dup(id=500)])
    code, out = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--allow-superseded"], fetch)
    assert code == 0
    assert "run 500 on the same head SHA" in out
    pages = [dict(urllib.parse.parse_qsl(c.partition("?")[2])) for c in calls[1:]]
    assert [p["page"] for p in pages] == ["1", "2"]
    assert all(p["event"] == "pull_request" and p["head_sha"] == "abc" for p in pages)


@pytest.mark.parametrize("twins,pages", [(150, 2), (98, 1), (2000, 10)])
def test_exhausted_pages_without_successor_fail(monkeypatch, capsys, twins, pages):
    # 2000 cancelled twins: the live run lies past the page bound and is never read.
    fetch, calls = _fake_fetch([*_cancelled_twins(twins), _dup(id=5000)] if twins == 2000
                               else _cancelled_twins(twins))
    code, out = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--allow-superseded"], fetch)
    assert code == 1
    assert "::error::1 job(s) failed: detect" in out
    assert len(calls) == 1 + pages


def test_api_error_on_a_later_page_fails_closed(monkeypatch, capsys):
    serve, _ = _fake_fetch([*_cancelled_twins(150), _dup(id=500)])

    def fetch(path):
        if "page=2" in path:
            raise OSError("api down")
        return serve(path)
    code, out = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--allow-superseded"], fetch)
    assert code == 1
    assert "keeping the failure" in out


def test_two_cancelled_duplicates_cannot_vouch_for_each_other(monkeypatch, capsys):
    # A (id 1) is cancelled by B (id 2); B is then cancelled while A's always() gate still
    # runs, so each gate sees the other run still in progress. At most one may pass.
    run_a = {**_OWN, "id": 1}
    run_b = {**_OWN, "id": 2}
    codes = {}
    for own, other in ((run_a, run_b), (run_b, run_a)):
        fetch, _ = _fake_fetch([other], own=own)
        codes[own["id"]], _ = _run_main(monkeypatch, capsys, _CANCELLED_DETECT,
                                        ["--allow-superseded"], fetch, run_id=own["id"])
    assert codes == {1: 0, 2: 1}


@pytest.mark.parametrize("needs", [
    {"detect": {"result": "cancelled"}, "tests": {"result": "failure"}},
    {"detect": {"result": "success", "outputs": {"python": "true"}}, "tests-os": {"result": "cancelled"}},
    {},
])
def test_real_failures_never_consult_the_api(monkeypatch, capsys, needs):
    def fetch(path):
        raise AssertionError(f"unexpected API call {path}")
    code, out = _run_main(monkeypatch, capsys, needs, ["--allow-superseded"], fetch)
    assert code == 1
    assert "::error::" in out


def _api_down(path):
    raise OSError("api down")


@pytest.mark.parametrize("fetch", [
    _api_down, lambda path: {}, lambda path: None,
    # a non-object runs page: AttributeError on .get must still print the report
    lambda path: _OWN if path.endswith("runs/1") else [],
])
def test_lookup_error_fails_closed(monkeypatch, capsys, fetch):
    code, out = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--allow-superseded"], fetch)
    assert code == 1
    assert "keeping the failure" in out
    assert "::error::1 job(s) failed: detect" in out


def test_without_flag_cancelled_duplicate_still_fails(monkeypatch, capsys):
    def fetch(path):
        raise AssertionError(path)
    code, _ = _run_main(monkeypatch, capsys, _CANCELLED_DETECT, [], fetch)
    assert code == 1


def test_release_rejects_allow_superseded(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, capsys, _CANCELLED_DETECT, ["--release", "--allow-superseded"],
                  lambda path: None)
    assert exc.value.code == 2


# ── --allow-unallocated: "never ran" is not "failed" ──────────────────────────
# Measured 2026-11-19: five lanes pin GitHub larger hosted runners this account
# cannot allocate, so GitHub queues them for its 24 h maximum and cancels them with
# steps == [] and runner_name == "". Such a job never executed; the aggregate gate
# must be able to say so without pretending it passed.

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
    """Serve repos/o/r/actions/runs/1/jobs?per_page=100&page=N like GitHub."""
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
    """A run that allocated work and also had unallocatable cancels."""
    jobs = [_job(conclusion="success", runner="GitHub Actions 1", steps=[{"name": "run"}],
                 labels=("ubuntu-latest",)) for _ in range(executed)]
    jobs += [_job(**over) for _ in range(cancelled)]
    return jobs


def _run_unalloc(monkeypatch, capsys, needs, fetch, argv=("--allow-unallocated",)):
    code, out = _run_main(monkeypatch, capsys, needs, list(argv), fetch)
    return code, out


def test_t1_unallocated_cancels_are_non_blocking_with_evidence(monkeypatch, capsys):
    fetch, calls = _jobs_fetch(_allocated_jobs())
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 0, out
    assert "::warning::Unallocated jobs (no runner assigned, non-blocking): tests, tests-os" in out
    assert "🕳️ tests: unallocated (no runner assigned) — non-blocking" in out
    assert "::error::" not in out
    assert len(calls) == 1


def test_t1b_negative_control_without_the_flag_still_fails(monkeypatch, capsys):
    # The same input and the same evidence, but no flag: the gate must stay red.
    fetch, calls = _jobs_fetch(_allocated_jobs())
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch, argv=())
    assert code == 1
    assert "::error::2 job(s) failed: tests, tests-os" in out
    assert calls == []


def test_t2_release_refuses_the_flag(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, lambda path: None,
                     argv=("--release", "--allow-unallocated"))
    assert exc.value.code == 2


def test_t3_required_consumer_never_waived(monkeypatch, capsys):
    # detect selects the python lane: tests/tests-os must have executed. No API call at all.
    needs = {**_UNALLOC_NEEDS, "detect": {"result": "success", "outputs": {"python": "true"}}}
    code, out = _run_unalloc(monkeypatch, capsys, needs, lambda path: _raise(path))
    assert code == 1
    assert "::error::" in out


def _raise(path):
    raise AssertionError(f"unexpected API call {path}")


def test_t4_real_failures_never_consult_the_api(monkeypatch, capsys):
    needs = {**_UNALLOC_NEEDS, "tests": {"result": "failure"}}
    code, out = _run_unalloc(monkeypatch, capsys, needs, _raise)
    assert code == 1
    assert "::error::2 job(s) failed: tests, tests-os" in out


@pytest.mark.parametrize("fetch", [
    lambda path: (_ for _ in ()).throw(OSError("api down")),
    lambda path: None,
    lambda path: [],
    lambda path: {"total_count": 4},              # jobs key missing
    lambda path: {"total_count": 99, "jobs": []},  # nothing read, count disagrees
])
def test_t5_lookup_faults_fail_closed(monkeypatch, capsys, fetch):
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "keeping the failure" in out or "Not downgrading" in out
    assert "::error::" in out


def test_t6_one_executed_cancel_blocks_the_whole_downgrade(monkeypatch, capsys):
    jobs = _allocated_jobs() + [_job(runner="GitHub Actions 9", steps=[{"name": "run"}])]
    fetch, _ = _jobs_fetch(jobs)
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "Not downgrading to unallocated" in out
    assert "::error::" in out


def test_t10_zero_execution_run_is_not_waived(monkeypatch, capsys):
    # A run cancelled before anything allocated: every job is a zero-step cancel, so
    # "all cancelled jobs are unallocated" is trivially true. It must still fail.
    jobs = [_job() for _ in range(4)]
    fetch, _ = _jobs_fetch(jobs)
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "executed=0" in out
    assert "::error::" in out


def test_t11_incomplete_job_list_is_not_evidence(monkeypatch, capsys):
    fetch, _ = _jobs_fetch(_allocated_jobs(), total=99)
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "truncated=True" in out


def test_t12_label_outside_the_pinned_set_blocks_the_downgrade(monkeypatch, capsys):
    # The macos leg cancelled on a standard, allocatable label: re-runnable, so fatal.
    fetch, _ = _jobs_fetch(_allocated_jobs(labels=("macos-latest",)))
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1
    assert "labels_ok=False" in out


def test_t13_extra_labels_block_the_downgrade(monkeypatch, capsys):
    fetch, _ = _jobs_fetch(_allocated_jobs(labels=("self-hosted", "ubuntu-latest-32-core")))
    code, _ = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 1


def test_t14_later_page_is_read_and_counted(monkeypatch, capsys):
    # 150 cancelled jobs: page 1 is full, so page 2 must be read before deciding.
    jobs = _allocated_jobs(cancelled=150)
    fetch, calls = _jobs_fetch(jobs)
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch)
    assert code == 0, out
    assert len(calls) == 2
    assert "150/150 cancelled jobs never ran" in out


def test_t15_successful_gate_never_calls_the_api(monkeypatch, capsys):
    needs = {**_UNALLOC_NEEDS, "tests": {"result": "success"}, "tests-os": {"result": "success"}}
    code, out = _run_unalloc(monkeypatch, capsys, needs, _raise)
    assert code == 0
    assert "All checks passed" in out


def test_t16_no_waiver_when_detect_never_ran(monkeypatch, capsys):
    # detect selects the required lanes; if it was itself cancelled we cannot know which
    # consumers had to execute, so the exemption set is unknown and the waiver is refused.
    needs = {**_UNALLOC_NEEDS, "detect": {"result": "cancelled", "outputs": {}}}
    fetch, calls = _jobs_fetch(_allocated_jobs())
    code, out = _run_unalloc(monkeypatch, capsys, needs, fetch)
    assert code == 1
    assert "::error::" in out
    assert calls == []          # no evidence lookup at all


# The production pairing: ci.yaml passes --allow-unallocated AND --allow-superseded
# together on every pull_request run. Both paths can green a cancelled gate, so their
# precedence is a production behaviour, not an option.
def _both_fetch(jobs, runs=None):
    jobs_serve, _ = _jobs_fetch(jobs)
    runs_serve, runs_calls = _fake_fetch(runs or [])

    def fetch(path):
        return jobs_serve(path) if "/jobs" in path else runs_serve(path)
    return fetch, runs_calls


def test_t17_both_flags_waiver_wins_and_never_asks_about_duplicates(monkeypatch, capsys):
    fetch, runs_calls = _both_fetch(_allocated_jobs(), runs=[_dup(status="in_progress")])
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch,
                             argv=("--allow-unallocated", "--allow-superseded"))
    assert code == 0, out
    assert "::warning::Unallocated jobs" in out
    assert "::notice::" not in out
    assert runs_calls == []          # the waiver settles it; no duplicate lookup
    assert out.count("All checks passed") == 1


def test_t18_both_flags_declined_waiver_falls_through_to_the_duplicate_run(monkeypatch, capsys):
    # One cancelled job DID run, so the waiver declines; a later live duplicate run then
    # carries the verdict through the pre-existing --allow-superseded path.
    jobs = _allocated_jobs() + [_job(runner="GitHub Actions 9", steps=[{"name": "run"}])]
    fetch, runs_calls = _both_fetch(jobs, runs=[_dup(status="in_progress")])
    code, out = _run_unalloc(monkeypatch, capsys, _UNALLOC_NEEDS, fetch,
                             argv=("--allow-unallocated", "--allow-superseded"))
    assert code == 0, out
    assert "Not downgrading to unallocated" in out
    assert "::notice::Cancelled duplicate run" in out
    assert "::warning::Unallocated jobs" not in out
    assert runs_calls                  # the duplicate lookup did happen


@pytest.mark.parametrize("env", ["GITHUB_RUN_ID", "GITHUB_REPOSITORY"])
def test_t19_missing_run_identity_fails_closed(monkeypatch, capsys, env):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(_UNALLOC_NEEDS)))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    code = required_results.main(["--allow-unallocated"], fetch=_raise)
    out = capsys.readouterr().out
    assert code == 1
    assert "keeping the failure" in out or "Not downgrading" in out
