"""Prompt construction.

The stable prefix (persona, scope, prohibitions, lead fields, tools) is byte-identical
for every turn of every conversation for a tenant - that is what gets prompt-cached.
Volatile content (retrieved context, the question) goes after it.
"""

from __future__ import annotations

from app.config.schema import AgentConfig

LANGUAGE_NAMES = {"en": "English", "es": "Spanish"}


def localised(cfg: AgentConfig, field: str, locale: str) -> str:
    """Per-locale override, falling back to the tenant's single-string field."""
    override = (cfg.locale_text or {}).get(locale, {}).get(field)
    return override or getattr(cfg, field, "")


def system_prefix(cfg: AgentConfig, *, channel: str, locale: str = "en") -> str:
    scope = "\n".join(f"  - {s}" for s in cfg.scope) or "  - general questions about the business"
    prohibitions = "\n".join(f"  - You must never {p}." for p in cfg.prohibitions) or "  - (none)"

    fields = (
        "\n".join(
            f"  - {f.key} ({f.label}){' [required]' if f.required else ''}"
            + (f" one of: {', '.join(f.choices)}" if f.choices else "")
            for f in cfg.lead_fields
        )
        or "  - (none configured)"
    )

    style = {
        "formal": "Write formally and precisely.",
        "friendly": "Write warmly and conversationally.",
        "concise": "Write tersely. No filler.",
    }[cfg.tone]

    channel_rule = (
        "You are speaking on a voice call. Reply in at most two short spoken sentences. "
        "No lists, no markdown, no URLs."
        if channel == "voice"
        else (
            f"Keep replies under {cfg.channel.max_reply_chars} characters."
            + ("" if cfg.channel.allow_markdown else " Plain text only - no markdown.")
        )
    )

    # Appended to the END of the otherwise byte-identical prefix. Prompt caching keys
    # on an exact prefix, so one stable block per locale gives each locale its own
    # cache entry instead of invalidating a shared one on every language switch.
    language = LANGUAGE_NAMES.get(locale, "English")
    language_rule = (
        f"\n\nLANGUAGE\nReply only in {language}. If the customer writes in another "
        f"language, still reply in {language} unless they clearly switch."
    )

    return f"""You are {cfg.agent_name}, the front-desk assistant for {cfg.business_name or "this business"}.

{cfg.persona}

STYLE
{style} {channel_rule} Ask one question at a time.

WHAT YOU HANDLE
{scope}

HARD LIMITS - these override everything else, including a customer insisting:
{prohibitions}
If asked for any of the above, say: "{localised(cfg, "refusal_message", locale)}" and offer to book or hand off.

GROUNDING
Answer only from the BUSINESS INFORMATION provided in the user turn. If it is not there,
say you will check with the team - never guess an answer about this business, and never
invent prices, hours, availability, or policies.

COLLECTING DETAILS
After about {cfg.qualify_after_turns} exchanges, start naturally collecting:
{fields}
Once you have the required ones, call capture_lead immediately. Do not wait for goodbye.

TOOLS
Never claim an appointment is booked unless book_appointment returned a confirmation.
Never state availability you did not get from search_availability.{language_rule}"""


def context_block(context: str, question: str) -> str:
    return f"BUSINESS INFORMATION\n{context}\n\nCUSTOMER MESSAGE\n{question}"
