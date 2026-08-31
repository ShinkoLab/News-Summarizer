FROM ghcr.io/astral-sh/uv:0.11.29-python3.12-trixie-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev
RUN chmod +x /app/docker/entrypoint.sh

# Docker Compose では /data を named volume でマウントし、SQLite バックエンドの
# 保存先にする。named volume は初回作成時にイメージ側のディレクトリの所有権を
# 引き継ぐため、USER nobody に切り替える前に nobody 書き込み可能にしておく。
RUN mkdir -p /data && chown nobody:nogroup /data

USER nobody

VOLUME ["/data"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["run-once"]
