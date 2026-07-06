from abc import ABC, abstractmethod

class LLMProvider(ABC):
    @abstractmethod
    def chat(self, messages: list[dict], *, temperature: float = 0.3,
             enable_thinking: bool = False) -> str: ...

    @abstractmethod
    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]: ...

    @abstractmethod
    def health(self) -> dict: ...
