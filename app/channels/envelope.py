"""Normalised inbound message.

Web, WhatsApp, SMS and voice all collapse to this before the graph sees anything.
Adding a channel is an adapter that produces an Envelope - never a new graph.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.config.schema import Channel


class Envelope(BaseModel):
    public_key: str
    session_id: str = Field(min_length=6, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    channel: Channel = "web"
    locale: str = "en"
    location_key: str | None = None
    consent: bool = False
    meta: dict = Field(default_factory=dict)
