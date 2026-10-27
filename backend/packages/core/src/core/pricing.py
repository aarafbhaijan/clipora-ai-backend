"""LLM prices for the estimated cost in `llm_usage` (architecture.md section 16).

Only prices read from the provider's published price list belong here (AWS's price list, Groq's models page);
never estimate one. Do not guess prices. Until a model is listed, its calls record tokens and a null cost.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal = Decimal(0)
    cache_write: Decimal = Decimal(0)


# Amazon Bedrock on-demand prices, Sydney (ap-southeast-2), from AWS's public price list
# (offers/v1.0/aws/AmazonBedrock/current/ap-southeast-2/index.json, read 2026-10-25).
# Cross-region inference profiles (apac.*) are billed at the source region's price.
# Add a model here before using it; unlisted models record tokens with a null cost.
_NOVA_MICRO = Price(input=Decimal("0.037"), output=Decimal("0.148"), cache_read=Decimal("0.00925"))
_NOVA_LITE = Price(input=Decimal("0.063"), output=Decimal("0.252"), cache_read=Decimal("0.01575"))

# Groq on-demand prices from https://console.groq.com/docs/models (read 2026-10-26; groq.com/pricing shows no
# table). Llama models there say "Contact sales", so they record tokens with a null cost.
_GPT_OSS_120B = Price(input=Decimal("0.15"), output=Decimal("0.60"))
_GPT_OSS_20B = Price(input=Decimal("0.075"), output=Decimal("0.30"))
_QWEN3_8_27B = Price(input=Decimal("0.80"), output=Decimal("4.00"))

PRICES: dict[str, Price] = {
    "apac.amazon.nova-micro-v1:0": _NOVA_MICRO,
    "amazon.nova-micro-v1:0": _NOVA_MICRO,
    "apac.amazon.nova-lite-v1:0": _NOVA_LITE,
    "amazon.nova-lite-v1:0": _NOVA_LITE,
    "openai/gpt-oss-120b": _GPT_OSS_120B,
    "openai/gpt-oss-20b": _GPT_OSS_20B,
    "qwen/qwen3.8-27b": _QWEN3_8_27B,
}

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def cost_usd(model: str, usage: TokenUsage) -> Decimal | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (
        price.input * usage.input_tokens
        + price.output * usage.output_tokens
        + price.cache_read * usage.cache_read_tokens
        + price.cache_write * usage.cache_write_tokens
    ) / _MILLION
