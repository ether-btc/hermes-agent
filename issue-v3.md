## Proposal: add `/model <name> --chat` (per-chat sticky model selection), opt-in via `model.persist_chat_by_default`

### What I'm asking for

A new `/model` scope that survives conversation boundaries (explicit `/new`, idle/daily/suspended auto-reset, compression-exhausted reset, `/resume` to a non-existent target, `/branch`) **without** surviving the chat itself, and a parallel opt-in config knob that flips this on as the default for plain `/model`.

Concretely:

- New flag: `/model <name> --chat` — sticky per session_key (the per-chat routing entry). Persists across `/new`, `/resume`, auto-reset, etc.
- Existing flags keep their meaning: `/model <name>` (session-only, today), `/model <name> --once` (next turn), `/model <name> --session` (explicit session-only), `/model <name> --global` (config.yaml + every chat).
- New config knob: `model.persist_chat_by_default: true` — when true, plain `/model <name>` behaves like `/model <name> --chat`. Default `false` so today's behavior is preserved.

### Why I'm framing it this way

After reading `main` and the prior decisions, the current behavior is **deliberate**, not an oversight:

- `gateway/run.py:26735-26790` (`_clear_conversation_scope`) is documented as *"THE single conversation-boundary funnel. Call this — and nothing else — whenever a session_key crosses a conversation boundary: /new, /resume, auto-reset (idle/daily/suspended), expiry finalization, and the compression-exhausted auto-reset."* Its docstring lists every kind of `boundary X forgot dict Y` drift bug — `#48031, #58403, #10702, #35809` — and explains the funnel exists *because* per-attribute pop-lists drift.
- `gateway/session.py:867-872` documents `_session_model_overrides` as conversation-scoped. The field is sanitized on persist (`sanitize_model_override`, `:759`) and never writes `api_key` to disk.
- `gateway/session.py:2342-2361` (`set_expiry_finalized`) defaults to `clear_model_override=True`, comment: *"Session finalization is a conversation boundary — drop the persisted /model override too."*
- `gateway/run.py:19175-19198` consumes `was_auto_reset` to funnel-clear everything via `_clear_conversation_scope("auto_reset")` — it's the very mechanism that closes #48031.
- The docs at `website/docs/reference/cli-commands.md:222` say: *"By default, /model changes apply to the current session only. Add `--global` to persist the change to config.yaml (or set `model.persist_switch_by_default: true` to make every switch persist)."*
- The existing flag axis at `gateway/slash_commands.py:1756-1758` is already `--once` / `--session` / `--global` — `--chat` fits the same matrix.

So this is a proposed UX contract *extension* (a third sticky scope), not a bug report. I'm filing it as a feature request so it lands as a design discussion instead of a triage-sweeper `cannot_reproduce` / `incoherent` close.

### Why a fourth scope (instead of changing the default of `/model`)

There are three precedents that argue against flipping the default of plain `/model`:

1. **`--global` already exists** for users who want sticky (`/model <name> --global` persists to `config.yaml`, `gateway/slash_commands.py:2137-2139, 2476-2478`). Changing the default would change the meaning of every existing user's `/model`.
2. **`channel_overrides` already exists** for operators who want per-chat sticky (`gateway/run.py:8130-8166` resolves `_get_channel_override` from config and applies it before the session override). It's the right tool for *operator-set* per-chat routing, but it requires a config edit + restart and lives in a config file the user may not own.
3. **`model.persist_switch_by_default` already exists** as the opt-in config knob that makes plain `/model` behave like `--global`. A parallel `model.persist_chat_by_default` is the idiomatic shape here — same name pattern, same opt-in semantics, same code paths.

The gap is **per-chat sticky set by the user at runtime** — neither `--global` (host-wide) nor `channel_overrides` (operator-set) nor today's `--session` (drops on `/new`) cover it.

### Why `--chat` and not "auto-stick everything"

Threads share one session_key across all participants by default — `build_session_key(thread_sessions_per_user=False)` at `gateway/session.py:1093`, with the comment *"When thread_sessions_per_user is False (default), threads are shared across all participants — user_id is NOT appended, so every user in the thread shares a single session. This is the expected UX for threaded conversations (Telegram forum topics, Discord threads, Slack threads)."*

If `/model <name>` started silently persisting chat-sticky, every participant in a shared thread would re-route (and re-bill) onto the model the first user picked. That is a cost or privacy surprise the user did not sign up for. Two consequences:

- **`/model <name> --chat` is explicit** — the user typed the scope they want. No silent re-billing of other participants.
- **`model.persist_chat_by_default: true` is operator opt-in** — same as `model.persist_switch_by_default`. An admin deploying to a single-user deployment (personal Telegram bot, dedicated Discord server) can flip it; a multi-tenant deployment shouldn't.

### Reproduction

1. `~/.hermes/config.yaml`: `model.default: anthropic/claude-opus-4-6`. No `channel_overrides` for the chat under test.
2. Telegram chat connected to the gateway (group or DM).
3. In that chat: `/model anthropic/claude-sonnet-4-6`. Send a user message. Confirm the turn ran on Sonnet (`state.db → sessions.model` for that row, response header).
4. In the same chat: `/new`.
5. Send another user message.
6. **Expected:** Sonnet (sticks). **Actual:** Opus (drops).

Same symptom on every conversation boundary:

- Idle / daily / suspended auto-reset (`gateway/session.py:2504 get_reset_policy` evaluates both policies; gateway/run.py:13959-13965 handles expiry and calls `set_expiry_finalized` + `_clear_conversation_scope`).
- Compression-exhausted auto-reset (`gateway/run.py:20764-20766`, calls `reset_session` + `_clear_conversation_scope`).
- `/resume` to a non-existent target (auto-reset path).
- `/branch` (`gateway/slash_commands.py:5242`, calls `switch_session`).
- API-server `POST /api/sessions` omitting the `model` field.

I exercised this on Telegram. The shared gateway code path means Discord/Slack/WhatsApp almost certainly behave identically, but I haven't confirmed each one — flagging this honestly so reviewers don't have to re-verify.

### Root cause (five sites + the funnel)

There are **five** constructors and **one** funnel that drop the override on a conversation boundary. The funnel is by design (see above); the constructors are by oversight.

**1. `gateway/session.py:3419-3429 reset_session`** (used by explicit `/new`, `/reset`, and `compression_exhausted` reset)

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
# model_override is NOT carried over
```

**2. `gateway/session.py:2895-2908 get_or_create_session`** (used by idle/daily/suspended/resume_pending_expired auto-reset)

```python
candidate = SessionEntry(
    session_key=session_key,
    session_id=session_id,
    created_at=now,
    updated_at=now,
    origin=source,
    display_name=source.chat_name,
    platform=source.platform,
    chat_type=source.chat_type,
    was_auto_reset=was_auto_reset,
    auto_reset_reason=auto_reset_reason,
    reset_had_activity=reset_had_activity,
    prev_session_id=prev_session_id,
)
# model_override is NOT carried over
```

**3. `gateway/session.py:3555-3572 switch_session`** (used by `/resume` at `gateway/slash_commands.py:4994` and `/branch` at `gateway/slash_commands.py:5242`)

```python
new_entry = SessionEntry(
    session_key=session_key,
    session_id=target_session_id,
    created_at=now,
    updated_at=now,
    origin=old_entry.origin,
    display_name=old_entry.display_name,
    platform=old_entry.platform,
    chat_type=old_entry.chat_type,
)
# model_override is NOT carried over
```

**4. `gateway/session.py:2342-2361 set_expiry_finalized`** (called by the expiry watcher)

```python
def set_expiry_finalized(self, entry, *, clear_model_override=True):
    with self._lock:
        entry.expiry_finalized = True
        if clear_model_override:
            # Session finalization is a conversation boundary — drop the
            # persisted /model override too …
            entry.model_override = None
        self._save()
```

Already takes a `clear_model_override=False` opt-out — used by callers that need today’s behavior.

**5. `gateway/run.py:19184-19188` (auto-reset cleanup hook)** + `gateway/run.py:13959-13965` (expiry funnel) + `gateway/run.py:20764-20766` (compression-exhausted funnel) all call `_clear_conversation_scope(session_key, reason=…)`. The funnel clears every conversation-scoped dict at once (and clears the in-memory `_session_model_overrides[session_key]`) — which is correct for *every* other conversation-scoped dict but wrong for the one the user explicitly asked to be sticky.

**Permanent sites (don't change):** `set_expiry_finalized(True)` call at `gateway/run.py:13964` and the funnel itself remain unchanged; the fix below is gated on the new `model.persist_chat_by_default` config, so today’s behavior is preserved when the operator hasn’t opted in.

### The runtime path

The runtime rehydration path is `gateway/run.py:26499 _rehydrate_session_model_override`, which reads `SessionStore.get_model_override(session_key)` → `SessionEntry.model_override` → `sessions.json` (`gateway/session.py:3106-3114`). `api_key` is never persisted (`sanitize_model_override`, `:759`); only `model` / `provider` / `base_url` survive. So once `SessionEntry.model_override` is set on a per-chat entry, the next turn after `/new` / auto-reset / `/resume` rehydrates it the same way it rehydrates across a gateway restart. **There is no schema change and no state.db change.** `state.db → sessions.model` is the dashboard mirror written by `/model` itself (`gateway/slash_commands.py:2294`) — that's a separate concern.

### Proposed shape

Three options, smallest-to-largest. I prefer **B+** (config knob + flag); the maintainers may prefer just the flag.

**A. Just the flag (`/model <name> --chat`).**

- New flag accepted by `parse_model_switch_args` (`hermes_cli/model_switch.py`).
- New persistence path in `gateway/slash_commands.py` `_handle_model_command`: when the flag is set, call `set_model_override` AND set `SessionEntry.model_override` (so `reset_session`, `get_or_create_session`, `switch_session` inherit it via the constructors above — change the constructors to `model_override=old_entry.model_override`).
- Net diff: ~+30/-10 across 4 files. New public surface: one new flag. No config knob.

**B. Config knob + flag (`model.persist_chat_by_default`, default `false`).** (Recommended)

- Everything in A.
- New config knob in `config_defaults.py` and parsed in `gateway/run.py` near the existing `model.persist_switch_by_default` handling.
- When `model.persist_chat_by_default` is `true`, plain `/model <name>` (no flag) is rewritten to `/model <name> --chat` before dispatch. When `false`, today's behavior is preserved.
- Net diff: A + ~+15/-3 across 2 files.

**C. Flip the default of plain `/model` to chat-sticky.**

- Today's `--global` users get chat-sticky by surprise. Today's session-only users get chat-sticky by surprise. The blast radius is every existing user.
- I don't think this lands without a major-version bump and a migration warning. I list it only for completeness.

**Important design constraint that argues for A/B over the "smallest 4-site fix" I sketched in v1 of this draft:** `_clear_conversation_scope` (`gateway/run.py:26735-26790`) is documented as a *single funnel* with a per-attribute pop-list (`_CONVERSATION_SCOPED_STATE`) that has been the source of every `#48031, #58403, #10702, #35809` drift bug. Carving `_session_model_overrides` out of that funnel (so it survives `_clear_conversation_scope` but every other dict still drops) is the kind of partial-exception that causes the drift class it was built to prevent. **The clean shape is to gate the *constructors* on the new opt-in, not to crack open the funnel.** Sites 1-3 (`reset_session`, `get_or_create_session`, `switch_session`) all take `old_entry` / a previously-existing entry; carrying `model_override=old_entry.model_override` through is the minimal, funnel-respecting change.

### What I'm NOT proposing

- **Not** a change to today's behavior for users who don't opt in. `model.persist_chat_by_default` defaults to `false`; plain `/model` keeps meaning "this session only".
- **Not** a new `HERMES_*` env var (per the contribution rubric).
- **Not** a change to `state.db` schema. The fix lives entirely in the routing entry (`sessions.json` + `SessionEntry.model_override`); `state.db` continues to be the dashboard / billing ledger and continues to be written by `/model`.
- **Not** a change to `_clear_conversation_scope`. The funnel stays whole.
- **Not** changes to the cache- or summarization-safety contract. `_evict_cached_agent` already runs on every conversation boundary; nothing the new entry inherits can leak across cached prefixes.
- **Not** changes to `channel_overrides` semantics. That config knob already does *operator-set* per-chat stickiness; `/model --chat` is the *user-set* equivalent and shouldn't behave worse.
- **Not** changes to thread sharing semantics. The `thread_sessions_per_user=False` default is the right UX for forum topics / Discord threads / Slack threads, and I'm explicitly not asking for sticky model to leak between participants — `--chat` is explicit and `persist_chat_by_default` is operator opt-in.

### Related prior work

- **#5343** (`/model --global` writes wrong key) — closed 2026-04-25. Unrelated.
- **#72838 + #69899, #72863, #72888** (channel_overrides display bug) — open / in-flight. Genuinely separate: those are about the *display* path showing the wrong model; this proposal is about the *runtime dispatch* path. They should converge on the same source-of-truth once both land, but they are not the same fix.
- **#73622** (show channel-specific model in /model and /stop) — closed (per `gh issue view 73622`). Same display-path theme as above; unrelated to dispatch.
- **#48031** — closed 2026-06-24; title: *"/model switch silently lost when first message after session auto-reset (was_auto_reset flag never consumed)"*. It fixed the inverse problem: a stale `was_auto_reset` flag was wiping the override even when the user had `/model`-switched *after* the auto-reset. The actual code that closes it (`gateway/run.py:19175-19198` "capture and immediately consume was_auto_reset") is now part of the very mechanism this proposal is asking to be softened for users who *want* per-chat stickiness. Worth a careful read of #48031's PR to see how the maintainers reasoned about the boundary, since any fix here will have to keep the #48031 invariant intact.
- **#58403** — closed 2026-07-04; title: *"/new doesn't reset model config — old session keeps using stale model after config change"*. **This is the inverse-direction prior:** the maintainers decided `/new` *should* drop the model on config change (and tagged it `sweeper:risk-session-state`, P2). That decision is about config-default changes (operator side) and reads as "model stays stale after operator changes config.yaml". This proposal is about user-explicit `/model <name>` (user side). They are different intents and shouldn't get merged at the code level — but #58403's existence tells me the maintainers have explicitly considered the survival side and intentionally rejected it *for the config-change case*, so the burden is on this proposal to show why the *user-switch case* deserves a different answer. The `--chat` flag is the cleanest way to draw that line.
- **#10702** — closed 2026-06-29; title: *"Fix gateway /resume leaking cached agent state across session switches"*. Cited by `_clear_conversation_scope` as one of the drift bugs the funnel exists to prevent. Unrelated to `/model` per se, but relevant context for why I am not proposing a partial-exception inside the funnel.

### Are you willing to submit a PR?

- [x] I'd like to fix this myself and submit a PR — happy to draft against (B) once the maintainers signal whether they want the config knob or just the flag.

---

*Pre-emptive note for reviewers: yes, `--global` and `channel_overrides` and `model.persist_switch_by_default` already exist. They cover operator-set per-chat stickiness (`channel_overrides`) and host-wide stickiness (`--global` / `persist_switch_by_default`). They do not cover user-set per-chat stickiness, which is what `--chat` adds. I'm explicit about this so the issue doesn't get closed as a duplicate of the `--global` path.*