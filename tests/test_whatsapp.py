"""WhatsApp/Twilio: signature is the only thing that authorises, replays are deduped."""

import pathlib
import re

from app.integrations.twilio import expected_signature, valid_signature

# Twilio's own documented example, so this asserts against the published vector
# rather than against my own implementation.
DOC_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
DOC_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675309",
    "Digits": "1234",
    "From": "+14158675309",
    "To": "+18005551212",
}
DOC_TOKEN = "12345"
DOC_SIGNATURE = "RSOYDt4T1cUTdK1PDd93/VVr8B8="


def test_matches_twilios_published_test_vector():
    assert expected_signature(DOC_TOKEN, DOC_URL, DOC_PARAMS) == DOC_SIGNATURE


def test_valid_signature_accepts_the_real_one():
    assert valid_signature(DOC_TOKEN, DOC_URL, DOC_PARAMS, DOC_SIGNATURE)


def test_wrong_token_is_rejected():
    assert not valid_signature("wrong", DOC_URL, DOC_PARAMS, DOC_SIGNATURE)


def test_tampered_parameters_are_rejected():
    tampered = {**DOC_PARAMS, "Digits": "9999"}
    assert not valid_signature(DOC_TOKEN, DOC_URL, tampered, DOC_SIGNATURE)


def test_tampered_url_is_rejected():
    assert not valid_signature(DOC_TOKEN, DOC_URL + "&evil=1", DOC_PARAMS, DOC_SIGNATURE)


def test_missing_token_or_header_is_rejected_not_defaulted():
    assert not valid_signature("", DOC_URL, DOC_PARAMS, DOC_SIGNATURE)
    assert not valid_signature(DOC_TOKEN, DOC_URL, DOC_PARAMS, "")


def test_parameter_order_does_not_matter():
    shuffled = dict(reversed(list(DOC_PARAMS.items())))
    assert expected_signature(DOC_TOKEN, DOC_URL, shuffled) == DOC_SIGNATURE


def test_signature_is_compared_in_constant_time():
    src = pathlib.Path("app/integrations/twilio.py").read_text()
    assert "compare_digest" in src, "a plain == leaks timing information"


def test_replay_is_deduped_because_the_signature_has_no_timestamp():
    """Twilio's scheme has no nonce, so a captured POST stays valid forever."""
    models = pathlib.Path("app/db/models.py").read_text()
    block = re.search(r'__tablename__ = "inbound_messages".*?(?=\nclass |\Z)', models, re.S).group(
        0
    )
    assert "UniqueConstraint" in block and "provider_sid" in block

    endpoint = pathlib.Path("app/api/whatsapp.py").read_text()
    assert "IntegrityError" in endpoint and "twilio_duplicate" in endpoint


def test_signature_check_precedes_any_work():
    """Routing happens first, but it authorises nothing - the signature does."""
    src = pathlib.Path("app/api/whatsapp.py").read_text()
    sig_at = src.index("valid_signature(")
    dedupe_at = src.index("InboundMessage(")
    graph_at = src.index("_graph.ainvoke(")
    assert sig_at < dedupe_at < graph_at, "signature must gate everything expensive"


def test_bad_signature_returns_403_and_no_reply():
    src = pathlib.Path("app/api/whatsapp.py").read_text()
    branch = src.split("twilio_bad_signature")[1][:200]
    assert "403" in branch


def test_rate_limit_keys_on_sender_not_ip():
    """Every Twilio webhook arrives from Twilio's IPs - keying on IP limits nothing."""
    src = pathlib.Path("app/api/whatsapp.py").read_text()
    assert 'f"wa:{tenant_id}:{from_address}"' in src


def test_reply_text_is_xml_escaped():
    """A model reply containing < or & would otherwise produce malformed TwiML."""
    src = pathlib.Path("app/api/whatsapp.py").read_text()
    assert "escape(text)" in src

    from app.api.whatsapp import _twiml

    body = _twiml("5 < 6 & 7 > 2").body.decode()
    assert "&lt;" in body and "&amp;" in body
    assert "<Message>5 <" not in body


def test_errors_stay_silent_on_the_customers_phone():
    src = pathlib.Path("app/api/whatsapp.py").read_text()
    failure = src.split("twilio_failed")[1][:200]
    assert '_twiml("")' in failure
