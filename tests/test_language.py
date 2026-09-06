"""Language: detected without a model call, ambiguity resolves to the tenant default."""

import pathlib

from app.config.schema import AgentConfig
from app.graph.prompts import localised, system_prefix
from app.services.language import detect, resolve


def test_detection_makes_no_model_call():
    """A classifier per turn would add latency and cost to answer what stopwords settle."""
    src = pathlib.Path("app/services/language.py").read_text()
    for forbidden in ("get_model", "ainvoke", "httpx", "openai", "langchain"):
        assert forbidden not in src, f"detection reaches for {forbidden}"


def test_english_is_not_routed_to_spanish():
    """The failure mode that matters: short English function words look Spanish."""
    for text in ("Hello, can I book?", "thanks", "no", "ok", "What are your hours?", "yes please"):
        assert detect(text, candidates=("en", "es")) == "en", text


def test_clear_spanish_is_detected():
    for text in (
        "Hola, quiero una cita para mi coche",
        "¿Cuánto cuesta la reparación?",
        "Necesito ayuda por favor",
        "Mi vehículo necesita servicio",
    ):
        assert detect(text, candidates=("en", "es")) == "es", text


def test_inverted_punctuation_is_decisive():
    assert detect("¿?", candidates=("en", "es")) == "es"
    assert detect("mañana", candidates=("en", "es")) == "es"


def test_borrowed_accents_alone_do_not_flip_english():
    """café and naïve are ordinary English words."""
    assert detect("I visited a café", candidates=("en", "es")) == "en"


def test_single_language_tenant_is_never_switched():
    assert detect("Hola quiero una cita", candidates=("en",)) == "en"


def test_empty_and_ambiguous_fall_back_to_the_default():
    for text in ("", "   ", "123", "!!!"):
        assert detect(text, candidates=("en", "es"), default="en") == "en"
    assert detect("", candidates=("en", "es"), default="es") == "es"


def test_declared_locale_is_a_hint_not_an_instruction():
    """A widget on a Spanish page still receives English visitors."""
    assert (
        resolve(declared="es", text="Hello, what are your hours?", supported=["en", "es"]) == "en"
    )
    assert resolve(declared="en", text="Hola, quiero una cita", supported=["en", "es"]) == "es"


def test_unsupported_declared_locale_falls_back():
    assert resolve(declared="fr", text="bonjour", supported=["en", "es"], default="en") == "en"


def test_per_locale_overrides_are_additive():
    """A tenant with only English must be completely unaffected."""
    plain = AgentConfig(refusal_message="Nope.")
    assert localised(plain, "refusal_message", "en") == "Nope."
    assert localised(plain, "refusal_message", "es") == "Nope."

    bilingual = AgentConfig(
        refusal_message="Nope.", locale_text={"es": {"refusal_message": "No puedo."}}
    )
    assert localised(bilingual, "refusal_message", "es") == "No puedo."
    assert localised(bilingual, "refusal_message", "en") == "Nope."


def test_each_locale_gets_its_own_stable_cacheable_prefix():
    """Prompt caching keys on an exact byte prefix. One stable block per locale means
    each language has its own cache entry rather than churning a shared one."""
    cfg = AgentConfig(agent_name="R", business_name="B", locales=["en", "es"])
    en = system_prefix(cfg, channel="web", locale="en")
    es = system_prefix(cfg, channel="web", locale="es")

    assert en != es
    assert "reply in English" in en and "reply in Spanish" in es
    # stable across calls - a prefix that varies per turn never caches at all
    assert system_prefix(cfg, channel="web", locale="en") == en


def test_language_rule_is_appended_not_interleaved():
    """Injected mid-prefix, it would invalidate everything after it on a switch."""
    cfg = AgentConfig(agent_name="R", business_name="B", locales=["en", "es"])
    en = system_prefix(cfg, channel="web", locale="en")
    es = system_prefix(cfg, channel="web", locale="es")

    common = 0
    for a, b in zip(en, es, strict=False):
        if a != b:
            break
        common += 1
    # the languages share nearly the whole prefix and diverge only at the tail
    assert common > len(en) * 0.85, "locale text is interleaved, not appended"
