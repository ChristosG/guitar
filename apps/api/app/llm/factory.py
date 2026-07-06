from functools import lru_cache

from app.config import settings
from app.llm.base import LLMProvider
from app.llm.qwen import QwenVLLM


@lru_cache(maxsize=1)
def get_provider() -> LLMProvider:
    if settings.llm_provider == "qwen":
        return QwenVLLM()
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")  # 'claude' added later
