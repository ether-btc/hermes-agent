## Proposal: add `/model <name> --chat` for per-chat sticky model selection

Add a fourth `/model` scope, `/model <name> --chat`, that survives conversation boundaries without becoming host-wide, plus an opt-in `model.persist_chat_by_default` setting that makes plain `/model` use that scope.

### Why this is a feature request, not a bug

The current session-scoped behavior is intentional. The docs say that `/model` applies to the current session unless `--global` (or `model.persist_switch_by_default`) is used (`website/docs/reference/cli-commands.md:222`), `SessionEntry.model_override` is explicitly documented as session-scoped (`gateway/session.py:865-872`), and `_clear_conversation_scope` is deliberately the single funnel for `/new`, `/resume`, auto-reset, expiry finalization, and compression-exhausted reset (`gateway/run.py:26735-26750`).

This request extends that UX contract rather than changing it. The existing matrix is next turn (`--once`), current conversation (`--session`, or plain `/model` today), and global config (`--global`); `--chat` would add the missing per-chat scope.

### Why the new scope must be explicit

Threads share one `session_key` across participants by default. `build_session_key(thread_sessions_per_user=False)` documents that participant IDs are omitted for shared threads, including Telegram forum topics, Discord threads, and Slack threads (`gateway/session.py:1090-1123`). If plain `/model` silently became chat-sticky, one participant could reroute—and potentially rebill—later turns for everyone sharing that thread.

That argues for both safeguards:

- `/model <name> --chat` makes the requested scope explicit.
- `model.persist_chat_by_default: true` is an operator opt-in. It can suit a personal deployment without changing behavior for multi-user deployments.

### Existing precedents and the remaining gap

Four existing precedents establish the shape but do not cover user-set, runtime per-chat stickiness:

1. `--global` persists a switch to `config.yaml`; the gateway already distinguishes global from session-only results (`gateway/slash_commands.py:2373-2399`, `2473-2478`). It is host-wide, not chat-local.
2. `channel_overrides` already provides operator-set per-chat routing. Runtime resolution gives a session `/model` override priority over `channel_overrides`, then the global default (`gateway/run.py:8056-8067`, `8076-8167`). It requires operator configuration rather than a user command.
3. `model.persist_switch_by_default` is the opt-in that makes plain `/model` global (`hermes_cli/model_switch.py:749-772`). A parallel `model.persist_chat_by_default` follows the established naming and opt-in semantics.
4. The CLI reference explicitly defines the current-session default and the `--global` alternative (`website/docs/reference/cli-commands.md:222-226`). It does not offer a scope between those two.

### Reproduction and honest uncertainty

1. Set `model.default: anthropic/claude-opus-4-6` in `~/.hermes/config.yaml`, with no `channel_overrides` entry for the test chat.
2. In a Telegram chat connected to the gateway, run `/model anthropic/claude-sonnet-4-6`.
3. Send a message and confirm the turn uses Sonnet (`state.db` → `sessions.model`, plus the response header).
4. Run `/new` in the same chat.
5. Send another message.
6. Expected for the requested chat-sticky scope: Sonnet. Current intentional behavior: Opus.

I reproduced this on Telegram. Discord, Slack, and WhatsApp are inferred from the shared gateway path; I have not reproduced each platform independently.

The same boundary behavior is visible in the shared paths for idle/daily/suspended auto-reset (`gateway/session.py:2785-2908`, consumed at `gateway/run.py:19175-19194`), expiry finalization (`gateway/run.py:13959-13966`), compression exhaustion (`gateway/run.py:20761-20768`), `/resume` (`gateway/slash_commands.py:4994-5003`), and `/branch` (`gateway/slash_commands.py:5241-5245`). `POST /api/sessions` without a `model` field is another conversation-creation path with no model carry-over (`gateway/platforms/api_server.py:4324-4364`, `4380-4391`).

### Root cause: five boundary sites and the funnel

Three `SessionEntry` construction paths create a new routing entry without carrying `model_override`:

1. `reset_session` (`gateway/session.py:3401-3429`), used by explicit reset/new flows and compression-exhausted reset.
2. `get_or_create_session`'s new candidate (`gateway/session.py:2891-2908`), used for new and auto-reset routing entries.
3. `switch_session` (`gateway/session.py:3534-3572`), used by `/resume` (`gateway/slash_commands.py:4994`) and `/branch` (`gateway/slash_commands.py:5242`).

Two cleanup sites then enforce the existing conversation boundary:

4. `set_expiry_finalized` defaults to `clear_model_override=True` and clears the persisted field (`gateway/session.py:2342-2361`). Its `False` opt-out is reserved for the existing give-up path.
5. Auto-reset, expiry finalization, and compression exhaustion call `_clear_conversation_scope` (`gateway/run.py:19175-19194`, `13959-13966`, `20761-20768`), which clears `_session_model_overrides` as part of `_CONVERSATION_SCOPED_STATE` (`gateway/run.py:2787-2802`).

That funnel should remain intact. Its contract explicitly exists to prevent per-boundary pop lists from drifting (`gateway/run.py:26738-26750`). Chat-stickiness should instead be represented in the persisted routing entry and carried through the three constructors only when the new scope is active.

### Runtime rehydration path

`_rehydrate_session_model_override` reads `SessionStore.get_model_override(session_key)` and restores the in-memory override (`gateway/run.py:26499-26555`; `gateway/session.py:3106-3113`). Persistence is already sanitized to `model`, `provider`, and `base_url`; credentials are excluded (`gateway/session.py:751-773`, `3084-3104`). The `/model` handler already writes the non-secret override through to the session store (`gateway/slash_commands.py:2317-2355`).

Once a chat-scoped `SessionEntry.model_override` is carried into the replacement entry, the existing runtime path can rehydrate it after `/new`, auto-reset, `/resume`, or a gateway restart. This requires no `state.db` schema change: `state.db → sessions.model` remains the dashboard/billing mirror updated by `/model` (`gateway/slash_commands.py:2283-2297`).

### Related prior work

- **#5343** — closed 2026-04-25; `/model --global` wrote the wrong key. Unrelated to per-chat scope.
- **#48031** — closed 2026-06-24; fixed a stale `was_auto_reset` flag wiping an override selected after auto-reset. The consuming path is now `gateway/run.py:19175-19194`; this proposal must preserve that invariant.
- **#58403** — closed 2026-07-04; established that `/new` should drop a stale model after an operator changes config. That intentional config-default behavior is why this request proposes an explicit user-selected scope instead of changing `/new` globally.
- **#72838** — open/in flight, with related **#69899**, **#72863**, and **#72888**; concerns `channel_overrides` display, not runtime dispatch.
- **#10702** — closed 2026-06-29; fixed cached agent state leaking across `/resume` and is cited by the boundary funnel. Relevant to preserving the funnel, not a duplicate.
- **#73622** — closed; concerned showing the channel-specific model in `/model` and `/stop`, again a display-path issue rather than dispatch scope.

### Proposed shape

#### A. Flag only

- Add `/model <name> --chat` to the shared parser in `hermes_cli/model_switch.py`.
- In `gateway/slash_commands.py::_handle_model_command`, persist the selected non-secret override as the chat-scoped value.
- Carry that value through the three `SessionEntry` constructors above.

#### B. Flag plus opt-in config (recommended)

- Everything in A.
- Add `model.persist_chat_by_default`, default `false`, beside the existing model persistence configuration.
- When enabled, plain `/model <name>` uses chat scope; when disabled, current behavior is unchanged.

#### C. Make plain `/model` chat-sticky by default

This would surprise both existing session-only users and users who intentionally distinguish chat-local from `--global`. I do not recommend it.

### Why this is narrow

This extends the existing `/model` parser, config, and routing-entry persistence paths: one flag, one `config.yaml` opt-in, and constructor carry-over. It adds no model tool, environment variable, schema migration, new state manager, or exception inside `_clear_conversation_scope`; it also leaves prompt caching, `channel_overrides`, and thread-sharing semantics unchanged.

### Are you willing to submit a PR?

- [x] Yes. I would submit option B once maintainers confirm they want the config knob as well as the flag.

---

*Pre-emptive note: `--global`, `channel_overrides`, and `model.persist_switch_by_default` cover host-wide persistence or operator-set routing. They do not provide user-set, runtime per-chat persistence; that is the specific gap `--chat` fills.*
