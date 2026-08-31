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

# DATABASE_BACKEND=sqlite のとき、明示的に SQLITE_DATABASE_PATH を渡し忘れても
# 下の VOLUME にきちんと書き込まれるようにする既定値（config.py 側の相対パス
# デフォルト "data/news_summarizer.db" だと /app/data 配下＝コンテナの書き込み層に
# 書かれてしまい、ボリュームを永続化しているつもりでも再作成時に消える）。
ENV SQLITE_DATABASE_PATH=/data/news_summarizer.db

VOLUME ["/data"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["run-once"]
