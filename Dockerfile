FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1

WORKDIR /app
RUN pip install --no-cache-dir "uv==0.5.30"
COPY pyproject.toml uv.lock README.md ./
COPY torrwatch torrwatch
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system torrwatch \
    && useradd --system --gid torrwatch --home-dir /app --shell /usr/sbin/nologin torrwatch \
    && mkdir /app /data \
    && chown torrwatch:torrwatch /app /data

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=torrwatch:torrwatch alembic alembic
COPY --chown=torrwatch:torrwatch torrwatch torrwatch
COPY --chown=torrwatch:torrwatch alembic.ini ./

USER torrwatch
EXPOSE 8080
CMD ["torrwatch-web"]
