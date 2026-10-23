#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OS のタスクスケジューラが数分おきに起動する、ただ 1 つの入口。

やること（1〜3 は AI なし。新着が無い回は、検索を数回引いて終わる）:
  1. 受付: Discord の検索を引いて、自分宛ての新着を inbox/ に書き出す（watcher/search.py。Discord へは GET だけ）
  2. 途中で落ちた回の掃除と、inbox の claim（古い順に 1 件だけ。ADR-0041）
  3. 異常の通知（Discord を読めない状態が続いている、3 回失敗して退避した）
  4. claim できたものがあるときだけ、その場で Claude を 1 回動かして、結果を投稿する（run_claude.triage）

1 回の起動で仕分けるのは 1 件だけ。別々のメンションを、届いた時刻が近いというだけで、同じセッションにしないため。
同じ流れの続きの件は、次の回が、前の件のセッションに書き足す（continuation.py）。
同時に動くのは 1 つだけ（ProcessLock）。Claude が動いている間（数分）に起動された回は、何もせずに終わる。
その間の新着は inbox に溜まり、Claude が終わったあとの回が、1 件ずつ仕分ける。

使い方（タスクスケジューラ）:
  python -X utf8 pipeline/dispatch.py --user NAME
診断:
  python -X utf8 pipeline/dispatch.py --user NAME --dry-run            検索は引くが、状態を進めず、Claude も起動しない
  python -X utf8 pipeline/dispatch.py --user NAME --backfill-hours 6   過去 6 時間ぶんも拾う（初回や、長く止めていたあと）

出力（ASCII の 1 行）:
  OK POLL=ok FOUND=0 EMITTED=0 CLAIMED=0 RAN=0
  OK POLL=ok FOUND=2 EMITTED=1 SKIP=reacted:1 CLAIMED=1 RAN=1 RESULT=done
  OK POLL=ok FOUND=1 EMITTED=0 RETRY=1 CLAIMED=0 RAN=0              1 件を読めなかった。次の回でやり直す（5 回で諦める）
  OK POLL=down:http_401_guilds CLAIMED=0 RAN=0                       Discord を読めていない（続くと通知する）
  OK POLL=resident_watcher CLAIMED=0 RAN=0                          常駐の受付（古い版）が、まだ動いている。検索は引かない
  OK SKIPPED=locked                                                 前の回が、まだ動いている
終了コード: 0 正常 / 2 設定の不備 / 3 異常を知らせようとして、送れなかった / 4 このスクリプト自身の故障
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

from common import add_target_args, ascii_hint, resolve  # noqa: E402
import notify  # noqa: E402
import run_claude  # noqa: E402
import tick  # noqa: E402
from watcher import config as wconfig  # noqa: E402
from watcher import search  # noqa: E402

PER_RUN = 1  # 1 回の起動で claim する件数（ADR-0041）
POLL_DOWN_SEC = 15 * 60  # Discord を読めない状態が、これだけ続いたら知らせる
POLL_ALERT_COOLDOWN_SEC = 60 * 60
RESIDENT_FRESH_SEC = 180  # 常駐版の受付は、30 秒おきに生存信号を書いていた。これより新しければ、まだ動いている
AUTH_ERRORS = ("http_401", "http_403", "no_token")  # 待っても直らない。すぐ知らせる
ALERT_COOLDOWN = {"failed": 6 * 3600}
SKIP_ORDER = ("blocked", "guild", "reacted", "replied", "deleted", "emit_failed")


class ProcessLock:
    """同時に 1 つだけ動かすための、OS のファイルロック。

    持ち主のプロセスが終われば（落ちても）、OS がロックを外す。「古いロックを何秒で捨てるか」を決めなくてよい。
    Claude を動かしている間（最長で claude_timeout_sec）も、持ったままにする。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.held = False
        self._f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._f, self.held = f, True
        except OSError:
            f.close()  # ほかの回が持っている
        return self

    def __exit__(self, *exc):
        if self._f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._f.close()
            self._f, self.held = None, False


def poll_discord(cfg, watcher, home, dry_run: bool, backfill_hours: float) -> search.PollResult:
    """受付を 1 回動かす。ボットのトークンは、ここで読んで受付に渡すだけ（環境変数には置かない。Claude にも渡らない）。"""
    try:
        token = wconfig.load_token(cfg)
    except wconfig.ConfigError:
        res = search.PollResult()
        res.fail("no_token")
        return res
    return search.poll_once(cfg, watcher, home, token, backfill_hours=backfill_hours, dry_run=dry_run)


def poll_alert_due(home, res: search.PollResult, now: float, dry_run: bool) -> tuple:
    """(通知が要るか, 何分前から読めていないか)。同じ停止では、1 時間に 1 回しか知らせない。"""
    if res.ok:
        if not dry_run:
            home.clear_alert("poll_down")
        return False, 0
    since = (home.read_poll() or {}).get("fail_since") or now
    minutes = max(0, int((now - float(since)) / 60))
    urgent = res.error.startswith(AUTH_ERRORS)
    if dry_run or not (urgent or now - float(since) >= POLL_DOWN_SEC):
        return False, minutes
    return home.should_alert("poll_down", POLL_ALERT_COOLDOWN_SEC, now), minutes


def alert(home, key: str, text: str, poster) -> bool:
    """同じ内容を連打しない通知。送れなかったら False。"""
    cooldown = ALERT_COOLDOWN.get(key.split(":")[0], 3600)
    if not home.should_alert(key, cooldown):
        return True
    ok, _ = notify.send(home, text, mention=True, poster=poster)
    return ok


def run(args, runner=run_claude.triage, poster=notify.post, poller=poll_discord, now: float | None = None) -> tuple:
    home, watcher = resolve(args)
    home.ensure()
    cfg = None if args.home else wconfig.load(args.config)
    now = time.time() if now is None else now
    dry_run = args.dry_run
    alert_failed = False

    with ProcessLock(home.root / "dispatch.lock") as lock:
        if not lock.held:
            return "OK SKIPPED=locked", 0

        # 1. 受付（--home だけを渡されたときは、誰の設定で引くか分からないので、引かない）
        polled = None
        # 常駐版の受付が、まだ動いている（フォルダだけ新しくして、自動起動を登録し直していない）間は、引かない。
        # 両方が拾うと、同じメンションが 2 通届く。`python triage.py install` が古い受付を止めると、次の回から引き始める
        resident = home.resident_watcher_alive(now, RESIDENT_FRESH_SEC)
        if watcher is not None and cfg is not None and not resident:
            polled = poller(cfg, watcher, home, dry_run, getattr(args, "backfill_hours", 0) or 0)
            due, minutes = poll_alert_due(home, polled, now, dry_run)
            if due:
                sent, _ = notify.send(
                    home,
                    "⚠ Discord を読めていません（%s、%d 分前から）。その間のメンションは、まだ拾えていません。"
                    "ボットのトークン（.discord-mention-triage/.env）と、ネットワークを確認してください。"
                    "直れば、止まっていた間の分も自動で拾います（最大 %d 時間）。docs/setup.md の「止まったとき」を見てください。"
                    % (polled.error, minutes, cfg.catchup_max_age_hours),
                    mention=True,
                    poster=poster,
                )
                alert_failed = not sent

        # 2. 掃除と claim（認証切れ・枠切れの直後は、新しい仕分けを始めない。届いた分は inbox に溜めておく）
        until, blocked_reason = run_claude.blocked_state(home)
        blocked = until > now
        got = tick.collect(home, now, dry_run, limit=0 if blocked else PER_RUN)

        # 3. 人手が要る状態の通知
        for name in got["failed"]:
            alert(home, "failed:" + name, "⚠ 仕分けに3回失敗したので failed/ に退避しました（%s）。中身を見ないと直りません。" % name, poster)

        # 4. 仕分け（新着があるときだけ。Claude が動くのは、ここだけ）
        ran, result = 0, None
        claimed = got["claimed"]
        if claimed and not dry_run:
            if watcher is None or cfg is None:
                return "ERROR=need_user_to_run", 2  # --home だと、誰の設定で Claude を動かすか分からない
            result = runner(home, watcher, cfg, claimed, poster=poster)
            ran = 1

    parts = ["OK"]
    if resident:
        parts.append("POLL=resident_watcher")
    if polled is not None:
        parts.append("POLL=ok" if polled.ok else "POLL=down:%s" % (polled.error or "unknown"))
        if polled.ok or polled.found:
            parts += ["FOUND=%d" % polled.found, "EMITTED=%d" % polled.emitted]
        if polled.retrying:
            parts.append("RETRY=%d" % polled.retrying)
        skip_parts = ["%s:%d" % (k, polled.skips[k]) for k in SKIP_ORDER if polled.skips.get(k)]
        if skip_parts:
            parts.append("SKIP=" + ",".join(skip_parts))
        if polled.first_run:
            parts.append("FIRST=1")
    parts += ["CLAIMED=%d" % len(claimed), "RAN=%d" % ran]
    if result is not None:
        parts.append("RESULT=%s" % (result.get("reason") or ("done" if result.get("ok") else "failed")))
    if blocked:
        parts.append("BLOCKED=%s" % (blocked_reason or "unknown"))
    if got["requeued"]:
        parts.append("REQUEUED=%d" % len(got["requeued"]))
    if got["failed"]:
        parts.append("FAILED=%d" % len(got["failed"]))
    if dry_run:
        parts.append("dry=1")
    if alert_failed:
        parts.append("ALERT_FAILED=1")
    return " ".join(parts), 3 if alert_failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--dry-run", action="store_true", help="検索は引くが、状態を進めず、Claude も起動しない")
    ap.add_argument("--backfill-hours", type=float, default=0,
                    help="過去 N 時間ぶんのメンションも拾う（既定: 0 = 前回の続きから。初回は、いまから）")
    args = ap.parse_args(argv)
    try:
        line, code = run(args)
    except wconfig.ConfigError as e:
        print("ERROR=config %s" % ascii_hint(e))
        return 2
    except Exception as e:  # noqa: BLE001 - 常に1行で返す
        print("ERROR=dispatch %s" % type(e).__name__)
        return 4
    print(line)
    return code


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="ascii", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="ascii", errors="replace")
    sys.exit(main())
