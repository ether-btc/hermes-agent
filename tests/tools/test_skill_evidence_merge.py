"""Tests for universal evidence preservation (option A, oracle-approved).

These probes prove that the evidence block on a SKILL.md survives ANY write
(content= rewrite, old_string/new_string patch) — not just the explicit
``evidence_merge`` path. The evidence_merge path itself remains the
additive-intent, counter-updating path; preservation is suspended during it.

Every "negative" check has a control probe so a broken implementation is
detected (2026-09-26 lesson: a control probe proves the probe can fire).
"""
import json
from pathlib import Path

import pytest


# Reuse the project's parsing helper so the assertion uses the same view of
# the frontmatter a downstream reader (skill_view, prompt cache) would see.
from agent.skill_utils import parse_frontmatter as _parse_frontmatter


VALID_SKILL_WITH_EVIDENCE = """---
name: test-evidence-skill
description: skill with evidence for preservation tests
evidence:
  version: 1
  updated: 2026-10-01
  success_count: 5
  fail_count: 1
  steps:
    - name: alpha
      ok: 5
      fail: 1
  evolution:
    - from: 0
      to: 1
      date: 2026-10-01
      reason: initial
---

# Test Skill

Original body.
"""


VALID_SKILL_NO_EVIDENCE = """---
name: test-no-evidence-skill
description: skill without evidence
---

# Test Skill

Original body.
"""


VALID_SKILL_WITH_BOM_AND_EVIDENCE = (
    "\ufeff"  # UTF-8 BOM
    + """---
name: bom-skill
description: skill with BOM and evidence
evidence:
  version: 1
  updated: 2026-10-01
  success_count: 2
  fail_count: 0
  steps:
    - name: alpha
      ok: 2
      fail: 0
  evolution:
    - from: 0
      to: 1
      date: 2026-10-01
      reason: initial
---

# BOM Skill

Original body.
"""
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + skills dir; bypass the write-approval gate so
    ``skill_manage`` mutates directly (no staging) — every probe here is
    about preservation, not the gate.
    """
    from agent import skill_utils
    from tools import skill_ledger, skill_manager_tool, skill_usage

    home = tmp_path / "home"
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)

    monkeypatch.setattr(skill_ledger, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_usage, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skills_dir])

    # Bypass the write-approval gate so skill_manage(action="patch", ...) etc.
    # mutate directly. The skill_ledger._find_skill import in skill_manager_tool
    # already routes to the patched SKILLS_DIR, so a created skill is findable
    # by _locate_for_write later in the same test.
    bypass = skill_manager_tool._skill_gate_bypass
    token = bypass.set(True)
    try:
        yield {"home": home, "skills": skills_dir, "monkeypatch": monkeypatch}
    finally:
        bypass.reset(token)


def _create(name: str, content: str, skills_env) -> Path:
    """Create a skill via skill_manage; return the SKILL.md path."""
    from tools.skill_manager_tool import skill_manage

    r = json.loads(skill_manage(action="create", name=name, content=content))
    assert r["success"] is True, r
    return skills_env["skills"] / name / "SKILL.md"


def _read_fm(skill_md: Path):
    """Parse the current SKILL.md frontmatter; the body is irrelevant to these
    probes, only the frontmatter mapping is checked."""
    text = skill_md.read_text(encoding="utf-8")
    fm, _ = _parse_frontmatter(text)
    return fm


# ---------------------------------------------------------------------------
# Control probes (5) — would fail under a broken implementation
# ---------------------------------------------------------------------------


def test_edit_preserves_evidence(skills_env):
    """Control probe 1: a full ``content=`` rewrite that OMITS the evidence
    key must NOT drop the existing evidence block. A broken implementation
    that skips preservation fails this probe because the rewritten SKILL.md
    would carry no ``evidence`` frontmatter at all.
    """
    from tools.skill_manager_tool import _edit_skill

    skill_md = _create("with-evidence", VALID_SKILL_WITH_EVIDENCE, skills_env)
    before = _read_fm(skill_md)
    assert "evidence" in before  # sanity: setup wrote the evidence

    new_content = """---
name: test-evidence-skill
description: skill with evidence for preservation tests
---

# Test Skill

Rewritten body — no evidence key in the new frontmatter.
"""
    r = _edit_skill("with-evidence", new_content)
    assert r["success"] is True, r

    after = _read_fm(skill_md)
    assert "evidence" in after, "evidence block was dropped by _edit_skill"
    # The whole evidence mapping must be byte-for-byte the same — counters,
    # steps, evolution, version metadata all preserved.
    assert after["evidence"] == before["evidence"]


def test_edit_preserves_evidence_when_description_changes(skills_env):
    """Control probe 2: changing the description (and the body) must still
    preserve the evidence mapping with counters intact. Catches a broken
    implementation that only preserves when the entire new frontmatter
    matches the old one.
    """
    from tools.skill_manager_tool import _edit_skill

    skill_md = _create("with-evidence-2", VALID_SKILL_WITH_EVIDENCE, skills_env)
    before = _read_fm(skill_md)
    assert before["evidence"]["success_count"] == 5
    assert before["evidence"]["fail_count"] == 1

    new_content = """---
name: with-evidence-2
description: a new description that overrides the old one
---

# Test Skill

Rewritten body with a different description.
"""
    r = _edit_skill("with-evidence-2", new_content)
    assert r["success"] is True, r

    after = _read_fm(skill_md)
    assert after["description"] == "a new description that overrides the old one"
    assert "evidence" in after
    # Counters and steps survived; the new description coexists with the
    # preserved evidence mapping.
    assert after["evidence"]["success_count"] == 5
    assert after["evidence"]["fail_count"] == 1
    assert after["evidence"]["steps"] == before["evidence"]["steps"]


def test_edit_without_evidence_untouched(skills_env):
    """Control probe 3: a skill with NO evidence block must not gain one from
    preservation. Catches a broken implementation that always injects a fresh
    (or empty) evidence block.
    """
    from tools.skill_manager_tool import _edit_skill

    skill_md = _create("no-evidence", VALID_SKILL_NO_EVIDENCE, skills_env)
    before = _read_fm(skill_md)
    assert "evidence" not in before  # sanity: setup had no evidence

    new_content = """---
name: no-evidence
description: skill without evidence (unchanged)
---

# Test Skill

Rewritten body — still no evidence key.
"""
    r = _edit_skill("no-evidence", new_content)
    assert r["success"] is True, r

    after = _read_fm(skill_md)
    assert "evidence" not in after, (
        "preservation injected an evidence block where none should exist")


def test_patch_preserves_evidence(skills_env):
    """Control probe 4: an old_string/new_string patch on SKILL.md must also
    preserve the evidence block. The body changes; the frontmatter (and
    therefore the evidence) is untouched by the patcher, but the evidence
    still has to survive the post-patch write.
    """
    from tools.skill_manager_tool import _patch_skill

    skill_md = _create("patch-evidence", VALID_SKILL_WITH_EVIDENCE, skills_env)
    before = _read_fm(skill_md)
    assert "evidence" in before

    r = _patch_skill(
        "patch-evidence",
        old_string="# Test Skill\n\nOriginal body.",
        new_string="# Test Skill\n\nPatched body via old/new string.",
    )
    assert r["success"] is True, r

    after = _read_fm(skill_md)
    assert "evidence" in after
    assert after["evidence"] == before["evidence"]
    # And the body change actually landed.
    assert "Patched body via old/new string." in skill_md.read_text(encoding="utf-8")


def test_bom_preserved_with_evidence(skills_env):
    """Control probe 5: a UTF-8 BOM in the new content must be re-emitted AND
    the evidence block must survive. Catches an implementation that strips
    the BOM (because the file is read with utf-8-sig) or that loses evidence
    in the process.
    """
    from tools.skill_manager_tool import _edit_skill

    skill_md = _create("bom-skill", VALID_SKILL_WITH_BOM_AND_EVIDENCE, skills_env)
    before_text = skill_md.read_text(encoding="utf-8")
    assert before_text.startswith("\ufeff")  # sanity: setup had a BOM
    before = _parse_frontmatter(before_text.lstrip("\ufeff"))[0]
    assert "evidence" in before

    # New content also starts with the BOM; description is changed so the
    # re-emitted frontmatter is observably different from a no-op.
    new_content = (
        "\ufeff"
        + """---
name: bom-skill
description: updated description for BOM probe
---

# BOM Skill

Body changed in the rewrite.
"""
    )
    r = _edit_skill("bom-skill", new_content)
    assert r["success"] is True, r

    after_text = skill_md.read_text(encoding="utf-8")
    assert after_text.startswith("\ufeff"), "BOM was dropped by _edit_skill"
    after = _parse_frontmatter(after_text.lstrip("\ufeff"))[0]
    assert "evidence" in after
    assert after["evidence"] == before["evidence"]
    assert after["description"] == "updated description for BOM probe"


# ---------------------------------------------------------------------------
# Regression probes (2) — the existing paths must still work
# ---------------------------------------------------------------------------


def test_evidence_merge_still_updates_counters(skills_env):
    """Regression: the explicit evidence_merge path must STILL update
    counters and append steps. This is the additive-intent, counter-updating
    path; preservation is suspended during it so the freshly merged block
    is written as-is.
    """
    from tools.skill_manager_tool import skill_manage

    skill_md = _create("merge-regression", VALID_SKILL_WITH_EVIDENCE, skills_env)
    before = _read_fm(skill_md)
    assert before["evidence"]["success_count"] == 5
    assert before["evidence"]["fail_count"] == 1
    assert len(before["evidence"]["steps"]) == 1

    # Bump success counter by 3, fail counter by 2, add a new step.
    r = json.loads(
        skill_manage(
            action="patch",
            name="merge-regression",
            evidence_merge={
                "success_count": 3,
                "fail_count": 2,
                "steps": [{"name": "beta", "ok": 3, "fail": 2}],
            },
        )
    )
    assert r["success"] is True, r
    assert "Evidence merged" in r.get("message", "")

    after = _read_fm(skill_md)
    assert after["evidence"]["success_count"] == 5 + 3
    assert after["evidence"]["fail_count"] == 1 + 2
    # Both steps are present (existing alpha unchanged, new beta appended).
    step_names = sorted(s["name"] for s in after["evidence"]["steps"])
    assert step_names == ["alpha", "beta"]
    # The alpha step is unchanged.
    alpha = next(s for s in after["evidence"]["steps"] if s["name"] == "alpha")
    assert alpha == {"name": "alpha", "ok": 5, "fail": 1}


def test_evidence_merge_either_or_still_enforced(tmp_path, monkeypatch):
    """Regression: the EITHER_OR preflight must still reject ``evidence_merge``
    combined with ``content`` on the same call. The check fires inside the
    write-gate preflight, so this test enables the gate (skills.write_approval)
    and does NOT bypass it — the other tests bypass the gate so mutations
    can run synchronously, but the EITHER_OR rule is enforced there.

    The setup writes the skill directly to disk (no skill_manage call) so the
    pre-existing skill is in place without going through the now-active gate.
    """
    from agent import skill_utils
    from tools import write_approval as wa
    from tools import skill_ledger, skill_manager_tool, skill_usage
    from tools.skill_manager_tool import skill_manage

    home = tmp_path / "home"
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)

    monkeypatch.setattr(skill_ledger, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_usage, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skills_dir])
    # Gate ON, bypass OFF — the preflight must run.
    monkeypatch.setattr(wa, "write_approval_enabled", lambda s: True)
    assert skill_manager_tool._skill_gate_bypass.get() is False

    # Place the skill directly so setup doesn't have to clear the gate.
    skill_dir = skills_dir / "either-or"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(VALID_SKILL_NO_EVIDENCE, encoding="utf-8")

    r_raw = skill_manage(
        action="patch",
        name="either-or",
        content="---\nname: either-or\ndescription: trying to mix shapes\n---\n\n# Either Or\n",
        evidence_merge={"success_count": 1},
    )
    # The gate preflight returns a dict (via _err), the staging path returns
    # a JSON string. Normalize so the assertion checks the payload, not the
    # wrapping.
    r = json.loads(r_raw) if isinstance(r_raw, str) else r_raw
    assert r["success"] is False, r
    # The preflight EITHER_OR message (from skill_manager_tool._apply_skill_write_gate).
    err = r.get("error", "").lower()
    assert "either" in err and ("content" in err or "or" in err), r
