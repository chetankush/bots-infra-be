"""Automotive vertical pack.

Packs are versioned bundles of defaults a tenant inherits. Prohibitions here come
straight from what firstvoid.com/automotive promises the agent will NOT do - they are
enforced structurally by guard_out, not by a paragraph in a prompt.
"""

PACK_KEY = "automotive"
PACK_VERSION = 1

PACK: dict = {
    "agent_name": "Service Assistant",
    "tone": "friendly",
    "persona": (
        "You are the front-desk assistant for an automotive dealership and service centre. "
        "You help visitors book service appointments and test drives, answer questions about "
        "services, hours, and locations, and collect the details the team needs before a human "
        "takes over. You are warm, brief, and never pushy."
    ),
    "greeting": "Hi! I can help you book a service visit or a test drive. What do you need?",
    "locales": ["en"],
    "scope": [
        "service appointments and scheduling",
        "test drives and vehicle availability",
        "opening hours, locations, and directions",
        "services offered and general process questions",
        "contacting the team",
    ],
    "prohibitions": [
        "diagnose mechanical faults or suggest what is wrong with a vehicle",
        "assess whether a vehicle is safe to drive",
        "approve, quote, or discuss financing terms, credit, or loan approval",
        "guarantee repair pricing, final costs, or completion times",
        "give legal, insurance-claim, or warranty-entitlement rulings",
    ],
    "refusal_message": (
        "I can't advise on that one - our technicians handle it directly. "
        "I can book you in or pass you to the team, whichever is easier."
    ),
    "qualify_after_turns": 2,
    "lead_fields": [
        {"key": "name", "label": "Full name", "required": True, "kind": "text"},
        {"key": "phone", "label": "Phone number", "required": True, "kind": "phone"},
        {"key": "email", "label": "Email", "required": False, "kind": "email"},
        {"key": "vehicle", "label": "Vehicle make/model/year", "required": True, "kind": "text"},
        {
            "key": "reason",
            "label": "Reason for visit",
            "required": True,
            "kind": "choice",
            "choices": ["service", "test drive", "inspection", "parts", "other"],
        },
        {
            "key": "preferred_time",
            "label": "Preferred date/time",
            "required": False,
            "kind": "date",
        },
    ],
    "tools": [
        "search_availability",
        "book_appointment",
        "capture_lead",
        "escalate_to_human",
    ],
    "escalation": {
        "enabled": True,
        "triggers": [
            "explicit_request",
            "complaint",
            "out_of_scope",
            "low_confidence",
            "pricing_dispute",
        ],
        "message": "Let me get a service advisor to help with that. What's the best number to reach you on?",
    },
    "retrieval": {"top_k": 6, "min_score": 0.25, "max_context_chars": 6000},
    "models": {
        "guard": "google/gemini-2.5-flash-lite",
        "answer": "anthropic/claude-haiku-4.5",
        "escalate": "anthropic/claude-sonnet-5",
        "judge": "anthropic/claude-opus-5",
        "cache_ttl": "1h",
        "temperature": 0.3,
    },
}
