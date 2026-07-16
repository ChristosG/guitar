FROM python:3.12-slim
WORKDIR /app

# --- The embedding model, baked in at BUILD time (Plan 13, Stage 4) -----------
#
# multilingual-e5-small, fp32 ONNX, 470MB, into /opt/models/e5-small. Downloaded
# HERE and never at runtime: a first-run download is a first-run FAILURE waiting
# to happen on a tutor's iMac behind a hotel wifi, and it turns "the app is
# installed" into "the app is installed and has been online once".
#
# `intfloat` publishes the ONNX graph itself, so this needs neither torch (~800MB)
# nor optimum to produce — and it is verified bit-identical to the
# sentence-transformers reference (cosine 1.000000; see tests/test_embedder.py).
#
# FP32, NOT int8. The int8 export is 4x smaller (118MB) and its vectors look fine
# (cosine 0.986-0.992 against the reference) — but it CHANGES THE TOP-5 RETRIEVAL
# RANKING on 6 of 6 real queries. A quietly worse index is not worth 350MB.
#
# The two files land flat, because that is what `app/llm/embedder.py` looks for:
# the repo puts the graph under `onnx/` and the tokenizer at the root.
RUN pip install --no-cache-dir "huggingface_hub[hf_xet]>=0.26" \
 && hf download intfloat/multilingual-e5-small onnx/model.onnx tokenizer.json \
      --local-dir /tmp/e5 \
 && mkdir -p /opt/models/e5-small \
 && mv /tmp/e5/onnx/model.onnx /opt/models/e5-small/model.onnx \
 && mv /tmp/e5/tokenizer.json /opt/models/e5-small/tokenizer.json \
 && rm -rf /tmp/e5 /root/.cache \
 && pip uninstall -y huggingface_hub hf-xet

# Belt to the braces above: even if some future dependency decides it would like
# to fetch a model, it cannot. An offline failure is loud and immediate; a silent
# background download on a metered connection is not.
ENV HF_HUB_OFFLINE=1

# Copy source BEFORE the editable install so the `app` package exists at install
# time — otherwise the editable mapping freezes empty and any non-uvicorn entrypoint
# (alembic, pytest, the worker) fails with ModuleNotFoundError. .dockerignore keeps
# the host .venv/caches out of the context.
COPY . .
RUN pip install --no-cache-dir -e ".[dev]"

# MIGRATIONS RUN AT BOOT, BEFORE THE SERVER — `alembic upgrade head` is
# idempotent (a no-op on an already-current schema) and the chain is linear.
# Without this, every image update that carries a new migration boots uvicorn
# cleanly against a stale schema and 500s on first touch of the new column —
# on a machine (the client's iMac) with nobody there to `docker compose exec`.
# The README's manual step remains valid; this just stops it being load-bearing.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
