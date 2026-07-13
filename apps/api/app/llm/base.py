from abc import ABC, abstractmethod
from typing import Iterator

from app.llm.tools_types import AssistantTurn

class LLMProvider(ABC):
    @abstractmethod
    def chat(self, messages: list[dict], *, temperature: float = 0.3,
             enable_thinking: bool = False) -> str: ...

    @abstractmethod
    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]: ...

    @abstractmethod
    def guided_json(self, messages: list[dict], schema: dict, *,
                    temperature: float = 0.2) -> dict: ...

    @abstractmethod
    def chat_tools(self, messages: list[dict], tools: list[dict], *,
                   tool_choice: str = "auto", temperature: float = 0.3) -> AssistantTurn: ...

    def chat_tools_stream(self, messages: list[dict], tools: list[dict], *,
                          tool_choice: str = "auto", temperature: float = 0.3) -> Iterator[dict]:
        """Streaming counterpart of `chat_tools` (Plan 11 Task 3, C4): yields
        `{"type": "content", "text": ...}` for each content delta as the
        model writes it, then exactly one terminal `{"type": "done",
        "content": str | None, "tool_calls": list[ToolCall]}` once the
        response is complete — see `qwen.py`'s implementation for the exact
        contract `app.agent.loop.stream_plain_turn` (the only caller) relies
        on.

        NOT `@abstractmethod`: unlike every other method on this ABC, a
        concrete provider that never implements this degrades gracefully —
        `app.routers.chat`'s streaming endpoint falls back to the existing
        REST turn for anything it can't stream (Task 3's brief explicitly
        blesses this as an honest simplification), so a provider without a
        real implementation just always falls back, rather than failing to
        even instantiate. The default here raises so that fallback is loud
        (a clear `NotImplementedError`) rather than silently doing nothing.
        """
        raise NotImplementedError

    @abstractmethod
    def health(self) -> dict: ...

    @abstractmethod
    def vision(self, image_bytes: bytes, prompt: str, *,
               media_type: str = "image/jpeg") -> str: ...
