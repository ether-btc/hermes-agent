"""OpenCode provider profiles (Zen + Go).

Both route api_mode per model in core; these profiles carry the
chat_completions reasoning translations (GLM-5.2, Kimi K2, DeepSeek, Ox Alpha).
"""

from typing import Any

from agent import reasoning_effort as re_
from agent.gemini_native_adapter import GeminiAPIError, GeminiNativeClient
from hermes_cli.version_info import get_version_info
from providers import register_provider
from providers.base import ProviderProfile
from datetime import UTC

# Attribution headers (same values as OpenRouter / Vercel / Fireworks); via
# default_headers so they survive model switches and credential rotation.
_ATTRIBUTION_HEADERS = {
    "HTTP-Referer": "https://hermes-agent.nousresearch.com",
    "X-Title": "Hermes Agent",
    "User-Agent": f"HermesAgent/{get_version_info().base_version}",
}


def _flat_model_name(model: str | None) -> str:
    """Bare OpenCode model ID, tolerating aggregator prefixes."""
    return (model or "").strip().rsplit("/", 1)[-1].lower()


# Version-less DeepSeek ids that still carry the thinking/effort knobs on this wire: the retired
# ``deepseek-reasoner`` alias and the canonical ``deepseek-flash`` (2026-09 Flash refresh), for
# which the Go relay honours the same top-level ``reasoning_effort``/``thinking`` contract.
_THINKING_CAPABLE_IDS: frozenset[str] = frozenset({"deepseek-reasoner", "deepseek-flash"})


def _is_deepseek_thinking_model(model: str | None) -> bool:
    m = _flat_model_name(model)
    return (m.startswith("deepseek-v") and not m.startswith("deepseek-v3")) or m in _THINKING_CAPABLE_IDS


def _is_glm_5_2_model(model: str | None) -> bool:
    """GLM-5.2 across alias spellings (glm-5.2 / glm-5-2 / glm-5p2)."""
    m = _flat_model_name(model)
    return any(token in m for token in ("glm-5.2", "glm-5-2", "glm-5p2"))


class OpenCodeGoGeminiClient(GeminiNativeClient):
    """Gemini native transport over OpenCode Go's inference endpoint (Bearer auth)."""

    def __init__(self, *, base_url: Any = None, default_headers: Any = None, **kwargs: Any) -> None:
        merged_headers = dict(default_headers or {})
        # OpenCode Go requires a stable session id and a client-identifying header on
        # every request; merge after the caller's headers so caller-pinned values win.
        try:
            from agent.opencode_affinity import opencode_session_headers

            session_headers = opencode_session_headers("opencode-go", str(base_url or ""))
            for key, value in session_headers.items():
                merged_headers.setdefault(key, value)
        except Exception:
            pass
        effective_base = base_url or "https://opencode.ai/inference/go/v1"
        super().__init__(base_url=effective_base, default_headers=merged_headers, **kwargs)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _http_error(self, response: Any, body_text: Any = None) -> GeminiAPIError:
        from agent.gemini_native_adapter import gemini_http_error

        return gemini_http_error(response, body_text=body_text, base_url=self.base_url, key_guidance=False)
class OpenCodeGoProfile(ProviderProfile):
    """OpenCode Go - model-specific reasoning controls."""

    # The relay's default max_tokens (262144) exceeds what Xiaomi accepts for
    # mimo-v2.5-pro and 400s; keys are normalized via _flat_model_name().
    _MODEL_MAX_TOKENS: dict[str, int] = {"mimo-v2.5-pro": 131072}

    def get_max_tokens(self, model: str | None) -> int | None:
        cap = self._MODEL_MAX_TOKENS.get(_flat_model_name(model))
        return self.default_max_tokens if cap is None else cap

    def create_client(self, **client_kwargs: Any) -> Any:
        model = str(client_kwargs.pop("model", "") or "").strip().lower()
        base_url = str(client_kwargs.get("base_url") or "").strip()
        if not str(client_kwargs.get("api_key") or "").strip():
            return None
        is_gemini = model.startswith("gemini") or "/inference/" in base_url.lower()
        if not is_gemini:
            return None
        effective_base = base_url if ("/inference/" in base_url.lower() and "opencode.ai" in base_url.lower()) else "https://opencode.ai/inference/go/v1"
        # Keep both caller and profile headers, then add OpenCode session affinity.
        merged_headers = dict(self.default_headers or {})
        existing_headers = client_kwargs.get("default_headers")
        if isinstance(existing_headers, dict):
            merged_headers.update(existing_headers)
        try:
            from agent.opencode_affinity import opencode_session_headers

            for key, value in opencode_session_headers("opencode-go", effective_base).items():
                merged_headers.setdefault(key, value)
        except Exception:
            pass
        kwargs = {k: v for k, v in client_kwargs.items() if k not in {"default_headers", "model", "api_key", "base_url", "timeout", "http_client"}}
        return OpenCodeGoGeminiClient(
            api_key=client_kwargs.get("api_key", ""),
            base_url=effective_base,
            default_headers=merged_headers,
            timeout=client_kwargs.get("timeout"),
            http_client=client_kwargs.get("http_client"),
            **kwargs,
        )

    def fetch_account_usage(self, *, base_url: str | None = None, api_key: str | None = None):
        """Go subscription windows for /usage via ``/zen/go/v1/usage`` (anomalyco/opencode#16513).

        Percent-based payload ``{"usage": {"rolling"|"weekly"|"monthly": {"percent", "resetsAt"}}}``.
        Literal endpoint, NOT the runtime base_url: that one loses its /v1 suffix in
        anthropic_messages mode and /usage only exists under /v1.
        """
        from datetime import datetime, timezone

        import httpx

        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=self.name, explicit_base_url=base_url, explicit_api_key=api_key)
        token = str(runtime.get("api_key", "") or "").strip()
        if not token:
            return None
        with httpx.Client(timeout=10.0) as client:
            response = client.get("https://opencode.ai/zen/go/v1/usage",
                                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            response.raise_for_status()
        usage = (response.json() or {}).get("usage") or {}
        windows = []
        for key, label in (("rolling", "Rolling window"), ("weekly", "Weekly"), ("monthly", "Monthly")):
            window = usage.get(key) or {}
            if window.get("percent") is None:
                continue
            reset_raw = str(window.get("resetsAt") or "").replace("Z", "+00:00")
            reset_at = datetime.fromisoformat(reset_raw) if reset_raw else None
            windows.append(AccountUsageWindow(label=label, used_percent=float(window["percent"]), reset_at=reset_at))
        return AccountUsageSnapshot(provider=self.name, source="go_usage_api",
                                    fetched_at=datetime.now(UTC), windows=tuple(windows))

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if _is_glm_5_2_model(model):
            # Native reasoning_effort knob (high/max); server default when unset/disabled.
            effort = re_.requested_effort(reasoning_config)
            if effort is None or effort == "none":
                return {}, {}
            clamped = re_.clamp_effort(effort, re_.GLM52_EFFORTS, re_.GLM52_OVERRIDES)
            return {}, {"reasoning_effort": clamped if clamped in re_.GLM52_EFFORTS else "high"}
        if _flat_model_name(model).startswith("kimi-k2"):
            if not isinstance(reasoning_config, dict):
                return {}, {}
            return re_.thinking_toggle_extras(reasoning_config, re_.KIMI_K2_EFFORTS)
        if _is_deepseek_thinking_model(model):
            return re_.thinking_toggle_extras(reasoning_config, re_.DEEPSEEK_V4_EFFORTS, re_.DEEPSEEK_V4_OVERRIDES)
        return {}, {}


class OpenCodeZenProfile(ProviderProfile):
    """OpenCode Zen - model-specific reasoning controls."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return re_.ox_alpha_reasoning_extras(reasoning_config, model)


opencode_zen = OpenCodeZenProfile(
    name="opencode-zen", aliases=("opencode", "opencode_zen", "zen"), env_vars=("OPENCODE_ZEN_API_KEY",),
    base_url="https://opencode.ai/zen/v1", default_headers=dict(_ATTRIBUTION_HEADERS),
    default_aux_model="gemini-3-flash",
)

opencode_go = OpenCodeGoProfile(
    name="opencode-go", aliases=("opencode_go", "go", "opencode-go-sub"), env_vars=("OPENCODE_GO_API_KEY",),
    base_url="https://opencode.ai/zen/go/v1", default_headers=dict(_ATTRIBUTION_HEADERS),
    default_aux_model="glm-5",
    # The Go relay's upstream validates tool content as a strict string: list-type tool
    # content (native vision embeds) 422s with ``messages.N.tool.content.str Input should
    # be a valid string`` (Console Go, #104731) or 400s ``text is not set`` (MiMo, #47026),
    # and the rejected row stays in history so every later call dies too. Images in user
    # messages are fine, so vision itself keeps working via the text-summary downgrade.
    supports_vision_tool_messages=False,
)

register_provider(opencode_zen)
register_provider(opencode_go)
