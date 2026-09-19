"""Resolved agent configuration.

Config is layered: platform defaults -> vertical pack -> tenant -> location -> channel.
Each layer is a partial dict; they are deep-merged and validated into AgentConfig.
Adding a client is an INSERT, never a fork.
"""

from typing import Literal

from pydantic import BaseModel, Field

Channel = Literal["web", "whatsapp", "sms", "email", "voice"]


class ModelPolicy(BaseModel):
    """Model choice per role. Lives in the config chain so a price-sensitive
    client can run cheaper models than a Custom-tier dealership, with no code change."""

    guard: str = "google/gemini-2.5-flash-lite"
    answer: str = "anthropic/claude-haiku-4.5"
    escalate: str = "anthropic/claude-sonnet-5"
    judge: str = "anthropic/claude-opus-5"
    fallbacks: list[str] = Field(default_factory=list)
    cache_ttl: Literal["5m", "1h"] = "1h"
    temperature: float = 0.3


class LeadField(BaseModel):
    key: str
    label: str
    required: bool = True
    kind: Literal["text", "email", "phone", "date", "choice"] = "text"
    choices: list[str] = Field(default_factory=list)


class EscalationCfg(BaseModel):
    enabled: bool = True
    triggers: list[str] = Field(
        default_factory=lambda: ["explicit_request", "complaint", "out_of_scope", "low_confidence"]
    )
    email_to: list[str] = Field(default_factory=list)
    reply_to: str = ""
    webhook_url: str | None = None
    webhook_secret: str = ""
    message: str = (
        "Let me get a team member to help you with this. What's the best way to reach you?"
    )


class ChannelProfile(BaseModel):
    max_reply_chars: int = 1200
    allow_markdown: bool = True
    max_turns: int = 40
    # Streaming tokens straight to the visitor means they see the reply BEFORE
    # guard_out has checked it against the prohibition list. For a tenant whose
    # selling point is "never discusses financing", that is a real leak - the text
    # is on screen for ~1.4s before it is retracted. Default is to hold the reply
    # until it has been cleared; turn it on only for tenants with no prohibitions.
    stream_before_guard: bool = False


class BookingCfg(BaseModel):
    """Every scheduling knob lives here, so a new client is config not code."""

    hours: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "mon": ["09:00-13:00", "14:00-18:00"],
            "tue": ["09:00-13:00", "14:00-18:00"],
            "wed": ["09:00-13:00", "14:00-18:00"],
            "thu": ["09:00-13:00", "14:00-18:00"],
            "fri": ["09:00-13:00", "14:00-18:00"],
            "sat": ["09:00-13:00"],
            "sun": [],
        }
    )
    closed_dates: list[str] = Field(default_factory=list)  # ISO dates
    slot_minutes: int = Field(default=60, ge=5, le=480)
    slot_step_minutes: int = Field(default=60, ge=5, le=480)
    lead_time_minutes: int = Field(default=120, ge=0, le=20_160)
    horizon_days: int = Field(default=14, ge=1, le=90)
    max_slots: int = Field(default=6, ge=1, le=25)
    buffer_minutes: int = Field(default=0, ge=0, le=240)
    event_summary: str = "{service} - {name}"


class ReminderRule(BaseModel):
    key: str  # stable id; changing it re-plans
    hours_before: float = Field(ge=0, le=8760)  # 0 .. one year
    message: str = "Reminder: your appointment with {business} is at {when}."


class ReminderCfg(BaseModel):
    """Reminders cost real money per message, unlike tokens. Everything is bounded."""

    enabled: bool = False  # opt-in: shipping this on would message real customers
    rules: list[ReminderRule] = Field(default_factory=list)
    max_per_appointment: int = Field(default=3, ge=0, le=5)
    max_attempts: int = Field(default=3, ge=1, le=5)
    quiet_hours: list[str] = Field(default_factory=lambda: ["21:00-08:00"])


class ConsentCfg(BaseModel):
    """Visitor consent, decided by the SERVER.

    `required` is deliberately tri-state. None means "derive from the tenant's region",
    so a tenant does not silently lose its consent gate by omitting a config key. A
    client-supplied flag can only ever *assert acceptance* - it can never decide whether
    consent was needed in the first place.
    """

    required: bool | None = None  # None -> derived from Tenant.region
    version: int = 1
    notice: str = (
        "We use an assistant to answer questions and may store this chat to follow up. "
        "By continuing you agree to our privacy policy."
    )
    accept_label: str = "Start chat"
    policy_url: str = ""
    policy_label: str = "Privacy policy"


# Regions where consent is gathered unless a tenant explicitly overrides it.
CONSENT_REGIONS = frozenset({"eu", "ae", "uk"})


class RetrievalCfg(BaseModel):
    top_k: int = 6
    min_score: float = 0.25
    max_context_chars: int = 6000

    # Hybrid retrieval. Dense embeddings catch paraphrase; Postgres full-text catches the
    # exact tokens embeddings blur: product codes, part numbers, names. Both legs run
    # tenant-scoped, are fused by rank (RRF), then a cross-encoder reorders the top
    # candidates before top_k is cut.
    hybrid: bool = True
    rerank: bool = True
    candidates: int = 20  # per leg, and the rerank window
    rrf_k: int = 60  # damping constant from Cormack et al. (2009)


class AgentConfig(BaseModel):
    """The fully-resolved config the graph executes against."""

    # identity
    agent_name: str = "Assistant"
    business_name: str = ""
    persona: str = "You are a helpful assistant for this business."
    tone: Literal["formal", "friendly", "concise"] = "friendly"
    greeting: str = "Hi! How can I help you today?"
    locales: list[str] = Field(default_factory=lambda: ["en"])
    # Per-locale overrides, e.g. {"es": {"greeting": "Hola...", "refusal_message": "..."}}.
    # Additive: a tenant with only English keeps working untouched, and the existing
    # single-string fields remain the source for the default locale.
    locale_text: dict[str, dict[str, str]] = Field(default_factory=dict)

    # behaviour
    scope: list[str] = Field(default_factory=list)
    prohibitions: list[str] = Field(default_factory=list)
    refusal_message: str = (
        "That's outside what I can help with here, but I can connect you with the team."
    )
    qualify_after_turns: int = 2
    lead_fields: list[LeadField] = Field(default_factory=list)

    # capability
    tools: list[str] = Field(default_factory=lambda: ["capture_lead", "escalate_to_human"])
    escalation: EscalationCfg = Field(default_factory=EscalationCfg)
    consent: ConsentCfg = Field(default_factory=ConsentCfg)
    booking: BookingCfg = Field(default_factory=BookingCfg)
    reminders: ReminderCfg = Field(default_factory=ReminderCfg)
    # Outbound integration surface: Zapier / n8n / Make / Power Automate / any CRM.
    lead_webhook_url: str = ""
    lead_webhook_secret: str = ""
    lead_email_to: list[str] = Field(default_factory=list)
    # Customer-facing mail stays OFF by default. Turning it on for every existing
    # tenant by shipping a new default would email real customers without anyone
    # deciding to.
    appointment_email_enabled: bool = False

    # runtime
    models: ModelPolicy = Field(default_factory=ModelPolicy)
    retrieval: RetrievalCfg = Field(default_factory=RetrievalCfg)
    channel: ChannelProfile = Field(default_factory=ChannelProfile)


CHANNEL_PROFILES: dict[Channel, dict] = {
    "web": {"max_reply_chars": 1200, "allow_markdown": True, "max_turns": 40},
    "whatsapp": {"max_reply_chars": 900, "allow_markdown": False, "max_turns": 30},
    "sms": {"max_reply_chars": 300, "allow_markdown": False, "max_turns": 20},
    "email": {"max_reply_chars": 2500, "allow_markdown": False, "max_turns": 10},
    # voice needs short, speakable replies - same graph, different profile
    "voice": {"max_reply_chars": 220, "allow_markdown": False, "max_turns": 25},
}
