import pytest
from app.llm.factory import get_provider
from app.config import settings

@pytest.mark.integration
def test_qwen_chat_roundtrip():
    p = get_provider()
    if not p.health()["llm"]:
        pytest.skip("Qwen LLM not reachable")
    out = p.chat([{"role": "user", "content": "Reply with exactly: PONG"}])
    assert "PONG" in out.upper()

@pytest.mark.integration
def test_qwen_embed_dim_and_norm():
    import math
    p = get_provider()
    if not p.health()["embed"]:
        pytest.skip("Qwen embed not reachable")
    [v] = p.embed(["a humbucker is a type of guitar pickup"])
    assert len(v) == settings.embed_dim              # 2560
    assert math.isclose(math.sqrt(sum(x*x for x in v)), 1.0, rel_tol=1e-3)

@pytest.mark.integration
def test_qwen_guided_json_returns_dict_matching_tiny_schema():
    """response_format json_schema CONSTRAINS decoding server-side (vLLM) — the
    result must always be a dict with the schema's required "answer" key, never
    free-form prose or malformed JSON.
    """
    p = get_provider()
    if not p.health()["llm"]:
        pytest.skip("Qwen LLM not reachable")
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    out = p.guided_json(
        [{"role": "user", "content": "Reply with the single word PONG as the answer."}],
        schema,
    )
    assert isinstance(out, dict)
    assert "answer" in out
    assert isinstance(out["answer"], str)
