"""The config chain is what replaces the per-client template. It gets tests."""

from app.config.packs import PACKS
from app.config.resolver import deep_merge, resolve


def test_layers_apply_in_order():
    cfg = resolve(
        pack=PACKS["automotive"],
        tenant={"agent_name": "Riya", "business_name": "Northside Motors"},
        location={"retrieval": {"top_k": 9}},
        channel="web",
    )
    assert cfg.agent_name == "Riya"  # tenant beats pack
    assert cfg.retrieval.top_k == 9  # location beats tenant
    assert cfg.retrieval.min_score == 0.25  # untouched pack value survives
    assert len(cfg.prohibitions) == 5  # pack prohibitions inherited


def test_channel_profile_reshapes_same_tenant():
    args = dict(pack=PACKS["automotive"], tenant={}, location=None)
    web = resolve(**args, channel="web")
    voice = resolve(**args, channel="voice")
    sms = resolve(**args, channel="sms")

    assert web.channel.allow_markdown is True
    assert voice.channel.allow_markdown is False
    assert voice.channel.max_reply_chars < web.channel.max_reply_chars
    assert sms.channel.max_reply_chars < voice.channel.max_reply_chars + 200
    # same behaviour, different shape - one graph, not three
    assert web.prohibitions == voice.prohibitions == sms.prohibitions


def test_deep_merge_replaces_lists_and_ignores_none():
    base = {"a": {"b": 1, "c": 2}, "list": [1, 2, 3], "keep": "yes"}
    out = deep_merge(base, {"a": {"c": 9}, "list": [7], "keep": None})
    assert out == {"a": {"b": 1, "c": 9}, "list": [7], "keep": "yes"}


def test_packs_carry_their_prohibitions():
    auto = resolve(pack=PACKS["automotive"], tenant={}, location=None)
    generic = resolve(pack=PACKS["generic"], tenant={}, location=None)
    assert any("financing" in p for p in auto.prohibitions)
    assert any("diagnose" in p for p in auto.prohibitions)
    assert auto.prohibitions != generic.prohibitions


def test_adding_a_client_needs_no_code():
    """A whole new client is a dict, not a fork."""
    cfg = resolve(
        pack=PACKS["generic"],
        tenant={
            "business_name": "Bright Smile Dental",
            "agent_name": "Ava",
            "lead_fields": [{"key": "name", "label": "Name", "required": True}],
            "models": {"answer": "anthropic/claude-haiku-4.5"},
        },
        location=None,
    )
    assert cfg.agent_name == "Ava"
    assert [f.key for f in cfg.lead_fields] == ["name"]
    assert cfg.models.answer == "anthropic/claude-haiku-4.5"
