import math
from app.llm.embeddings import l2_normalize, query_instruct

def test_l2_normalize_unit_length():
    out = l2_normalize([3.0, 4.0])
    assert math.isclose(math.hypot(*out), 1.0, rel_tol=1e-9)
    assert math.isclose(out[0], 0.6) and math.isclose(out[1], 0.8)

def test_l2_normalize_zero_vector_is_safe():
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]

def test_query_instruct_prefix():
    assert query_instruct("what is a humbucker?").startswith(
        "Instruct: Given a question, retrieve passages that answer it\nQuery: "
    )
