"""Agent configuration from environment variables. Model ids are never hard-coded.

LLM_PROVIDER        "bedrock" (default, production), "groq" or "nvidia" (OpenAI-compatible APIs) or "ollama"
                    (local testing only). Used for planning, ideas and metadata, and for the other tiers unless they
                    name their own provider below.
LLM_SMALL_PROVIDER  Provider for small calls (intent routing, query rewrite, verifier, distill). Defaults to
                    LLM_PROVIDER.
LLM_CHAT_PROVIDER   Provider for streamed chat replies. Defaults to LLM_PROVIDER. Mix freely, e.g. agentic work
                    on Groq and chat on a local Ollama model.
LLM_MODEL_ID        Model for planning, ideas and metadata: a Bedrock model or inference profile id, a Groq model
                    id (openai/gpt-oss-120b) or an Ollama model name (qwen3:4b).
LLM_SMALL_MODEL_ID  Model for small calls. Defaults to LLM_MODEL_ID when both use the same provider.
LLM_CHAT_MODEL_ID   Model for streamed chat replies. Defaults to LLM_MODEL_ID when both use the same provider.
                    Locally, llama3.2 answers directly where qwen3 narrates its reasoning.
BEDROCK_REGION      Region of the Bedrock runtime (bedrock only).
GROQ_API_KEY        Groq API key (groq only). Secret: keep it in .env or Secrets Manager, never in code.
GROQ_BASE_URL       Default https://api.groq.com/openai/v1.
GROQ_STRUCTURED_METHOD  "json_schema" (default; gpt-oss and qwen3.8 support it) or "function_calling".
NVIDIA_API_KEY      NVIDIA API catalog key, nvapi-... (nvidia only). Secret, like GROQ_API_KEY.
NVIDIA_BASE_URL     Default https://integrate.api.nvidia.com/v1.
NVIDIA_STRUCTURED_METHOD  "function_calling" (default: on NVIDIA's reasoning models a forced tool call skips
                    most of the thinking, about 5x faster than json_schema) or "json_schema".
OLLAMA_BASE_URL     Ollama server (ollama only), default http://localhost:11434.
OLLAMA_NUM_CTX      Context window for Ollama, default 16384. Ollama's own default (4096) truncates transcripts.
OLLAMA_KEEP_ALIVE   How long Ollama keeps the model in memory after a call, default 1m.
LLM_TIMEOUT_S       Per-call timeout, default 60 (Bedrock, Groq). Local models are slower: raise it for Ollama.
LLM_STRUCTURED_METHOD  For Bedrock and Ollama: "json_schema" (default, Claude on Bedrock) or "function_calling"
                    (Amazon Nova, which rejects Bedrock's outputConfig).
LLM_SEND_TEMPERATURE  "1" to send temperature to Bedrock. Off by default: current Claude models reject sampling
                      parameters, so the documented temperatures (0 and 0.2) are only sent when enabled. Groq and
                      Ollama always get them.
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache


class ConfigError(RuntimeError):
    pass


PROVIDERS = ("bedrock", "groq", "nvidia", "ollama")
TIERS = ("main", "small", "chat")


@dataclass(frozen=True)
class AgentConfig:
    model_id: str
    small_model_id: str
    chat_model_id: str
    region: str
    send_temperature: bool
    timeout_s: int = 60
    max_output_tokens: int = 16000
    provider: str = "bedrock"  # the main tier's provider
    small_provider: str = ""  # "" means the same as provider
    chat_provider: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_num_ctx: int = 16384
    ollama_keep_alive: str = "1m"
    structured_method: str = "json_schema"
    groq_api_key: str = field(default="", repr=False)
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_structured_method: str = "json_schema"
    nvidia_api_key: str = field(default="", repr=False)
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_structured_method: str = "function_calling"

    def provider_for(self, tier: str) -> str:
        return {"small": self.small_provider, "chat": self.chat_provider}.get(tier) or self.provider

    def uses(self, provider: str) -> bool:
        return any(self.provider_for(t) == provider for t in TIERS)


def _method(name: str, default: str = "json_schema") -> str:
    method = os.environ.get(name, default).strip() or default
    if method not in ("json_schema", "function_calling"):
        raise ConfigError(f"{name} must be json_schema or function_calling, not {method!r}")
    return method


def _provider(name: str, default: str) -> str:
    provider = os.environ.get(name, "").strip().lower() or default
    if provider not in PROVIDERS:
        raise ConfigError(f"{name} must be one of {', '.join(PROVIDERS)}, not {provider!r}")
    return provider


@lru_cache(maxsize=1)
def agent_config() -> AgentConfig:
    provider = _provider("LLM_PROVIDER", "bedrock")
    small_provider = _provider("LLM_SMALL_PROVIDER", provider)
    chat_provider = _provider("LLM_CHAT_PROVIDER", provider)
    model = os.environ.get("LLM_MODEL_ID", "").strip()
    # A tier on another provider can't borrow the main model's id: it must name its own.
    small = os.environ.get("LLM_SMALL_MODEL_ID", "").strip() or (model if small_provider == provider else "")
    chat = os.environ.get("LLM_CHAT_MODEL_ID", "").strip() or (model if chat_provider == provider else "")
    region = os.environ.get("BEDROCK_REGION", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    nvidia_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    used = {provider, small_provider, chat_provider}
    required = [("LLM_MODEL_ID", model), ("LLM_SMALL_MODEL_ID", small), ("LLM_CHAT_MODEL_ID", chat)]
    if "bedrock" in used:
        required.append(("BEDROCK_REGION", region))
    if "groq" in used:
        required.append(("GROQ_API_KEY", groq_key))
    if "nvidia" in used:
        required.append(("NVIDIA_API_KEY", nvidia_key))
    missing = [name for name, v in required if not v]
    if missing:
        raise ConfigError(
            f"Set {', '.join(missing)} for LLM_PROVIDER={provider} (small: {small_provider}, chat: {chat_provider}) "
            "before starting the agent worker"
        )
    return AgentConfig(
        model_id=model,
        small_model_id=small,
        chat_model_id=chat,
        region=region,
        send_temperature=os.environ.get("LLM_SEND_TEMPERATURE", "") == "1",
        timeout_s=int(os.environ.get("LLM_TIMEOUT_S", "60")),
        provider=provider,
        small_provider=small_provider,
        chat_provider=chat_provider,
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").strip(),
        ollama_num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "16384")),
        ollama_keep_alive=os.environ.get("OLLAMA_KEEP_ALIVE", "1m").strip() or "1m",
        structured_method=_method("LLM_STRUCTURED_METHOD"),
        groq_api_key=groq_key,
        groq_base_url=os.environ.get("GROQ_BASE_URL", "").strip() or "https://api.groq.com/openai/v1",
        groq_structured_method=_method("GROQ_STRUCTURED_METHOD"),
        nvidia_api_key=nvidia_key,
        nvidia_base_url=os.environ.get("NVIDIA_BASE_URL", "").strip() or "https://integrate.api.nvidia.com/v1",
        nvidia_structured_method=_method("NVIDIA_STRUCTURED_METHOD", "function_calling"),
    )
