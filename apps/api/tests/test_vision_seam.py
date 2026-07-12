import base64
from app.llm.qwen import QwenVLLM


class _FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = type("M", (), {"content": content})()
        self.finish_reason = finish_reason


def test_vision_sends_image_as_data_uri_and_returns_text(monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return type("R", (), {"choices": [_FakeChoice("PAGE TEXT")]})()

    p = QwenVLLM()
    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = p.vision(b"\xff\xd8jpegbytes", "Transcribe this page.")

    assert out == "PAGE TEXT"
    content = captured["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image_url")
    assert image_block["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert base64.b64decode(
        image_block["image_url"]["url"].split(",", 1)[1]
    ) == b"\xff\xd8jpegbytes"
    # A page of dense text needs room: the live probe truncated at 400.
    assert captured["max_tokens"] >= 4000


def test_vision_returns_empty_string_when_model_returns_none(monkeypatch):
    p = QwenVLLM()
    monkeypatch.setattr(
        p._client.chat.completions, "create",
        lambda **k: type("R", (), {"choices": [_FakeChoice(None)]})(),
    )
    assert p.vision(b"x", "prompt") == ""
