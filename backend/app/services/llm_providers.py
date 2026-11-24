"""LLM provider presets, thinking-level parameter mapping, and Anthropic protocol helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

LLMProviderId = Literal[
    "openai",
    "anthropic",
    "gemini",
    "deepseek",
    "openrouter",
    "qwen",
    "zhipu",
    "ollama",
    "custom",
]
LLMThinkingLevel = Literal["auto", "off", "low", "medium", "high"]
LLMProtocol = Literal["openai_compatible", "anthropic_messages"]
ThinkingStrategy = Literal[
    "openai_reasoning_effort",
    "gemini_reasoning_effort",
    "anthropic_thinking",
    "deepseek_thinking",
    "openrouter_reasoning",
    "dashscope_thinking",
    "zhipu_thinking",
]

VALID_LLM_PROVIDERS: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "openrouter",
        "qwen",
        "zhipu",
        "ollama",
        "custom",
    }
)
VALID_LLM_THINKING_LEVELS: frozenset[str] = frozenset({"auto", "off", "low", "medium", "high"})

_THINKING_BUDGET_TOKENS: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}


@dataclass(frozen=True, slots=True)
class LLMProviderPreset:
    """Metadata and protocol strategy for a supported LLM provider."""

    id: LLMProviderId
    name: str
    protocol: LLMProtocol
    default_base_url: str
    default_model: str
    models: tuple[str, ...]
    requires_api_key: bool
    supports_thinking: bool
    thinking_strategy: ThinkingStrategy

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "default_base_url": self.default_base_url,
            "default_model": self.default_model,
            "models": list(self.models),
            "requires_api_key": self.requires_api_key,
            "supports_thinking": self.supports_thinking,
        }


LLM_PROVIDER_PRESETS: tuple[LLMProviderPreset, ...] = (
    LLMProviderPreset(
        id="openai",
        name="OpenAI",
        protocol="openai_compatible",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        models=(
            "gpt-5",
            "gpt-5-mini",
            "o4-mini",
            "o3",
            "gpt-4.1",
            "gpt-4o",
            "gpt-4o-mini",
        ),
        requires_api_key=True,
        supports_thinking=True,
        thinking_strategy="openai_reasoning_effort",
    ),
    LLMProviderPreset(
        id="anthropic",
        name="Anthropic (Claude)",
        protocol="anthropic_messages",
        default_base_url="https://api.anthropic.com",
        default_model="claude-sonnet-4-5",
        models=(
            "claude-opus-4-1",
            "claude-sonnet-4-5",
            "claude-sonnet-4-0",
            "claude-3-7-sonnet-latest",
            "claude-3-5-haiku-latest",
        ),
        requires_api_key=True,
        supports_thinking=True,
        thinking_strategy="anthropic_thinking",
    ),
    LLMProviderPreset(
        id="gemini",
        name="Google Gemini",
        protocol="openai_compatible",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-2.5-flash",
        models=(
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ),
        requires_api_key=True,
        supports_thinking=True,
        thinking_strategy="gemini_reasoning_effort",
    ),
    LLMProviderPreset(
        id="deepseek",
        name="DeepSeek",
        protocol="openai_compatible",
        default_base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        models=(
            "deepseek-chat",
            "deepseek-reasoner",
        ),
        requires_api_key=True,
        supports_thinking=True,
        thinking_strategy="deepseek_thinking",
    ),
    LLMProviderPreset(
        id="openrouter",
        name="OpenRouter",
        protocol="openai_compatible",
        default_base_url="https://openrouter.ai/api/v1",
        default_model="openai/gpt-5-mini",
        models=(
            "openai/gpt-5",
            "openai/gpt-5-mini",
            "anthropic/claude-sonnet-4.5",
            "google/gemini-2.5-pro",
            "deepseek/deepseek-r1",
        ),
        requires_api_key=True,
        supports_thinking=True,
        thinking_strategy="openrouter_reasoning",
    ),
    LLMProviderPreset(
        id="qwen",
        name="Alibaba DashScope (Qwen)",
        protocol="openai_compatible",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        models=(
            "qwen3-max",
            "qwen-plus",
            "qwen-turbo",
            "qwq-plus",
        ),
        requires_api_key=True,
        supports_thinking=True,
        thinking_strategy="dashscope_thinking",
    ),
    LLMProviderPreset(
        id="zhipu",
        name="Zhipu AI (GLM)",
        protocol="openai_compatible",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4.5",
        models=(
            "glm-4.5",
            "glm-4.5-air",
            "glm-4-plus",
        ),
        requires_api_key=True,
        supports_thinking=True,
        thinking_strategy="zhipu_thinking",
    ),
    LLMProviderPreset(
        id="ollama",
        name="Ollama (Local)",
        protocol="openai_compatible",
        default_base_url="http://host.docker.internal:11434/v1",
        default_model="qwen3:14b",
        models=(
            "qwen3:14b",
            "deepseek-r1:14b",
            "llama3.1:8b",
        ),
        requires_api_key=False,
        supports_thinking=True,
        thinking_strategy="openai_reasoning_effort",
    ),
    LLMProviderPreset(
        id="custom",
        name="Custom OpenAI-Compatible",
        protocol="openai_compatible",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        models=(),
        requires_api_key=False,
        supports_thinking=True,
        thinking_strategy="openai_reasoning_effort",
    ),
)

_PRESETS_BY_ID: dict[str, LLMProviderPreset] = {p.id: p for p in LLM_PROVIDER_PRESETS}


def normalize_provider_id(value: str | None) -> LLMProviderId:
    """Normalize and validate an LLM provider ID."""
    cleaned = str(value or "openai").strip().lower()
    if cleaned not in _PRESETS_BY_ID:
        raise ValueError(f"Unsupported llm_provider: {value!r}")
    return cleaned  # type: ignore[return-value]


def normalize_thinking_level(value: str | None) -> LLMThinkingLevel:
    """Normalize and validate an LLM thinking level."""
    cleaned = str(value or "auto").strip().lower()
    if cleaned not in VALID_LLM_THINKING_LEVELS:
        raise ValueError(f"Unsupported llm_thinking_level: {value!r}")
    return cleaned  # type: ignore[return-value]


def get_provider_preset(provider_id: str | None) -> LLMProviderPreset:
    """Return provider preset metadata, falling back to openai if unknown."""
    key = str(provider_id or "openai").strip().lower()
    return _PRESETS_BY_ID.get(key, _PRESETS_BY_ID["openai"])


def list_provider_presets() -> list[dict[str, Any]]:
    """Return serializable metadata for all supported LLM providers."""
    return [preset.to_public_dict() for preset in LLM_PROVIDER_PRESETS]


def normalize_anthropic_base_url(base_url: str | None) -> str:
    """Strip trailing /v1 or /v1/messages since AsyncAnthropic appends /v1/messages."""
    raw = str(base_url or "https://api.anthropic.com").strip().rstrip("/")
    for suffix in ("/v1/messages", "/v1"):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)].rstrip("/")
            break
    return raw or "https://api.anthropic.com"


def _is_fixed_temperature_reasoning_model(model_name: str) -> bool:
    lower = model_name.strip().lower()
    return lower.startswith(("o1", "o3", "o4-", "gpt-5")) or "deepseek-reasoner" in lower


def build_openai_completion_kwargs(
    *,
    provider_id: str,
    model_name: str,
    thinking_level: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    include_response_format: bool = False,
    include_thinking: bool = True,
    include_temperature: bool = True,
) -> dict[str, Any]:
    """Build kwargs for client.chat.completions.create() with provider-aware thinking params.

    Note: ``response_format={"type": "json_object"}`` is never combined with ``tools`` because
    many OpenAI-compatible gateways (e.g., GLM/Zhipu, vLLM, Qwen, Ollama) suppress tool calling
    when JSON mode is active while still returning HTTP 200.
    """
    preset = get_provider_preset(provider_id)
    level = (
        str(thinking_level or "auto").strip().lower()
        if str(thinking_level or "").strip().lower() in VALID_LLM_THINKING_LEVELS
        else "auto"
    )

    has_tools = bool(tools)
    kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
    }
    if has_tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    elif include_response_format:
        kwargs["response_format"] = {"type": "json_object"}

    extra_body: dict[str, Any] = {}
    omit_temperature = False

    if include_thinking and level != "auto":
        strategy = preset.thinking_strategy
        if strategy in ("openai_reasoning_effort", "gemini_reasoning_effort"):
            if level == "off":
                kwargs["reasoning_effort"] = "none"
            else:
                kwargs["reasoning_effort"] = level
                omit_temperature = True
        elif strategy == "openrouter_reasoning":
            if level == "off":
                extra_body["reasoning"] = {"enabled": False}
            else:
                extra_body["reasoning"] = {"effort": level}
                omit_temperature = True
        elif strategy == "dashscope_thinking":
            if level == "off":
                extra_body["enable_thinking"] = False
            else:
                extra_body["enable_thinking"] = True
                extra_body["thinking_budget"] = _THINKING_BUDGET_TOKENS[level]
                omit_temperature = True
        elif strategy in ("deepseek_thinking", "zhipu_thinking"):
            if level == "off":
                extra_body["thinking"] = {"type": "disabled"}
            else:
                extra_body["thinking"] = {"type": "enabled"}
                omit_temperature = True

    if include_temperature and not omit_temperature and not _is_fixed_temperature_reasoning_model(
        model_name
    ):
        kwargs["temperature"] = 0.1

    if extra_body:
        kwargs["extra_body"] = extra_body

    return kwargs


def convert_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function tool schemas into Anthropic Messages API tool schemas."""
    converted: list[dict[str, Any]] = []
    for item in tools:
        fn = item.get("function") if isinstance(item, dict) else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        input_schema = fn.get("parameters")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        converted.append(
            {
                "name": name,
                "description": str(fn.get("description") or ""),
                "input_schema": input_schema,
            }
        )
    return converted


def convert_messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert OpenAI-style conversation history into (system_prompt, anthropic_messages)."""
    system_parts: list[str] = []
    anthropic_msgs: list[dict[str, Any]] = []

    idx = 0
    total = len(messages)
    while idx < total:
        msg = messages[idx]
        role = str(msg.get("role") or "")

        if role == "system":
            text = str(msg.get("content") or "").strip()
            if text:
                system_parts.append(text)
            idx += 1
            continue

        if role == "user":
            anthropic_msgs.append({"role": "user", "content": str(msg.get("content") or "")})
            idx += 1
            continue

        if role == "assistant":
            raw_blocks = msg.get("_anthropic_content_blocks")
            if isinstance(raw_blocks, list) and raw_blocks:
                anthropic_msgs.append({"role": "assistant", "content": raw_blocks})
                idx += 1
                continue

            blocks: list[dict[str, Any]] = []
            text = msg.get("content")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed_input = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(parsed_input, dict):
                        parsed_input = {}
                except Exception:
                    parsed_input = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tc.get("id") or "toolu_unknown"),
                        "name": str(fn.get("name") or ""),
                        "input": parsed_input,
                    }
                )
            anthropic_msgs.append({"role": "assistant", "content": blocks or ""})
            idx += 1
            continue

        if role == "tool":
            # Group consecutive tool results into one Anthropic user turn.
            tool_blocks: list[dict[str, Any]] = []
            while idx < total and str(messages[idx].get("role") or "") == "tool":
                tmsg = messages[idx]
                tool_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(tmsg.get("tool_call_id") or "toolu_unknown"),
                        "content": str(tmsg.get("content") or ""),
                    }
                )
                idx += 1
            anthropic_msgs.append({"role": "user", "content": tool_blocks})
            continue

        idx += 1

    return "\n\n".join(system_parts), anthropic_msgs


def build_anthropic_messages_kwargs(
    *,
    model_name: str,
    thinking_level: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    include_thinking: bool = True,
) -> dict[str, Any]:
    """Build kwargs for AsyncAnthropic.messages.create()."""
    level = (
        str(thinking_level or "auto").strip().lower()
        if str(thinking_level or "").strip().lower() in VALID_LLM_THINKING_LEVELS
        else "auto"
    )
    system_prompt, anthropic_msgs = convert_messages_to_anthropic(messages)
    anthropic_tools = convert_tools_to_anthropic(tools or [])

    kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": anthropic_msgs,
        "max_tokens": 4096,
    }
    if system_prompt:
        kwargs["system"] = system_prompt
    if anthropic_tools:
        kwargs["tools"] = anthropic_tools

    if include_thinking and level in _THINKING_BUDGET_TOKENS:
        budget = _THINKING_BUDGET_TOKENS[level]
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Anthropic requires max_tokens > thinking.budget_tokens and forbids custom temperature.
        kwargs["max_tokens"] = budget + 4096
    elif include_thinking and level == "off":
        kwargs["thinking"] = {"type": "disabled"}
        kwargs["temperature"] = 0.1
    elif include_thinking:
        kwargs["temperature"] = 0.1

    return kwargs


def parse_anthropic_response(resp: Any) -> dict[str, Any]:
    """Normalize an Anthropic Message response into the agent's unified turn structure."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    raw_blocks: list[dict[str, Any]] = []

    content_blocks = getattr(resp, "content", None) or []
    for block in content_blocks:
        if isinstance(block, dict):
            btype = str(block.get("type") or "")
            if btype == "text":
                text_val = str(block.get("text") or "")
                text_parts.append(text_val)
                raw_blocks.append({"type": "text", "text": text_val})
            elif btype == "thinking":
                thinking_val = str(block.get("thinking") or "")
                thinking_parts.append(thinking_val)
                item: dict[str, Any] = {"type": "thinking", "thinking": thinking_val}
                if block.get("signature"):
                    item["signature"] = block["signature"]
                raw_blocks.append(item)
            elif btype == "redacted_thinking":
                raw_blocks.append(dict(block))
            elif btype == "tool_use":
                tid = str(block.get("id") or "toolu_unknown")
                tname = str(block.get("name") or "")
                tinput = block.get("input") if isinstance(block.get("input"), dict) else {}
                tool_calls.append(
                    {
                        "id": tid,
                        "type": "function",
                        "function": {"name": tname, "arguments": json.dumps(tinput)},
                    }
                )
                raw_blocks.append(
                    {"type": "tool_use", "id": tid, "name": tname, "input": tinput}
                )
        else:
            btype = str(getattr(block, "type", "") or "")
            if btype == "text":
                text_val = str(getattr(block, "text", "") or "")
                text_parts.append(text_val)
                raw_blocks.append({"type": "text", "text": text_val})
            elif btype == "thinking":
                thinking_val = str(getattr(block, "thinking", "") or "")
                thinking_parts.append(thinking_val)
                item = {"type": "thinking", "thinking": thinking_val}
                sig = getattr(block, "signature", None)
                if sig:
                    item["signature"] = sig
                raw_blocks.append(item)
            elif btype == "redacted_thinking":
                data = getattr(block, "data", None)
                raw_blocks.append({"type": "redacted_thinking", "data": data})
            elif btype == "tool_use":
                tid = str(getattr(block, "id", "toolu_unknown") or "toolu_unknown")
                tname = str(getattr(block, "name", "") or "")
                raw_input = getattr(block, "input", None)
                tinput = raw_input if isinstance(raw_input, dict) else {}
                tool_calls.append(
                    {
                        "id": tid,
                        "type": "function",
                        "function": {"name": tname, "arguments": json.dumps(tinput)},
                    }
                )
                raw_blocks.append(
                    {"type": "tool_use", "id": tid, "name": tname, "input": tinput}
                )

    usage = getattr(resp, "usage", None)
    prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0
    completion_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0

    return {
        "content": "\n".join(text_parts).strip() if text_parts else None,
        "reasoning_content": "\n".join(thinking_parts).strip() if thinking_parts else None,
        "tool_calls": tool_calls or None,
        "raw_content_blocks": raw_blocks,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


__all__ = [
    "LLM_PROVIDER_PRESETS",
    "LLMProtocol",
    "LLMProviderId",
    "LLMProviderPreset",
    "LLMThinkingLevel",
    "ThinkingStrategy",
    "VALID_LLM_PROVIDERS",
    "VALID_LLM_THINKING_LEVELS",
    "build_anthropic_messages_kwargs",
    "build_openai_completion_kwargs",
    "convert_messages_to_anthropic",
    "convert_tools_to_anthropic",
    "get_provider_preset",
    "list_provider_presets",
    "normalize_anthropic_base_url",
    "normalize_provider_id",
    "normalize_thinking_level",
    "parse_anthropic_response",
]
