## Proposal: make `/model <name>` sticky per chat (routing key), not per conversation

### What I'm asking for

In a gateway chat, after I pick a model with `/model anthropic/claude-sonnet-4-6`, I want subsequent `/new` (and idle/daily/suspended auto-reset) to **keep using Sonnet** until I `/model` again or change the global default. Right now every conversation boundary drops the selection back to `model.default` from `config.yaml`, so I have to re-pick my model after every `/new` in a Telegram chat that lives across many sessions.

### What I understand about the current design

I've read through `main` and the maintainers' previous decisions on this surface. The current behavior is **deliberate**, not an oversight:

- `gateway/session.py:867-872` documents `_session_model_overrides` as conversation-scoped by design.
- `gateway/run.py:19184-19188` (the auto-reset cleanup) explicitly calls `_clear_conversation_scope` and comments: *"Treat auto-reset as a full conversation boundary — clear every conversation-scoped per-session dict in one funnel call so the fresh session does not inherit the previous conversation's model/reasoning overrides … (#48031, #58403)."*
- `gateway/session.py:2342-2360` (`set_expiry_finalized`) defaults to `clear_model_override=True`, with the comment *"Session finalization is a conversation boundary — drop the persisted /model override too."*
- The docs at `/model` say: *"By default, /model changes apply to the current session only."*

So the framing here is a **proposed UX contract change** (per-chat sticky rather than per-conversation), not a bug report. I'm filing it as a feature request rather than a bug because, after reading the existing comments and #48031/#58403, I believe the maintainers will want to engage with this as a design decision rather than a fix.

I want to make sure this lands productively, not as another triage-sweeper close on `cannot_reproduce` / `incoherent`. So this report includes the file/line trace, the reproduction recipe, and a proposed shape that fits the existing primitives — please tell me if any of it is wrong before forming a product opinion.

### Why I'm asking

If the answer is "we want it this way for privacy / cost / cross-chat-leak reasons and the answer is no," that's fine and I'll stop filing similar issues. But I think the current rule has costs the design didn't anticipate:

- A user running one chat per *project* in Telegram topics loses their model choice between any two `/new`s in the same topic.
- A user who picks Sonnet because Opus is broken for their task right now has to re-pick every time the idle timer fires (which it does aggressively — see the suspended-reset code path).
- The "switch your model" affordance becomes much less attractive as a power-user feature because the choice evaporates on the next boundary.

There's also an existing parallel: `channel_overrides` (config.yaml) is already *per-chat* and *persistent*. The `/model` command just isn't.

### Reproduction

1. `~/.hermes/config.yaml`: `model.default: anthropic/claude-opus-4-6`.
2. Telegram chat connected to the gateway.
3. In that chat: `/model anthropic/claude-sonnet-4-6` and send a user message. Confirm the turn ran on Sonnet (visible in the response header; `state.db → sessions.model` should be `"anthropic/claude-sonnet-4-6"`).
4. In the same chat: `/new`.
5. Send another user message.
6. Observe which model the turn ran on (response header; `state.db → sessions.model` for the post-`/new` row).

**Expected:** Sonnet. **Actual:** Opus (the config default).

Same symptom on:
- Idle expiry (default policy fires aggressively — see `gateway/run.py:13962` watcher)
- Daily expiry
- Suspended expiry
- `/resume` to a non-existent target
- `/branch` (`gateway/slash_commands.py:5165` — actively passes `model=self.config.get("model", {}).get("default")` to `create_session`, so the branch not only doesn't inherit the override, it explicitly writes the global default into the new row's `model` column)
- API-server `POST /api/sessions` when the request omits a model field

I exercised this on Telegram. The shared gateway code path means Discord/Slack/WhatsApp almost certainly behave identically, but I haven't confirmed each one — flagging this honestly so reviewers don't have to re-verify.

### Where the override gets dropped (root cause)

There are **four** sites that drop `SessionEntry.model_override` on a conversation boundary. `reset_session` is the loudest but not the only one.

**1. `gateway/session.py:3401` `reset_session`** (used by explicit `/new` / `/reset`)

```python
new_entry = SessionEntry(
    session_key=session_key,
    session_id=session_id,
    created_at=now,
    updated_at=now,
    origin=old_entry.origin,
    display_name=display_name if display_name is not None else old_entry.display_name,
    platform=old_entry.platform,
    chat_type=old_entry.chat_type,
    is_fresh_reset=True,
)
# NB: model_override is NOT carried over from old_entry
```

The new entry has `model_override=None`. `_save()` writes `sessions.json` without an override. Next turn: `_rehydrate_session_model_override` finds nothing, runtime falls back to `_resolve_gateway_model()` → config default.

**2. `gateway/session.py:2598` `get_or_create_session`** (used by idle/daily/suspended/resume_pending_expired auto-reset — line 2907 `candidate = SessionEntry(...)`)

Same pattern. The auto-reset `candidate = SessionEntry(...)` constructor omits `model_override=`. This is the path the `was_auto_reset` flag drives.

**3. `gateway/session.py:2342` `set_expiry_finalized`** (called by the expiry watcher)

```python
entry.expiry_finalized = True
if clear_model_override:                       # default True
    entry.model_override = None                # <-- explicit drop
self._save()
```

This is the explicit "drop on finalization" code. Comment says: *"Session finalization is a conversation boundary — drop the persisted /model override too."* This runs even when no fresh entry is being constructed — e.g., when a daily-expiry rolls over an entry that's still active but should logically be considered ended.

**4. `gateway/run.py:19184` (auto-reset cleanup hook)** calls `_clear_conversation_scope` which iterates `_CONVERSATION_SCOPED_STATE` and clears `_session_model_overrides`. That clears the **in-memory** override for the next dispatch within the same process — even if the SessionEntry override survived. So even if (1)-(3) carry `model_override` over, (4) wipes it from the runner's view.

The runtime rehydration path at `gateway/run.py:26499` (`_rehydrate_session_model_override`) reads from `session_store.get_model_override(session_key)`, which returns `SessionEntry.model_override`. So the *survives-a-restart* data lives in `sessions.json`; the *survives-a-/new* data is the same `SessionEntry.model_override` plus the in-memory `_session_model_overrides[session_key]` dict. Both have to carry over.

### Important corrections to similar prior reports

- **#5343** (`/model --global` writes wrong key) — closed 2026-04-25. Unrelated.
- **#72838 + #69899, #72863, #72888, #73622** (channel_overrides display bug) — open / in-flight. Genuinely separate: #72838 is about the *display* path showing the wrong model; this proposal is about the *runtime dispatch* path. They should converge on the same source-of-truth once both land, but they are not the same fix.
- **#48031** — closed 2026-06-24; title: *"/model switch silently lost when first message after session auto-reset (was_auto_reset flag never consumed)"*. It fixed the inverse problem: a stale `was_auto_reset` flag was wiping the override even when the user had `/model`-switched *after* the auto-reset. That fix is correctly scoped to its own bug — the actual code that closes it (`gateway/run.py:19175-19198` "capture and immediately consume was_auto_reset") is now the very mechanism that this proposal is asking to be softened for users who *want* per-chat stickiness. Worth a careful read of #48031's PR to see how the maintainers reasoned about the boundary, since any fix here will have to keep the #48031 invariant intact.

### Proposed shape

I'm presenting this as a UX contract change rather than a pure bug fix because the maintainers have clearly thought about this and decided against per-chat stickiness. Three pieces, smallest-to-largest:

**A. Tiny (just the missing-inheritance fix; user still has to `/model` once and it sticks):**

Stop dropping the override in the four sites above:

1. `reset_session`: pass `model_override=old_entry.model_override` into the new `SessionEntry(...)` constructor.
2. `get_or_create_session` (auto-reset path, line 2907): same.
3. `set_expiry_finalized`: add a parameter `clear_model_override: bool = False` (default off; pass `True` explicitly from any caller that needs today's "drop on finalize" behavior — and audit which of the current callers actually need it).
4. `_clear_conversation_scope`: when called from `was_auto_reset` cleanup (`gateway/run.py:19184`), *don't* clear `_session_model_overrides` if `session_entry.model_override` is set and the entry has been rehydrated from a fresh restart. (I.e., preserve the in-memory override only if the persisted override is also being preserved.)

Net diff: ~+15/-5 across three files. No schema, no config knob, no new public surface.

**B. Medium (add a per-chat-on / per-chat-off config knob so power users opt in explicitly):**

Add `model.persist_to_chat_by_default: true|false` (default: `false` to preserve today's contract). When `true`, every `/model` switch writes through to `SessionEntry.model_override` and the four sites above inherit the override. When `false`, today's behavior is preserved.

This lets users who *want* per-chat stickiness opt in once, and lets users on shared / kiosk / privacy-sensitive chats stay with conversation-scoped.

**C. Large (make per-chat stickiness the default, with an opt-out):**

Flip the default to `true`, add `model.persist_to_chat_by_default: false` for users who want today's behavior. Bigger blast radius — anyone whose privacy / cost assumptions relied on `/model` evaporating on `/new` would see a change.

**I would prefer (A) or (B).** I don't think the maintainers will want (C) without an explicit product decision and a migration plan.

### What I'm NOT proposing

- **Not** a new `HERMES_*` env var. (Per the contribution rubric in `AGENTS.md`.)
- **Not** a new core tool, hook, or extension point.
- **Not** changes to `state.db` schema. The fix lives entirely in the routing-entry (`sessions.json`) lifecycle; `state.db` is the dashboard / billing ledger and continues to record what actually ran.
- **Not** changes to the cache- or summarization-safety contract. `_evict_cached_agent` already runs on every conversation boundary; nothing the new entry inherits can leak across cached prefixes.
- **Not** changes to `channel_overrides` semantics. That config knob already does per-chat stickiness; `/model` is the user-facing equivalent and shouldn't behave worse.

### Are you willing to submit a PR?

- [x] I'd like to fix this myself and submit a PR — happy to draft against (A) or (B) once the maintainers signal which shape they prefer.