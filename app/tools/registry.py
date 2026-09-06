"""Tool registry.

Every tool ships with a real executor AND a mock. Eval runs execute thousands of
conversations - they must never write junk into a client's live calendar or CRM.
Which tools a tenant gets is config (`AgentConfig.tools`), never code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

ToolFn = Callable[..., Awaitable[dict]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    executor: ToolFn
    mock_executor: ToolFn
    required_credentials: list[str] = field(default_factory=list)

    def as_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas_for(self, enabled: list[str]) -> list[dict]:
        return [self._tools[n].as_openai_schema() for n in enabled if n in self._tools]

    async def execute(self, name: str, args: dict, ctx: dict, *, use_mock: bool) -> dict:
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        fn = spec.mock_executor if use_mock else spec.executor
        try:
            return await fn(args=args, ctx=ctx)
        except Exception as exc:  # tool failures are data, never a crashed turn
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


registry = ToolRegistry()


def build_registry() -> ToolRegistry:
    from app.tools import booking, handoff, lead  # noqa: F401  (registers on import)

    return registry
