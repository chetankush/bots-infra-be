"""The one graph. Channel-agnostic, config-driven.

    START ─┬─► guard_in ──┐
           └─► retrieve ──┴─► agent ◄──► tools ──► guard_out ──► END
                                 │                     ▲
                                 └── blocked ──────────┘ (skipped)

guard_in and retrieve are independent, so they run concurrently - that removes a
serial LLM round-trip from every single turn. Retrieval is wasted when a message is
blocked, but a local embedding plus a pgvector lookup is far cheaper than the ~2-3s
of latency the serial version cost.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from app.graph.nodes.agent import agent, tools
from app.graph.nodes.guards import guard_in, guard_out
from app.graph.nodes.retrieve import retrieve
from app.graph.state import AgentState
from app.logging import get_logger
from app.settings import get_settings

log = get_logger("graph")


def _after_agent(state: AgentState) -> str:
    # A blocked turn already emitted the configured refusal - re-checking it against
    # the prohibition list is a wasted LLM call.
    if state.get("blocked"):
        return END
    if state.get("iterations", 0) >= get_settings().max_graph_iterations:
        return "guard_out"
    last = state["messages"][-1] if state["messages"] else None
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "tools"
    return "guard_out"


def _timed(name: str, fn: Callable[..., Awaitable[dict]]):
    """Wrap a node so every turn reports where its milliseconds went."""

    async def wrapper(state):
        started = time.perf_counter()
        result = await fn(state)
        log.info("node_timing", node=name, ms=int((time.perf_counter() - started) * 1000))
        return result

    wrapper.__name__ = name
    return wrapper


def build_graph(checkpointer=None):
    graph = StateGraph(AgentState)

    graph.add_node("guard_in", _timed("guard_in", guard_in))
    graph.add_node("retrieve", _timed("retrieve", retrieve))
    graph.add_node("agent", _timed("agent", agent))
    graph.add_node("tools", _timed("tools", tools))
    graph.add_node("guard_out", _timed("guard_out", guard_out))

    # fan out, then fan in: agent waits for both to finish
    graph.add_edge(START, "guard_in")
    graph.add_edge(START, "retrieve")
    graph.add_edge("guard_in", "agent")
    graph.add_edge("retrieve", "agent")

    graph.add_conditional_edges("agent", _after_agent, ["tools", "guard_out", END])
    graph.add_edge("tools", "agent")
    graph.add_edge("guard_out", END)

    return graph.compile(checkpointer=checkpointer)
