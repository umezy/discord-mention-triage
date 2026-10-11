#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手動・診断用: 本文ファイルを、本人専用チャンネルへ Webhook で投稿する。

自動運転では dispatch.py / run_claude.py が同じ部品（notify.py）で投稿する。これは、Webhook の設定や
分割の見え方を手で確かめるための入口。

【なぜ Webhook か】
投稿に使えるのは Webhook だけ。ユーザートークンでの投稿は規約違反（自己ボット）で、受付のボットは
読み取り専用にしてあるので書く権限を持たない。投稿先は「本人だけが見る場所」に限る。

【罠1】Windows で日本語を print すると文字化けする → 本文は UTF-8 のファイルで渡す。引数で渡さない。
【罠2】1メッセージ 2000 文字の上限 → 切り捨てず分割して連投する。本文に `%%SPLIT%%` だけの行を置くと、
       そこで必ず切れる（件ごとに切りたいとき）。置かなければ行単位で自動分割する。
【罠3】`@名前` と書いてもメンションにならない。数値 ID の `<@1234>` でしか通知が飛ばない。
       メンションは先頭の1通だけに付ける（連投のたびに通知が飛ぶと邪魔なため）。

使い方:
    python post_digest.py --body-file <UTF-8のテキスト> [--user NAME]
    python post_digest.py --body-file <...> --dry-run     # 投稿せず分割結果だけ確認
    python post_digest.py --body-file <...> --no-mention  # 今回だけメンションを外す
"""

from __future__ import annotations

import argparse
import io
import sys

from common import add_target_args, resolve
from notify import CHUNK_LIMIT, DISCORD_LIMIT, SPLIT_MARK, post, read_env_file, read_target, send, split_body  # noqa: F401


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--body-file", required=True, help="投稿本文（UTF-8）。引数で日本語を渡すと Windows で壊れる")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-mention", action="store_true")
    args = ap.parse_args(argv)

    home, _ = resolve(args)
    with open(args.body_file, encoding="utf-8-sig") as f:  # メモ帳の BOM を、投稿の先頭に混ぜない
        body = f.read().strip()
    if not body:
        print("ERROR=empty_body")
        return 1

    url, mention, env_path = read_target(home)
    if not url:
        print("ERROR=no_webhook tried=%s" % env_path)
        print("HINT: add a line DIGEST_WEBHOOK=https://discord.com/api/webhooks/... to that file")
        return 2

    if args.dry_run:
        chunks = split_body(body)
        mention = "" if args.no_mention else mention
        print("DRY_RUN chunks=%d mention=%s" % (len(chunks), mention or "none"))
        for i, c in enumerate(chunks):
            print("--- chunk %d (%d chars)" % (i + 1, len(c)))
            print(c)
        return 0

    ok, line = send(home, body, mention=not args.no_mention)
    print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
