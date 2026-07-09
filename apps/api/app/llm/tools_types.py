"""Data shapes for tool-calling (function-calling) turns - the parsed view of
an OpenAI-protocol tool-call response produced by `LLMProvider.chat_tools`
(`base.py`'s abstractmethod, `qwen.py`'s implementation). Kept out of
`base.py`/`qwen.py` so a caller that only needs these shapes (e.g. Task 2's
ReAct loop) doesn't have to import the ABC or the vLLM client along with them
- same rationale as `errors.py` keeping `GuidedJSONError` out of `base.py`.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCall:
    """One parsed tool call out of an assistant turn. `id` pairs the
    eventual tool result back to this call (`{"role": "tool", "tool_call_id":
    id, "content": ...}` per the OpenAI tool-calling protocol - see
    `/mnt/nvme2TB/vllm_interract/examples/tool_calling_minimal.py`). `name`
    is the tool to invoke. `arguments` is already `json.loads`'d into a dict
    by `chat_tools` - never the raw JSON string (a malformed string raises
    `ToolArgsError` at parse time instead; see `qwen.py`'s `chat_tools`).
    Frozen: a parsed call is a value, not something a caller should mutate
    in place.
    """
    id: str
    name: str
    arguments: dict


@dataclass
class AssistantTurn:
    """One assistant turn out of `LLMProvider.chat_tools`. `content` is the
    model's plain-text reply, or `None` when the turn is tool-calls-only
    (the underlying OpenAI SDK's `message.content` is `None` in that case).
    `tool_calls` is always a list - empty (not `None`) when the model
    answered in plain text - so callers can iterate it unconditionally
    without a None-check.
    """
    content: str | None
    tool_calls: list[ToolCall]
