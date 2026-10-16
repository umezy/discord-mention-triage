#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""デスクトップ版へ取り込んだ（desktop_import）あと、見ていたセッションへ表示を戻す（ADR-0035）。

`claude --desktop --resume <ID>` は「デスクトップ版で開く」命令で、デスクトップ版は、取り込んだセッションを必ず開く
（取り込むだけで開かない指定は無い）。そのままでは、ほかのセッションで作業していても、仕分けのたびに表示が切り替わる。
そこで、取り込む前に見ていたセッションを控えておき、取り込んだセッションが開いたら、すぐ
`claude://code/continue?session=<控えた ID>` で戻す。

見ていたセッションは、デスクトップ版がセッションごとに残す記録
（%APPDATA%\\Claude\\claude-code-sessions\\<アカウント>\\<組織>\\local_<ID>.json）の lastFocusedAt が、いちばん新しいもの。
取り込んだセッションが開いたことも、その記録に lastFocusedAt が付いたことで知る（開いてから 1 秒ほどで書かれる）。
どちらもデスクトップ版の中の形なので、変わって読めなくなったら、戻すのをやめるだけにする（取り込みは続ける）。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

FOCUSED_RE = re.compile(rb'"lastFocusedAt"\s*:\s*(\d+)')
HEAD_BYTES = 16384  # lastFocusedAt は記録の頭の方にある。本文の入った大きい記録を、丸ごと読まない
OPEN_WAIT_SEC = 30  # 取り込んだセッションが開くのを待つ上限
POLL_SEC = 0.1


def sessions_root(env=None) -> Path | None:
    """デスクトップ版のセッションの記録の置き場。Windows 以外は None（戻さない）。"""
    env = os.environ if env is None else env
    if os.name != "nt" or not env.get("APPDATA"):
        return None
    return Path(env["APPDATA"]) / "Claude" / "claude-code-sessions"


def focused_at(path: Path) -> int:
    """その記録の lastFocusedAt（ミリ秒）。まだ開かれていない、読めない、なら 0。"""
    try:
        with open(path, "rb") as f:
            m = FOCUSED_RE.search(f.read(HEAD_BYTES))
    except OSError:
        return 0
    return int(m.group(1)) if m else 0


def viewed_session(root: Path | None) -> str:
    """いちばん新しく開かれたセッション（local_<ID>）。見つからなければ ""。"""
    best, best_at = "", 0
    if root is None:
        return best
    for path in root.glob("*/*/local_*.json"):
        at = focused_at(path)
        if at > best_at:
            best, best_at = path.stem, at
    return best


def open_link(url: str) -> None:
    os.startfile(url)  # noqa: S606 - claude:// をデスクトップ版に渡す。Windows だけ（sessions_root が None なら呼ばない）


class DesktopView:
    """取り込みの前に見ていたセッションを控え（remember）、取り込んだセッションが開いたら、そこへ戻す（go_back）。"""

    def __init__(self, root: Path | None = None, opener=open_link, sleep=time.sleep, clock=time.monotonic,
                 wait_sec: float = OPEN_WAIT_SEC):
        self.root = sessions_root() if root is None else root
        self.opener, self.sleep, self.clock, self.wait_sec = opener, sleep, clock, wait_sec

    def remember(self) -> str:
        return viewed_session(self.root)

    def go_back(self, viewed: str, cli_session_id: str) -> str:
        """戻した結果（実行記録に書く短い文字列）: ok / unsupported / nothing_viewed / not_opened。"""
        if self.root is None:
            return "unsupported"
        imported = "local_" + cli_session_id
        if not viewed or viewed == imported:
            return "nothing_viewed"
        deadline = self.clock() + self.wait_sec
        while not any(focused_at(p) for p in self.root.glob("*/*/%s.json" % imported)):
            if self.clock() >= deadline:
                return "not_opened"  # デスクトップ版が開かなかった（エラーを出した、など）。表示は動いていない
            self.sleep(POLL_SEC)
        self.opener("claude://code/continue?session=" + viewed)
        return "ok"
