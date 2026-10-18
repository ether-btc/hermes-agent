---
title: "Self Audit Correction Loop — Audit your own work, review it adversarially, file it"
sidebar_label: "Self Audit Correction Loop"
description: "Audit your own work, review it adversarially, file it"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Self Audit Correction Loop

Audit your own work, review it adversarially, file it.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/self-audit-correction-loop` |
| Version | `0.1.0` |
| Author | austrian_guy (ether-btc), Hermes Agent |
| License | MIT |
| Platforms | linux, macos |
| Tags | `SelfAudit`, `Review`, `Correction`, `Verification`, `Filing`, `Rigor` |
| Related skills | [`codex`](../../bundled/autonomous-ai-agents/autonomous-ai-agents-codex.md), [`requesting-code-review`](../../bundled/software-development/software-development-requesting-code-review.md), [`llm-wiki`](../../bundled/research/research-llm-wiki.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Self-Audit Correction Loop

Audit your **own** recent work, put it through an adversarial review, correct it in
place, and file it. This audits *your decisions, claims, and artifacts* — not a
system's wiring (services, databases, crons). Stdlib only, plus
`terminal`/`read_file`/`search_files`/`patch`/`write_file`/`delegate_task` and the
`codex` skill.

The governing fact, learned the hard way: **every serious error in a self-audit is
an unverified claim, not a missing tool.** Re-measuring catches them. Reasoning
harder never does.

## When to Use

- "Review your own behaviour over the last projects"
- "Audit what you did and improve it incrementally"
- Before publishing a report, wiki page, or memo you authored
- Any time you are about to say "X is fixed", "X is retired", or "X is the
  dominant cost" — all three are claims that have been wrong here before

## Prerequisites

- An external reviewer: `delegate_task`, or the `codex` skill via
  `codex exec` (PTY, in a git repo). See the fallback rule below.
- A durable home for the record: a project dir, a git repo, a wiki.

## How to Run

Work in this order. **Do not publish before the review returns.**

1. `write_file` a `STATUS.md` (resumable state) + a findings ledger.
2. Harvest read-only evidence; tag every claim **VERIFIED** (ran a tool, saw a
   result) or **ASSERTED** (read a file / recalled context).
3. Fix what is unambiguous, with a receipt per fix.
4. `delegate_task` an adversarial reviewer with an explicit contract:
   `BLOCKERS / MAJORS / MINORS / FILING-VERDICT`, read-only, "find what is
   wrong, overstated, or unreproducible".
5. **Re-derive each finding at source before accepting it** (see Pitfalls).
6. Correct in place, with the correction and its evidence left in the document.
7. File (wiki/git) only on `APPROVE`.

## Quick Reference

| Purpose | Command |
|---|---|
| Reviewer prompt | `delegate_task` w/ BLOCKERS/MAJORS/MINORS/FILING-VERDICT |
| Codex review | `codex exec --skip-git-repo-check "<prompt>"` |
| Poll reviewer | `delegate_task action="list"` |
| Verify a claim | run the tool; never `ASSERTED` it |
| Compare tiers | `hermes prompt-size` (labels are stale — read source) |
| Token ground truth | `session_model_usage` in `state.db` |
| Cron history | `$HERMES_HOME/cron/executions.db` (a **ring buffer**) |
| Safe bulk reclaim | copy-then-unlink to a backup disk (`rsync -a --remove-source-files` on rsync ≥3), never `rm` |
| Backup disk identity | verify by **UUID**, not a device node name (they can reshuffle) |

## Procedure

**1. Scaffold for resumability.** `STATUS.md` must let a cold session resume:
open decisions, what is actioned, receipts location, and what is blocked on the
user. Re-runnable scripts beat prose for anything that mutates.

**2. Harvest evidence, batched and read-only.** Independent probes go in one
`execute_code` block with a dict of commands; a fresh kernel per call, so
re-derive rather than assume carried state. Capture pre-state *before* any
mutation (`receipts/PRE-*.txt`).

**3. Fix only what is unambiguous.** One receipt per fix: what changed, the
command, the output proving it. If a fix is script-driven, make it idempotent and
print its effective values.

**4. Dispatch the review adversarially.** Give the reviewer the artifacts, the
claim list, and an explicit instruction to attack them. Ask for a filing verdict
so "approve" is a decision, not a vibe.

**5. Reviewer fallback rule.** If Codex is quota-exhausted or unavailable,
reroute to the cheap model via `delegate_task` with the **identical** contract.
Never downgrade a review gate into self-approval.

**6. Verify findings in source — in both directions.** Accept nothing on the
reviewer's word, and reject nothing on it either. Confirm in the code/config the
`terminal` runs. Note: Hermes source lives in **several trees**
(`$HERMES_HOME/hermes-agent/` is the one the gateway imports; a development
clone elsewhere may be stale) — cite the installed tree.

**7. Correct in place; never quietly drop.** Keep the retraction and its evidence
in the document. A published correction teaches more than a silent edit, and the
corrections are the most useful part of the record.

**8. Fix the generator, not the output.** If a generated file drifted, the bug is
in the script that made it. Otherwise the next regeneration reintroduces it.

**9. File, then verify the remote.** Push, then confirm with an independent read
(`gh api repos/<owner>/<repo>/commits/<branch> --jq .sha`). In a worktree, never
touch a checkout another session has uncommitted work in.

## Dormant projects: sweep the records, not just the code

A dorm/freeze decision stops the *code*, not the *records*. Once a project is
parked, its docs are the only thing a future reader inherits, and checked boxes
stop being maintained. Sweep for stale claims explicitly — do not assume the
pass that closed the project covered them.

1. Grep for checked/complete markers (`[x]`, `✅ COMPLETE`, "achieved", "now
   returns") and extract every named artefact from them.
2. Verify each named artefact with a real command (`ls`, `git ls-files`,
   `git log --all -- <path>`). A box claiming a LICENSE, changelog, CI matrix or
   wheel gate that was never built is the most common false claim.
3. Re-run every numeric claim. Do not validate it against a number already in
   the file — that number may be the stale one.
4. Execute every code snippet verbatim, end to end, in the project venv.

Claim-shape traps confirmed in the wild:

- **A doc fix is not done until the documented code runs.** Removing a wrong
  import can leave a `NameError` where the `ImportError` was.
- **A metric at a threshold is not the mechanism you assume.** A 1.00x token
  ratio does not mean "returned verbatim"; it can be a labeled rewrite with a
  net-zero token delta. Read the output strings.
- **Check the ratio convention before judging a threshold.** If ratio is
  input/output, higher is better, and 0.97 clears a ">= 0.95" bar.
- **Latency is host-load-dependent, so it is not a fixed value.** Give the
  idle-host range *and* the under-load range, and forbid cross-machine
  comparison.
- **A bare number in a comparison table implies a measurement you may never
  have made.** Mark inherited figures as unmeasured and say whether a
  head-to-head comparison exists.
- **Do not silently reconcile a two-source contradiction** (e.g. a version
  declared differently in two files). That is a maintainer decision; record it
  as an open item inside a docs pass.
- **Reopened boxes keep the strikethrough plus the absence evidence**, so the
  next reader inherits an accurate picture instead of re-discovering the gap.

## A reviewer that returns no verdict is not a wasted run

A delegated review can die at its iteration cap and produce no summary. Its
transcript still holds the findings it established:

```bash
tail -f "$HERMES_HOME/cache/delegation/live/<delegation_id>/task-0.log"
```

Harvest them, and **re-dispatch only the unfinished part**. On a 2026-09-28 pass
this recovered three verified findings from a review that reported nothing.

Dispose each finding on its own evidence — in both directions. A reviewer can be
right about the finding and wrong about the evidence: one review called
reproducible benchmark figures "fabricated" purely because its own runs were on
a differently-loaded host. Re-run contested numbers, and record in the commit
message which findings you rejected and why.

## Pitfalls

- **Tier/label metadata rots.** `hermes prompt-size` *labels* the stable tier
  "identity/guidance/skills", but the skills index is **volatile**-tier
  (`agent/system_prompt.py`, `volatile_parts`). Trust the source, not the label —
  and sanity-check with arithmetic (the stable tier must be ≥ any block you
  claim is inside it).
- **"Retired" means nothing still references it.** Proving processes are down is
  not proof of retirement. Enumerate *references*: symlinks, skills, data dirs,
  config comments. Report runtime-retired vs content-outstanding separately.
- **Averaging a ratio hides the win.** A high cache-hit ratio does not mean a
  prompt block is cheap; check which tier the block lands in before pricing it.
- **Ring buffers age out.** `executions.db` is capped (e.g., `MAX_TERMINAL_EXECUTIONS = 1000` rows in current upstream).
  A count that cannot be reproduced later is a *snapshot*. State the query,
  including whether `NULL` rows are included, or the number becomes folklore.
- **Overclaiming words survive review.** "fully", "nothing", "always" invite
  refutation and are the first things a good reviewer attacks. Prefer "runtime
  retired; content cleanup outstanding".
- **Bulk deletes are not cleanup.** Move to a backup disk (verify by UUID) with
  copy-then-unlink so a timeout mid-copy loses nothing.
- **Do not touch the live TMPDIR**, or the worktree you are standing in.
- **A newly enabled subsystem can do more than its name implies.** The curator's
  `consolidate: on` does not just archive — it merges content *between* skills.
  Tell the user what it can now do.
- **A null diff result is only evidence if the baseline PREDATES the change.**
  Check the snapshot timestamp is older than the event before reading anything
  into a zero. Comparing post-change disk state against a post-change snapshot
  is a vacuous test that looks like a clean result. Where a tool keeps its own
  `before`/`after` manifests per mutation, that is the authoritative record.
- **Cite a symbol you have actually grepped.** A verification record naming a
  function that does not exist in the repo discredits every other claim on the
  page. `grep -c '<symbol>' <file>` before citing a line number.
- **Subagent exit codes are not results.** A background wrapper can exit 0 while
  the real work is still running. Verify by artifact, not exit status. Codex is
  worse: when quota-exhausted it **exits 0 having produced nothing**, so check
  for actual output, not a success code.
- **An agent that reports its own failure may be right — verify anyway.** A stalled
  tool-call ("stream stalled mid tool-call; the action was not executed") is the
  fail-safe path working. Confirm with an independent check, then report the
  feature as **non-functional**, not as "ran and did nothing harmful".
- **Separate enforcement from prompt-instruction.** "The model is told not to do
  X" and "the code prevents X" are different guarantees. State which one you
  actually verified, and note config-contingent fallbacks (e.g. an aux-model slot
  that silently falls back to the main model on resolution failure).
- **A healthy process does not validate the conclusions drawn from its output.**
  During one session the daemon was verifiably healthy the whole time — enabled,
  active, logging every 5s, reconciler divergence $0.00 — while the analysis
  resting on its data carried an **88-200x** overlapping-trade inflation. Liveness
  checks answer "is it running", never "is what I concluded from it true". When you
  close out a long session, audit the *derivations* separately from the services.
- **Before you declare a wall, check whether the blocker is the world or your
  inventory.** A closing "no honest data source exists" was true of every source
  *to hand* and false in fact: an independent on-chain venue with 35 pools and
  $104.3M liquidity was reachable the whole time. Measure the obvious alternative
  before declaring something impossible — and measure liquidity *and turnover*,
  because a suggested substitute can be orders of magnitude worse on volume and
  therefore untradeable rather than equivalent.
- **A conclusion reached without reading the project's own history is not a
  conclusion.** Declaring "no edge found" without first running `git log` over the
  relevant window missed a commit that had already tested the leading rule and
  cleared t>=2.0. Search history by **quantity** (`git log -S"<number>"`), and read
  the retraction commits first — they name the exact measurement error.

## Verification

```bash
# Every published claim re-derives, and no stale wording survives
grep -rnE 'fully retired|RECOMMENDED, do first|cached stable tier' <project-dir> ; echo "(empty = all retracted)"
python3 -m py_compile <project-dir>/scripts/*.py && echo "generators compile"
hermes config check && hermes curator status
```

## References

- `references/worked-example-2026-09-26.md` — the full loop: 4 defects fixed,
  2 published conclusions refuted by review, what the reviewer caught and what it
  got wrong.
- The worked example also records a page that was **REJECTED and rewritten**
  after publication: a "byte-for-byte unchanged" claim that was a vacuous diff
  (baseline postdated the edits), plus a citation to a function that did not
  exist. Read it before publishing any diff-based or line-cited verification.
