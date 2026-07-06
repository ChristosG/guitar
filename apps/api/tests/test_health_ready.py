from fastapi.testclient import TestClient
from app.main import app

def test_ready_shape():
    r = TestClient(app).get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"db", "llm", "embed"}
    assert isinstance(body["db"], bool)
