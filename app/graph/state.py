from __future__ import annotations

import operator
import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    # envelope - the graph never knows which channel it is serving
    tenant_id: uuid.UUID
    location_id: uuid.UUID | None
    conversation_id: uuid.UUID
    channel: str
    locale: str

    config: dict[str, Any]  # resolved AgentConfig, dumped
    messages: Annotated[list[AnyMessage], add_messages]

    retrieved: list[dict[str, Any]]
    intake: dict[str, Any]

    blocked: bool
    block_reason: str
    escalate: bool
    iterations: int

    usage: dict[str, int]

    # guard_in and retrieve run concurrently and both append, so this needs a
    # reducer - nodes return only their own entries, never the accumulated list.
    trace: Annotated[list[str], operator.add]
