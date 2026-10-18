"""Tests for the self-audit-correction-loop bundled skill.

Pure-prose skill (no scripts): these tests verify frontmatter invariants,
the presence of load-bearing claims in the body, and that referenced
upstream symbols still exist in the current codebase.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "software-development"
    / "self-audit-correction-loop"
)
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES_DIR = SKILL_DIR / "references"


def test_skill_directory_exists():
    assert SKILL_DIR.is_dir(), f"missing skill dir: {SKILL_DIR}"


def test_skill_md_exists():
    assert SKILL_MD.is_file(), f"missing SKILL.md: {SKILL_MD}"


def test_reference_files_exist():
    """Every file mentioned in the References section must exist on disk."""
    content = SKILL_MD.read_text(encoding="utf-8")
    # Extract markdown links under ## References
    ref_section = re.search(r"## References\n(.*?)(?:\n## |\Z)", content, re.DOTALL)
    if not ref_section:
        return  # no references section = nothing to check
    refs = re.findall(r"`([^`]+\.md)`", ref_section.group(1))
    for rel in refs:
        # References section uses repo-relative paths like "references/foo.md"
        if rel.startswith("references/"):
            rel = rel[len("references/") :]
        path = REFERENCES_DIR / rel
        assert path.is_file(), f"referenced file missing: {rel}"


def test_related_skills_entries_resolve_in_repo():
    """Each related_skills entry must correspond to an in-repo skill directory."""
    import hermes_yaml as yaml

    fm_content = SKILL_MD.read_text(encoding="utf-8")
    m = re.search(r"\n---\s*\n", fm_content[3:])
    fm = yaml.safe_load(fm_content[3 : m.start() + 3])
    related = (fm.get("metadata", {}).get("hermes", {})).get("related_skills") or []
    repo_root = Path(__file__).resolve().parents[2]
    for rs in related:
        # skill dirs live under skills/ or optional-skills/
        matches = list(repo_root.glob(f"skills/**/{rs}/SKILL.md")) + list(
            repo_root.glob(f"optional-skills/**/{rs}/SKILL.md")
        )
        assert matches, f"related_skill {rs!r} not found in-repo"


def test_no_machine_local_paths_in_references():
    """The same MACHINE_LOCAL regex from test_authoring_standards must not match in references."""
    MACHINE_LOCAL = re.compile(r"/home/(?!runner\b)[a-z0-9_-]+/|[A-Z]:\\+(?!<)")
    for ref in REFERENCES_DIR.glob("*.md"):
        text = ref.read_text(encoding="utf-8")
        m = MACHINE_LOCAL.search(text)
        assert not m, f"{ref.name}: machine-local path {m.group(0)!r}"


def test_body_contains_key_assertions():
    """Sanity-check that the body mentions the upstream symbols it cites."""
    content = SKILL_MD.read_text(encoding="utf-8")
    # Quick-reference table mentions
    for symbol in [
        "hermes prompt-size",
        "session_model_usage",
        "$HERMES_HOME/cron/executions.db",
        "MAX_TERMINAL_EXECUTIONS",
    ]:
        assert symbol in content, f"body missing expected symbol: {symbol}"

    # Procedure step 6 cites system_prompt.py volatile_parts
    assert "volatile_parts" in content, "body missing 'volatile_parts' reference"

    # Verification block cites hermes config check && hermes curator status
    assert "hermes config check" in content, "body missing 'hermes config check'"
    assert "hermes curator status" in content, "body missing 'hermes curator status'"


def test_upstream_symbols_still_exist():
    """Verify the upstream symbols cited in the skill still exist in this codebase."""
    repo_root = Path(__file__).resolve().parents[2]
    # hermes prompt-size CLI command
    prompt_size_file = repo_root / "hermes_cli" / "prompt_size.py"
    assert prompt_size_file.is_file(), "hermes_cli/prompt_size.py missing"

    # MAX_TERMINAL_EXECUTIONS constant
    cron_exec = repo_root / "cron" / "executions.py"
    cron_text = cron_exec.read_text(encoding="utf-8")
    assert "MAX_TERMINAL_EXECUTIONS" in cron_text, "MAX_TERMINAL_EXECUTIONS missing in cron/executions.py"

    # session_model_usage in state db models
    state_db_glob = list(repo_root.glob("**/*.py"))
    found_session_model = any(
        "session_model_usage" in p.read_text(encoding="utf-8", errors="ignore")
        for p in state_db_glob
    )
    assert found_session_model, "session_model_usage not found in any .py file"

    # hermes config check and hermes curator status commands
    console_engine = repo_root / "hermes_cli" / "console_engine.py"
    ce_text = console_engine.read_text(encoding="utf-8")
    assert '"config"' in ce_text, "'config' command missing in console_engine"
    assert '"curator"' in ce_text, "'curator' command missing in console_engine"

    # system_prompt.py volatile_parts
    sys_prompt = repo_root / "agent" / "system_prompt.py"
    sp_text = sys_prompt.read_text(encoding="utf-8")
    assert "volatile_parts" in sp_text, "volatile_parts missing in agent/system_prompt.py"

    # delegation live log path convention
    delegate_tool = repo_root / "tools" / "delegate_tool_dispatch.py"
    dt_text = delegate_tool.read_text(encoding="utf-8")
    assert "cache/delegation/live" in dt_text, "delegation live log path convention missing"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])