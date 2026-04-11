FROM python:3.12-slim

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Install uv for fast dependency resolution ─────────────────────────────────
RUN pip install --no-cache-dir uv

WORKDIR /app

# ── Dependencies (cached layer) ───────────────────────────────────────────────
COPY pyproject.toml .
RUN uv pip install --system --no-cache ".[dev]"

# ── Source ────────────────────────────────────────────────────────────────────
COPY lcas/ ./lcas/

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN useradd --uid 65532 --no-create-home --shell /bin/false agent
USER agent

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Expects the user to COPY their agent.py into /app/agent.py
CMD ["python", "-m", "uvicorn", "lcas.server:get_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
