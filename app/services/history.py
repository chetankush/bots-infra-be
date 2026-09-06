"""Conversation replay.

Every turn is persisted to `messages` already, so history comes from there rather than
a second checkpoint store - one datastore, one driver, and the transcript the console
shows is the same one the model sees.

Tool call/result pairs are not replayed: a ToolMessage without its originating
tool_call in the same window is invalid, and the durable outcome (intake fields,
booked appointment) is carried in state instead.
"""

from __future__ import annotations

import uuid

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message


async def load_history(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    max_turns: int = 40,
) -> list[BaseMessage]:
    rows = (
        (
            await session.execute(
                select(Message)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.conversation_id == conversation_id,
                    Message.role.in_(("user", "assistant")),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(max_turns)
            )
        )
        .scalars()
        .all()
    )

    out: list[BaseMessage] = []
    for row in reversed(rows):
        content = (row.content or "").strip()
        if not content:
            continue
        out.append(
            HumanMessage(content=content) if row.role == "user" else AIMessage(content=content)
        )
    return out
