from abc import ABC, abstractmethod

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

    @abstractmethod
    def health(self) -> dict: ...
