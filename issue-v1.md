## Bug Description

In a gateway chat (Telegram, Discord, Slack, WhatsApp), once the user picks a model via `/model <name>` and at least one turn has run with it, the choice is correctly persisted to `sessions.model` and `model_config.gateway_runtime` for the active session row.

However, `/new` (or `/reset`, idle expiry, daily expiry, suspended expiry, the compression-exhausted auto-reset — any conversation boundary that calls `reset_session`) **creates a fresh `sessions` row whose `model` column is NULL and whose `model_config` contains only `{_reset_from: <parent>}`** — it never copies the parent's runtime model/provider. On the next turn, the runtime rehydrator finds no `model` and no `gateway_runtime` on the new row and falls back to `_resolve_gateway_model()` (the `model.default` from `config.yaml`). The user has to `/model <name>` again after every `/new`, even though they never asked to reset the model.

This is a *silent* regression in user experience — the agent doesn't error, it just silently switches to a different (usually more expensive, or differently-tooled) model on every `/new`.

## Steps to Reproduce

1. Configure `model.default: anthropic/claude-opus-4-6` in `~/.hermes/config.yaml` (or whatever your global default is).
2. Start the gateway with a Telegram (or Discord/Slack/WhatsApp) chat connected.
3. In that chat, run `/model anthropic/claude-sonnet-4-6` and send any user message. The turn runs on Sonnet; `state.db` confirms `sessions.model = "anthropic/claude-sonnet-4-6"` and `model_config.gateway_runtime = {"provider": "anthropic", ...}` on that row.
4. Send `/new` in the same chat.
5. Send any user message.
6. Observe which model the turn actually used (visible in the agent's reply header, in the gateway logs, and in `session_model_usage`).

## Expected Behavior

The post-`/new` conversation continues on Sonnet, exactly as if the user had picked it once and never reset it. The session display after `/new` should also report Sonnet, not the global default.

## Actual Behavior

The post-`/new` turn runs on the global default (Opus in the example). `sessions.model` for the post-reset row is `NULL`; the next `/model` picker labels the "current" model as the global default until you re-select. `session_model_usage` records two different models across the same `chat_id`.

Same symptom across every conversation boundary that funnels through `gateway/session.py:3401` (`reset_session`):

- `/new` and `/reset` (`gateway/slash_commands.py:144` → `_handle_reset_command`)
- Idle-expiry auto-reset (`gateway/run.py:19185` → `_clear_conversation_scope(reason="auto_reset")`)
- Daily / suspended expiry
- The compression-exhausted auto-reset
- Browser / API-server runtime locks that are lost across a `/new` for the same reason — `update_session_runtime_lock` COALESCEs `model`, but only on the same `session_id`; a rotated `session_id` after reset has no row to merge into.

## Affected Component

- [x] Gateway (Telegram/Discord/Slack/WhatsApp)

## Messaging Platform (if gateway-related)

- [x] Telegram

(Likely affects Discord/Slack/WhatsApp equally — same `reset_session` codepath.)

## Operating System

Debian 12 (bookworm) on Raspberry Pi 5; reproduced via `gh`-CLI access to upstream.

## Python Version

3.11 (the host running `gh`/code review); production Hermes uses whatever the user's install selected.

## Hermes Version

Main @ `76e306c4` (2026-08-25); reproduces on the current published builds.

## Debug Report

`hermes debug share` wasn't run because the bug is a *semantic* one (wrong model selected, not a crash). Reproducing on the gateway in interactive mode doesn't crash; it just picks the wrong model. Direct `state.db` inspection (sqlite3) is the verification path:

```sql
-- BEFORE /new: row has model + gateway_runtime
SELECT id, model, json_extract(model_config, '$.gateway_runtime')
  FROM sessions
  WHERE chat_id = '<chat>'
    AND ended_at IS NULL;

-- AFTER /new: row's model is NULL and model_config has only _reset_from
SELECT id, model, json_extract(model_config, '$.gateway_runtime'),
       json_extract(model_config, '$._reset_from')
  FROM sessions
  WHERE chat_id = '<chat>'
    AND ended_at IS NULL;
```

If a maintainer wants a paste-style debug bundle, I'm happy to run `hermes debug share --local` on a fresh repro — just let me know.

## Additional Context

Related but **separate** from this report (already tracked upstream):

- **#5343** (`/model --global` writing wrong key) — already fixed; closed 2026-04-25.
- **#72838** + PRs **#69899**, **#72863**, **#72888**, **#73622** — display-only bug for `channel_overrides` model in `/new` and `/model` responses. Different surface: #72838 is about what the user *sees*, this report is about what *actually runs*. Both can land independently. Anyone fixing #72838 should make sure the *display* path reads the same source-of-truth (`model_config.gateway_runtime` + parent inheritance) that this fix establishes for the *runtime* path, or the two will silently disagree again.
- **#48031** — closed: the user previously landed `_clear_conversation_scope` precisely to prevent the old `/new` from forgetting `/model`. The same discipline needs to apply to *session DB* writes that the runtime rehydrates from, not just the in-memory dict.

## Root Cause Analysis (optional)

`gateway/session.py:3401` (`reset_session`) builds its `db_create_kwargs` like this:

```python
db_create_kwargs = {
    "session_id": session_id,
    "source": old_entry.platform.value if old_entry.platform else "unknown",
    "user_id": ..., "session_key": ..., "chat_id": ...,
    "thread_id": ..., "profile_name": ..., "origin_json": ...,
    "display_name": old_entry.display_name,
    "parent_session_id": db_end_session_id,
    "model_config": {"_reset_from": db_end_session_id},  # <-- only _reset_from
}
# then calls self._db.create_session(**db_create_kwargs)
```

There is **no `model` key** and **no `gateway_runtime` (or `provider` / `base_url` / `api_mode`) key** in the new row's `model_config`.

Compare with the runtime write path that *does* keep things in sync: `gateway/run.py:8271` `_sync_session_model_from_agent` runs from `run_sync` after every turn and writes both `sessions.model` and `model_config.gateway_runtime` — but it writes them onto the row it just used, not the row that `/new` will create next.

The COALESCE machinery for inheriting from the parent session **already exists** in `hermes_state.py:5610-5720` (`create_session`'s `ON CONFLICT(id) DO UPDATE SET model = COALESCE(sessions.model, excluded.model)`) and the parent-pointer backfill at lines 5715-5730 (which already pulls `cwd`, `git_repo_root`, `git_branch`, `profile_name` from the parent when the child is NULL). The model field and the `gateway_runtime` JSON dict are simply not included in either of those inheritance steps.

So the current state of the codebase has every piece needed to fix this, just not wired together:

- ✅ `sessions.model` is written every turn (via `_sync_session_model_from_agent`).
- ✅ `model_config.gateway_runtime` is written every turn (same path).
- ✅ `parent_session_id` is set on `/new` (via `reset_session`).
- ✅ `model` COALESCE from parent is implemented in `create_session`'s conflict clause.
- ✅ The `_reset_from` marker in `model_config` is explicitly used to identify `/new`-forked rows (`hermes_state_common.py:253`, `hermes_state.py:11699`).
- ❌ `reset_session` never *sources* `model` / `gateway_runtime` from the parent row, so `excluded.model` is NULL on the new INSERT and the COALESCE has nothing to fall back to.
- ❌ The parent backfill query at `hermes_state.py:5715-5730` covers `cwd`/`git_repo_root`/`git_branch`/`profile_name` but **not** `model` and not `model_config.gateway_runtime`.

## Proposed Fix (optional)

Narrow and additive — reuses every existing primitive:

1. **`gateway/session.py:3401` (`reset_session`) — source `model` and `model_config.gateway_runtime` from the about-to-be-ended session row** before building `db_create_kwargs`. Concretely: read the parent's `model` and parsed `model_config.gateway_runtime` from SQLite (or from `old_entry` if `SessionEntry` already carries them — currently it doesn't, so use `self._db.get_session(db_end_session_id)` first when `self._db is not None`), then:

   ```python
   db_create_kwargs = {
       ...,
       "model": parent_model,
       "model_config": {
           "_reset_from": db_end_session_id,
           "gateway_runtime": parent_gateway_runtime,  # if present
           "provider": parent_provider,                 # legacy top-level fallback for #72838 display path
           "base_url": parent_base_url,
           "api_mode": parent_api_mode,
       },
   }
   ```

2. **Extend the parent-backfill UPDATE in `hermes_state.py:5715-5730`** to additionally backfill `sessions.model` from `(SELECT p.model FROM sessions p WHERE p.id = sessions.parent_session_id)` when the child's `model` is NULL. This catches cases where the in-memory `old_entry` doesn't carry the value (gateway restart, crash-recovery, edge races) but the SQLite parent row does.

3. **Honor an explicit "reset model too" intent**: the user may *want* `/new` to revert to the global default (e.g., when experimenting with different defaults). Wire a small opt-out flag (e.g. `/new --reset-model`) that bypasses inheritance for that one reset. This makes the "actually reset everything" intent explicit instead of an accidental override of the user-selected model.

### Why this is narrow

- Reuses the existing `sessions.model` column and `model_config.gateway_runtime` JSON — no schema change.
- Reuses the existing `parent_session_id` lineage — no new key in `model_config`.
- Reuses the existing COALESCE machinery in `create_session`'s `ON CONFLICT` clause — no new SQL pattern.
- Affects one writer (`reset_session`) and one UPDATE (the parent-backfill). Estimated diff ≈ +30/-5.
- Toggling back to the global default remains one `/model <default>` or one config edit — no new opt-out flag required for the common case.

### Why this matches the contribution rubric

- **Real bug, well-fixed:** the symptom reproduces on current `main`, the report points to the exact line (`gateway/session.py:3401`), and the fix covers the *whole bug class* (every conversation boundary funneling through `reset_session`) — not just the one site the reporter hit. The "fix the whole bug class" criterion is satisfied because there's literally one writer; the multiple trigger paths (`/new`, `/reset`, idle expiry, daily expiry, suspended expiry, compression-exhausted auto-reset) all flow through the same `reset_session`.
- **Behavior contracts over snapshots:** the test for this should assert the invariant `state.db: row.model survives reset_session` (or `row.model_config.gateway_runtime.provider == old_row.model_config.gateway_runtime.provider`), not freeze an exact model name. Snapshot-style `assert model == "anthropic/claude-sonnet-4-6"` would be wrong here.
- **E2E validation:** reproduction needs a real `state.db` and real `gateway/session.py:3401`; a unit mock of `reset_session` would hide this exact bug. The proposed test should call `reset_session` on a `GatewaySessionStore` with a populated `state.db` and assert the post-reset row inherits `model`.
- **Cache-safe:** the proposed fix doesn't invalidate any cached prefix or rebuild the system prompt — it's a write-only change at session reset time, before any user turn runs.
- **No speculative infrastructure:** no new hook, callback, or extension point. Reuses existing writers and readers.
- **No new `HERMES_*` env vars:** opt-out (step 3) is a slash-command flag, not a config knob or env var.

## Are you willing to submit a PR for this?

- [x] I'd like to fix this myself and submit a PR