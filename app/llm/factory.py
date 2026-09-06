"""The single seam between the graph and any model provider.

Everything goes through OpenRouter (one key, one invoice, per-tenant model choice).
Swapping to a direct provider later is a change in this file, not across the graph.
"""

from functools import lru_cache
from typing import Literal

from langchain_openai import ChatOpenAI

from app.config.schema import ModelPolicy
from app.settings import get_settings

Role = Literal["guard", "answer", "escalate", "judge"]


@lru_cache(maxsize=64)
def _client(
    model: str, temperature: float, max_tokens: int, streaming: bool, fallbacks: tuple[str, ...]
) -> ChatOpenAI:
    s = get_settings()
    if not s.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=streaming,
        api_key=s.openrouter_api_key,
        base_url=s.openrouter_base_url,
        default_headers={
            # OpenRouter attribution headers
            "HTTP-Referer": s.openrouter_app_url,
            "X-Title": s.openrouter_app_title,
        },
        timeout=60,
        max_retries=3,
        # OpenRouter routes to the next model if the primary errors or is rate-limited.
        # Cheap models sit on shared upstream pools and DO get 429'd - never run a
        # client-facing tenant on one without a fallback behind it.
        # NOTE: this is an OpenRouter body field, so it must go in extra_body -
        # model_kwargs is spread into the OpenAI SDK signature and would raise TypeError.
        extra_body={"models": [model, *fallbacks]} if fallbacks else None,
    )


def get_model(
    role: Role,
    policy: ModelPolicy,
    *,
    streaming: bool = False,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    model = getattr(policy, role)
    defaults = {"guard": 256, "answer": 1024, "escalate": 1024, "judge": 2048}
    temperature = 0.0 if role in ("guard", "judge") else policy.temperature
    return _client(
        model, temperature, max_tokens or defaults[role], streaming, tuple(policy.fallbacks)
    )


def cache_control(ttl: str = "1h") -> dict:
    """Anthropic prompt-cache marker; passes through OpenRouter to the provider.

    Applied to the stable prefix (persona + tools + config), which is byte-identical
    on every turn of every conversation for a tenant. Cache reads bill at 0.1x.
    """
    return {"cache_control": {"type": "ephemeral", "ttl": ttl}}
