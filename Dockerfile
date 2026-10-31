FROM ghcr.io/astral-sh/uv:0.11.29-python3.12-trixie-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# entrypoint.sh を実行可能にし、/data を named volume 用に用意する（後者は
# Docker Compose で SQLite バックエンドの保存先にする想定。named volume は
# 初回作成時にイメージ側のディレクトリの所有権を引き継ぐため、USER nobody に
# 切り替える前に nobody 書き込み可能にしておく）。
RUN chmod +x /app/docker/entrypoint.sh && mkdir -p /data && chown nobody:nogroup /data

USER nobody

# SQLite の保存先 /data は docker-compose.yml が SQLITE_DATABASE_PATH で明示する。
# イメージ全体の ENV にすると、Cloud Run など環境変数のみの実行の既定の保存先まで
# 変わってしまうため、ここでは設定しない。
VOLUME ["/data"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["run-once"]
