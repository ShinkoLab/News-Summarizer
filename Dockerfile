FROM ghcr.io/astral-sh/uv:0.11.29-python3.12-trixie-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

USER nobody

CMD ["/app/.venv/bin/python", "main.py"]
