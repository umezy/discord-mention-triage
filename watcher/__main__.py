"""python -m watcher [--config PATH] [--user NAME] [--check] [--dry-run] [--backfill-hours N]

受付を 1 回だけ動かす（手で確かめるとき用）。ふだんは pipeline/dispatch.py が、数分おきに同じことをする。

  --check     設定とトークンの有無だけ確認して終わる（Discord に繋がない）
  --dry-run   検索は引くが、inbox/ にも、続きの位置にも、何も書かない

出力（ASCII の 1 行）:
  OK config=... watchers=me token=present                 （--check）
  OK FOUND=1 EMITTED=1
  ERROR=poll http_401_guilds
終了コード: 0 正常 / 2 設定・トークンの不備 / 3 Discord を読めなかった
"""

from __future__ import annotations

import argparse
import sys

from . import __version__, config
from .store import Home


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="watcher", description="Discord メンションの読み取り専用受付（検索を 1 回引く）")
    ap.add_argument("--config", help="config.json のパス（省略時は環境変数 MENTION_TRIAGE_CONFIG か既定の場所）")
    ap.add_argument("--user", help="config.json の watchers[].name（省略時は最初の 1 人）")
    ap.add_argument("--check", action="store_true", help="設定とトークンの有無だけ確認して終わる（接続しない）")
    ap.add_argument("--dry-run", action="store_true", help="検索は引くが、何も書かない")
    ap.add_argument(
        "--backfill-hours",
        type=float,
        default=0,
        help="過去 N 時間ぶんのメンションも拾う（既定: 0 = 前回の続きから。初回は、いまから）",
    )
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    try:
        cfg = config.load(args.config)
        token = config.load_token(cfg)
        w = next((x for x in cfg.watchers if x.name == args.user), None) if args.user else cfg.watchers[0]
        if w is None:
            raise config.ConfigError("watchers に %s が無い" % args.user)
    except config.ConfigError as e:
        print("ERROR=config %s" % e, file=sys.stderr)
        return 2

    if args.check:
        print(
            "OK config=%s watchers=%s token=present"
            % (cfg.path, ",".join(x.name for x in cfg.watchers))
        )
        return 0

    from . import search  # 通信するモジュールは、ここで初めて読み込む

    res = search.poll_once(cfg, w, Home(w.home).ensure(), token, backfill_hours=args.backfill_hours,
                           dry_run=args.dry_run)
    if not res.ok:
        print("ERROR=poll %s" % (res.error or "unknown"))
        return 3
    parts = ["OK", "FOUND=%d" % res.found, "EMITTED=%d" % res.emitted]
    if res.retrying:
        parts.append("RETRY=%d" % res.retrying)
    if res.skips:
        parts.append("SKIP=" + ",".join("%s:%d" % kv for kv in sorted(res.skips.items())))
    if res.first_run:
        parts.append("FIRST=1")
    if args.dry_run:
        parts.append("dry=1")
    print(" ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
