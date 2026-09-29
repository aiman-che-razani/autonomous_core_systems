# Pinned by digest (multi-arch index for 3.12-slim, 2026-09-28); bump deliberately.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
WORKDIR /app
COPY pyproject.toml uv.lock ./
# uv from its official image, pinned by digest (not an unhashed `pip install uv`).
COPY --from=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 /uv /usr/local/bin/uv
RUN uv sync --frozen --no-dev --no-install-project
COPY greyqueue ./greyqueue
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev && useradd --create-home greyqueue
USER greyqueue
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "greyqueue.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8810"]
