FROM python:3.12-slim
WORKDIR /app
# Copy source BEFORE the editable install so the `app` package exists at install
# time — otherwise the editable mapping freezes empty and any non-uvicorn entrypoint
# (alembic, pytest, the worker) fails with ModuleNotFoundError. .dockerignore keeps
# the host .venv/caches out of the context.
COPY . .
RUN pip install --no-cache-dir -e ".[dev]"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
