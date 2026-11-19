#!/usr/bin/env python3
"""Strict CI aggregate gate (shared by ci.yaml and the stable orchestrator).

Behavior tests: tests/ci/test_required_results.py

Input: the JSON ``toJSON(needs)`` of the all-checks-pass job on stdin.
Any non-``success`` result fails the gate. ``skipped`` additionally fails in
release mode unless the job is in :data:`EXCLUDED_JOBS` (PR-only jobs that
cannot run on a tag event, plus the deferred Desktop E2E). The OSV scan is
advisory in its findings only — its execution is required.

``--allow-unallocated`` (never release): a ``cancelled`` job can also mean the job
never ran. On a plan that cannot allocate a pinned runner label, GitHub holds the job
for its 24 h maximum queue wait and cancels it with ``steps == []`` and
``runner_name == ""``. With the flag, a gate failed only by ``cancelled`` jobs may pass
when the run's own job list proves every cancelled job never executed, every one pinned
a known unallocatable label, at least one job did allocate, the list was read
completely, and ``detect`` succeeded (it decides which lanes were meant to run). Any
API error, truncation or ambiguity keeps the failure.

    echo "$NEEDS" | python3 scripts/ci/required_results.py [--release | --allow-unallocated]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Callable

# Jobs that can never run on a release (push/tag) event, plus the deferred
# Desktop E2E. These are the only skips a strict run tolerates.
PR_ONLY_JOBS = ("history-check", "lockfile-diff", "supply-chain")
DEFERRED_JOBS = ("e2e-desktop",)
EXCLUDED_JOBS = frozenset((*PR_ONLY_JOBS, *DEFERRED_JOBS))

NEEDS_JSON_OUTPUT = "needs-json"

# GitHub larger hosted runners (Team/Enterprise only). A job pinned to one of these
# cannot be allocated on this repository's plan, so it waits out GitHub's 24 h queue
# maximum and is cancelled with no steps and no runner. Measured 2026-11-19 on
# run 43947376952. `macos-latest` is deliberately absent: a cancel there is re-runnable.
UNALLOCATABLE_LABELS = frozenset({
    "ubuntu-latest-32-core",
    "windows-latest-32-core",
    "windows-latest-32-arm-core",
})
_JOBS_PER_PAGE = 100
_MAX_JOB_PAGES = 10


def evaluate_gate(
    needs: dict[str, dict[str, Any]] | None,
    release: bool = False,
) -> dict[str, Any]:
    """Verdict for a ``needs`` context; see the module docstring.

    Returns ``ok``, plus sorted ``failed`` (every non-success, non-allowed
    entry) and ``allowed_skips``.
    """
    failed: list[str] = []
    allowed_skips: list[str] = []
    entries = needs or {}
    if not entries:
        failed.append("<no-needs>")
    for name, info in entries.items():
        result = (info or {}).get("result")
        if result == "success":
            continue
        if result == "skipped" and (not release or name in EXCLUDED_JOBS):
            allowed_skips.append(name)
            continue
        failed.append(name)
    return {
        "ok": not failed,
        "failed": sorted(failed),
        "allowed_skips": sorted(allowed_skips),
        "unallocated": [],
    }


def _github_get(path: str) -> Any:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    request = urllib.request.Request(f"{api}/{path.lstrip('/')}")
    request.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _classify_jobs(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Count executed/cancelled/unallocated jobs and whether the labels are pinned."""
    executed = sum(1 for job in jobs if job.get("runner_name"))
    cancelled = [job for job in jobs if job.get("conclusion") == "cancelled"]
    unallocated = [job for job in cancelled
                   if not (job.get("steps") or []) and not job.get("runner_name")]
    labels_ok = all(set(job.get("labels") or []) <= UNALLOCATABLE_LABELS for job in cancelled)
    return {"executed_total": executed, "cancelled_total": len(cancelled),
            "unallocated_total": len(unallocated), "labels_ok": labels_ok}


def unallocated_verdict(fetch: Callable[[str], Any] = _github_get) -> tuple[bool, str]:
    """(may_downgrade, why) from this run's job list; every fault path returns False."""
    # Known residual: the evidence proves the RUN, not the individual needs entries —
    # the API exposes no needs-key <-> job-name mapping. Not constructible against
    # today's API (every needs-cancelled job appears in the run's job list), but it is
    # the assumption this waiver rests on.
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    jobs: list[dict[str, Any]] = []
    total: Any = None
    for page in range(1, _MAX_JOB_PAGES + 1):
        payload = fetch(f"repos/{repo}/actions/runs/{run_id}/jobs"
                        f"?per_page={_JOBS_PER_PAGE}&page={page}")
        if not isinstance(payload, dict):
            return False, "job list unreadable"
        total = payload.get("total_count")
        batch = payload.get("jobs") or []
        jobs.extend(batch)
        if len(batch) < _JOBS_PER_PAGE:
            break
    counts = _classify_jobs(jobs)
    truncated = not isinstance(total, int) or len(jobs) != total
    # One expression, never split: a fault or a partial view must never read as clean.
    may = (not truncated
           and counts["executed_total"] > 0
           and counts["cancelled_total"] > 0
           and counts["unallocated_total"] == counts["cancelled_total"]
           and counts["labels_ok"] is True)
    why = (f"{counts['unallocated_total']}/{counts['cancelled_total']} cancelled jobs never ran "
           f"(no steps, no runner); {counts['executed_total']} job(s) did allocate"
           if may else
           f"not every cancelled job is unallocated (executed={counts['executed_total']}, "
           f"cancelled={counts['cancelled_total']}, unallocated={counts['unallocated_total']}, "
           f"labels_ok={counts['labels_ok']}, truncated={truncated})")
    return may, why


def only_cancellations(needs: dict[str, dict[str, Any]] | None, verdict: dict[str, Any]) -> bool:
    """True when the gate failed solely because jobs were ``cancelled``."""
    failed = verdict["failed"]
    entries = needs or {}
    return bool(failed) and all(
        name in entries and (entries[name] or {}).get("result") == "cancelled" for name in failed
    )


def compact_results(needs: dict[str, dict[str, Any]] | None) -> dict[str, str]:
    """{job_name: result} for every entry, for the PR comment assembler."""
    return {name: (info or {}).get("result", "") for name, info in (needs or {}).items()}


def render_report(needs: dict[str, dict[str, Any]] | None, verdict: dict[str, Any]) -> list[str]:
    """Per-job lines plus ::error:: annotations for the failed set."""
    lines: list[str] = []
    unallocated = set(verdict.get("unallocated") or ())
    for name in sorted(needs or {}):
        result = (needs[name] or {}).get("result", "unknown")
        if name in unallocated:
            lines.append(f"🕳️ {name}: unallocated (no runner assigned) — non-blocking")
            continue
        icon = {"success": "✅", "skipped": "⏭️"}.get(result, "❌")
        lines.append(f"{icon} {name}: {result}")
    if verdict["failed"]:
        failed = verdict["failed"]
        lines.append(f"::error::{len(failed)} job(s) failed: {', '.join(failed)}")
    return lines


def main(argv: list[str] | None = None, fetch: Callable[[str], Any] = _github_get) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--release",
        action="store_true",
        help="Strict mode: a skipped excluded job is the only tolerated skip.",
    )
    parser.add_argument(
        "--allow-unallocated",
        action="store_true",
        help="Pass a gate failed only by cancelled jobs when the run shows every cancelled job never executed on an unallocatable runner label.",
    )
    args = parser.parse_args(argv)
    if args.release and args.allow_unallocated:
        parser.error("--allow-unallocated never applies to release runs")

    needs = json.load(sys.stdin)
    verdict = evaluate_gate(needs, release=args.release)
    compact = compact_results(needs)

    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as fh:
            fh.write(f"{NEEDS_JSON_OUTPUT}={json.dumps(compact)}\n")

    print(f"{NEEDS_JSON_OUTPUT}={json.dumps(compact)}")
    # `detect` decides which lanes were meant to run; if it never ran, the required
    # set is unknown and the waiver cannot be applied honestly.
    detect_ran = ((needs or {}).get("detect") or {}).get("result") == "success"
    unallocated: list[str] = []
    unallocated_why = ""
    if args.allow_unallocated and detect_ran and only_cancellations(needs, verdict):
        try:
            may, unallocated_why = unallocated_verdict(fetch)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:  # fail closed
            print(f"Unallocated-run evidence unavailable, keeping the failure: {exc}")
        else:
            if may:
                unallocated = list(verdict["failed"])
                verdict["unallocated"] = sorted(unallocated)
                verdict["failed"] = []
                verdict["ok"] = True
            else:
                print(f"Not downgrading to unallocated: {unallocated_why}")
    for line in render_report(needs, verdict):
        print(line)
    if unallocated:
        print(f"::warning::Unallocated jobs (no runner assigned, non-blocking): "
              f"{', '.join(unallocated)} — {unallocated_why}")
    if verdict["ok"]:
        print("All checks passed" if args.release else "All checks passed (or were skipped)")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
