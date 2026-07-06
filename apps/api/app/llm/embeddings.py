import math

def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return list(vec)
    return [x / norm for x in vec]

def query_instruct(q: str) -> str:
    return f"Instruct: Given a question, retrieve passages that answer it\nQuery: {q}"
