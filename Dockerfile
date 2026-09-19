FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# uv binary for reproducible installs from uv.lock
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install dependencies first (better layer caching).
# README.md is referenced by pyproject.toml, so it must be present.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Application source. dashboard.html must sit next to dashboard.py
# (dashboard.py loads it from the same directory).
COPY main.py database.py storage.py schemas.py errors.py worker.py dashboard.py dashboard.html ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Create a non-root user and give it ownership of the app dir
# (default SQLite DB sqlite:///./agent-relay.db is created here).
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# stdlib-only check so no curl dependency is needed on slim
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# NOTE: --host 0.0.0.0 is required so docker -p publishing works
# (uvicorn defaults to 127.0.0.1, which stays container-local).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
