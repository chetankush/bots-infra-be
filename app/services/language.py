"""Language detection.

Deliberately not a model call. A classifier per turn would add ~800ms and a cost to
every message to answer a question that stopwords settle for free, and the answer only
has to be good enough to pick a prompt.

Precedence: script/diacritic evidence, then stopword balance, then the tenant default.
Ambiguity resolves to the default rather than guessing - answering an English speaker
in Spanish is worse than the reverse, because they cannot even read the apology.
"""

from __future__ import annotations

import re

# Words common in one language and rare in the other. Short function words shared
# across both ("no", "me", "si") are excluded: they route plain English to Spanish.
_ES = {
    "hola",
    "gracias",
    "buenos",
    "buenas",
    "dias",
    "días",
    "tardes",
    "noches",
    "necesito",
    "quiero",
    "quisiera",
    "puedo",
    "puede",
    "podría",
    "cuánto",
    "cuanto",
    "cuándo",
    "cuando",
    "dónde",
    "donde",
    "cómo",
    "como",
    "qué",
    "para",
    "por",
    "cita",
    "citas",
    "horario",
    "precio",
    "precios",
    "coche",
    "carro",
    "vehículo",
    "servicio",
    "reparación",
    "taller",
    "ayuda",
    "favor",
    "usted",
    "ustedes",
    "tienen",
    "tiene",
    "estoy",
    "está",
    "están",
    "hacer",
    "sobre",
    "también",
}
_EN = {
    "hello",
    "hi",
    "hey",
    "thanks",
    "thank",
    "please",
    "need",
    "want",
    "would",
    "could",
    "can",
    "how",
    "much",
    "when",
    "where",
    "what",
    "why",
    "appointment",
    "booking",
    "book",
    "price",
    "prices",
    "hours",
    "car",
    "vehicle",
    "service",
    "repair",
    "help",
    "about",
    "your",
    "you",
    "the",
    "and",
    "with",
    "have",
    "does",
}

# Characters that appear in Spanish and effectively never in English text.
_ES_MARKS = set("ñ¿¡")
_ES_ACCENTS = set("áéíóúü")

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")][:60]


def detect(text: str, *, candidates: tuple[str, ...] = ("en",), default: str = "en") -> str:
    """Return a language code from `candidates`. Cheap, deterministic, no I/O."""
    if len(candidates) < 2:
        return default
    if "es" not in candidates:
        return default

    raw = (text or "").strip()
    if not raw:
        return default

    lowered = raw.lower()

    # Inverted punctuation and ñ are decisive: they are Spanish orthography, not accents
    # a stray English word might carry.
    if any(ch in _ES_MARKS for ch in lowered):
        return "es"

    words = _words(raw)
    if not words:
        return default

    es_hits = sum(1 for w in words if w in _ES)
    en_hits = sum(1 for w in words if w in _EN)

    # Accents only count once stopwords already lean Spanish - "café" and "naïve" are
    # ordinary English borrowings.
    if es_hits > en_hits and any(ch in _ES_ACCENTS for ch in lowered):
        return "es"

    if es_hits > en_hits:
        return "es"
    if en_hits > es_hits:
        return "en"

    # A tie is not evidence. Prefer the tenant's default.
    return default


def resolve(*, declared: str, text: str, supported: list[str], default: str = "en") -> str:
    """Combine a client-declared locale with detection.

    The declared locale is a hint, not an instruction: a widget on a Spanish page sends
    es for an English visitor. Detection wins when the text is clearly the other
    language; otherwise the declaration stands.
    """
    supported_codes = tuple(s.split("-")[0].lower() for s in (supported or ["en"]))
    declared_code = (declared or default).split("-")[0].lower()
    if declared_code not in supported_codes:
        declared_code = default

    if len(supported_codes) < 2:
        return default

    detected = detect(text, candidates=supported_codes, default=declared_code)
    return detected if detected in supported_codes else declared_code
