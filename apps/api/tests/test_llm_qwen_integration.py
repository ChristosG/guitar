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
