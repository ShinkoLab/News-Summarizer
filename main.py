import argparse

import config as config_module
from config import reload_config
from logger import setup_logging
from pipeline import RunOptions, run_pipeline


def main():
    parser = argparse.ArgumentParser(description="AI News Summarizer")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ドライラン（Discord出力や既読化、DB保存を行わない）",
    )
    parser.add_argument(
        "--output",
        type=str,
        choices=["discord", "db", "all"],
        nargs="+",
        help="ドライラン中でも強制的に出力するターゲット（例: --output discord）",
    )
    parser.add_argument(
        "--source",
        type=str,
        choices=["rss", "email", "all"],
        default="all",
        help="取得先ソースの指定",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="設定ファイルのパス",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="LLMの出力をストリーミング表示する（デバッグ用）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="embedding・類似度判定の詳細情報を表示する（デバッグ用）",
    )
    args = parser.parse_args()

    if args.config != "config.yaml":
        reload_config(args.config)

    setup_logging(debug=args.debug)

    options = RunOptions(
        dry_run=args.dry_run,
        forced_outputs=frozenset(args.output) if args.output else frozenset(),
        sources=frozenset({args.source}),
        stream=args.stream,
        debug=args.debug,
    )

    run_pipeline(config_module.config, options)


if __name__ == "__main__":
    main()
