FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

# curl for the healthcheck; libsql wheels (via sqlalchemy-libsql) need no build tools.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the project and install it EDITABLE so config/scheduling.yaml and the
# dashboard HTML resolve from the source tree at runtime (the schedule loader
# looks for config/ relative to the package). .dockerignore keeps .env out.
COPY . .
RUN pip install --upgrade pip && pip install -e .

# Non-root runtime user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /srv/app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Default process is the API + dashboard; the scheduler service overrides CMD.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
