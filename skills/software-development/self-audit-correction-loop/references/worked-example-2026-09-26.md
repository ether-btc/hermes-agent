# Worked example — 2026-09-26 self-audit (RPi5, Hermes 0.21.0)

A full pass of the loop, kept because the *corrections* are the reusable part.

## Shape

Bot team dispatched (researcher + reviewer) on cheap models for evidence
gathering; GLM-5.3-flash for the adversarial review after Codex hit its usage
limit. Output: `STATUS.md` (resumable), `findings/GAP-LEDGER.md`,
`OPTIONS.md`, `FIXES-APPLIED.md`, `receipts/`, and a **generated**
`registry/registry.md` with a weekly staleness check.

## Four real defects found and fixed

| Defect | Evidence of fix |
|---|---|
| 70 dangling security-skill symlinks in `researcher`+`reviewer` | 0 dangling; 88 loadable/profile (was 53). Undo TSV captured first. |
| `bot-fallback-watch.sh` validator rejected a **valid** `fallback_model` dict | exits 0 across 11 profiles (was exit 2 on 3); 5/5 negative controls still reject |
| `curator.consolidate` off → 66 duplicate copies, 40 drifted forks | `hermes curator status` → `consolidate: on` |
| hermes-lcm live despite user decommissioning | runtime stopped+disabled, ports free, health check all-clear |

Plus: 5.9 GB reclaimed to `hermes128` (verified by **UUID** `bceffe6e-…`),
gateway restarted and verified by **live Telegram API sockets**, not unit state.

## The two errors review caught in my own conclusions

**B1 — prompt tier inverted.** I published that the skills index (33,951 B) sits
in the *stable* cached tier, making trimming worth ~0.5% of cache-read. It fails on
arithmetic alone: the stable tier is **17,116 B**, smaller than the index.
Source: `agent/system_prompt.py:776` puts `skills_prompt` first in
`volatile_parts`; `hermes_cli/prompt_size.py:161-163` records it was moved *out*
of stable deliberately. Real figure: ~9% of uncached input (7,978 tok/call) — my
claim **understated the benefit by ~18×**. Root cause: the CLI's tier *label* is
stale. The 94.5% ratio itself was fine and reproduced to 94.55%.

**B2 — "fully retired" overstated.** Runtime was gone; four references remained:
`hermes-lcm` symlinks in both bot profiles, 3 default-profile LCM skills,
`lcm-large-outputs` (107,058,787 B), and a `config.yaml` comment pointing at a
deleted directory. Reframed as *runtime retired, content cleanup outstanding* —
and deliberately **not** silently deleted, since retiring a system does not imply
deleting skill content.

## What the reviewer got wrong

It reported "M2 org mirrors" as a counter-example to a finding. Reading the code
showed it was `_org/` skill-directory resolution — a same-name, different-subsystem
false lead, and the original finding stood. So verification runs **both**
directions: a reviewer's objection can be wrong, and its refutation can be right.

## Majors fixed

- `OPTIONS.md` still recommended a change the user had already declined → marked
  **superseded** so a settled decision is not re-raised.
- Cron figures reconciled only by including 94 `NULL started_at` rows (61+94=155);
  the naive filter returns 61. The DB is a **1000-row ring buffer**, so counts age
  out — documented the rule and labelled unreproducible snapshots.
- Registry drift fixed **at the generator**: a hardcoded "delete me / 1.03 GB"
  line outlived the directory it described. `os.path.isdir()` now decides the
  wording, and LCM residue is measured live.

## Pre-registration that made the review productive

The M2 follow-on project fixed its decision rule *before* measuring: adopt only if
≥10% reduction in **uncached** tokens with recall intact and one-key rollback;
reject if <1%. Stating the threshold first is what stops a favourable number from
being rationalised into a recommendation.

## What I got wrong (added after review REJECTED the first draft)

The first version claimed the curator's `patch` ledger entries produced **no
content changes**, and generalised that into a lesson ("a ledger verb is not an
effect"). **Both were false.** The ledger carries `before`/`after` sha256
manifests per mutation, and for all 10 entries checked:
`before != after`, with the live on-disk hash equal to the *after* value — real,
persisted content edits.

The cause of my false claim is the reusable part: **both snapshots postdated the
edits** (snapshots 14:17:28Z / 14:33:03Z; last patch 14:11:49Z), so diffing
post-change disk state against a post-change snapshot was a **vacuous test** that
returned "unchanged". It also misattributed those patches to this run (they were
stamped 13:03–14:11Z, before the 14:33:01Z run start) and cited a nonexistent
function `_apply_inactivity` (real name: `apply_automatic_transitions`).

Corrected invariants, with exact enforcement sites:
delete→archive lives in `tools/skill_manager_tool.py:540`/`:752`, not the
docstring; cron protection is enforced only in the deterministic pass (219/229)
and is **prompt-only** in the LLM fork (rule 3c, ~328); the aux client falls back
to the **main** model on slot-resolution failure (1028–1029).

**The outcome itself survived review intact:** prune worked (5 stale, 2 archived,
7 reactivated), merge fork stalled and executed nothing.

## Durable lessons

1. Re-measure before recommending; reasoning harder never caught anything.
2. Price tokens against `session_model_usage`, never `prompt-size` byte counts —
   and read the runtime source for tier membership.
3. Measure path arithmetic (`readlink -m` per level), don't compute it.
4. A failing watchdog is worse than none — it looks like coverage.
5. A claim that can't be reproduced later is a snapshot, not a fact.
6. "Retired" means nothing still references it.
7. When a generated file drifts, fix the generator.
8. **A null diff is evidence only if the baseline predates the change.**
9. **Grep a symbol before citing it** — a fabricated function name discredits the
   whole record.
10. **Enforcement ≠ prompt instruction.** Say which one you verified.
