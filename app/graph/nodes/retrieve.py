from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.config.schema import AgentConfig
from app.db.session import session_scope
from app.graph.state import AgentState
from app.rag.retriever import retrieve as vector_search


async def retrieve(state: AgentState) -> dict:
    cfg = AgentConfig.model_validate(state["config"])

    user_turns = [m for m in state["messages"] if isinstance(m, HumanMessage)]
    if not user_turns:
        return {"retrieved": [], "trace": ["retrieve"]}

    async with session_scope() as session:
        hits = await vector_search(
            session,
            tenant_id=state["tenant_id"],
            location_id=state.get("location_id"),
            query=str(user_turns[-1].content),
            cfg=cfg.retrieval,
        )
    return {"retrieved": hits, "trace": ["retrieve"]}
