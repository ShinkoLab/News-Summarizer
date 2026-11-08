import logging
import sys

from config import config


_APP_NAMESPACES = ("main", "config", "logger", "pipeline", "models", "summarizer", "fetchers", "outputs")

# リクエストのたびに URL を INFO / DEBUG で出すライブラリ。Discord の webhook の URL は
# それ自体が投稿の認証情報なので、logging.level が INFO 以下でも WARNING 未満は出さない。
_URL_LOGGING_LIBRARIES = ("httpx", "httpcore")


def setup_logging(debug: bool = False) -> None:
    """ロギングシステムを初期化する。config.yaml の logging.level を参照する。

    debug=True のとき、アプリ固有のロガー（_APP_NAMESPACES）だけ DEBUG に昇格する。
    サードパーティライブラリ（openai 等）はconfig指定レベルのまま維持される。
    httpx / httpcore はリクエストの URL をログに出すため、常に WARNING 以上に絞る。
    """
    level_str = config.logging.level.upper()
    level = getattr(logging, level_str, logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)

    for name in _URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    if debug:
        for ns in _APP_NAMESPACES:
            logging.getLogger(ns).setLevel(logging.DEBUG)


def get_logger(name: str) -> logging.Logger:
    """モジュール名を渡してロガーを取得する。"""
    return logging.getLogger(name)
