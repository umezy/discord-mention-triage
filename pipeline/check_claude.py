#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude を、本番と同じコマンド・同じ環境変数で1回だけ動かして確かめる（小さいモデルに「OK」と答えさせるだけ）。

メンションは読まない。記録も残さない。サブスクの利用枠を、ほんの少し使う。

使い方:
  python -X utf8 pipeline/check_claude.py --user NAME              いまの設定のまま確かめる
  python -X utf8 pipeline/check_claude.py --user NAME --isolated   `claude` のログインが無い状態で確かめる

--isolated は、空の設定フォルダ（CLAUDE_CONFIG_DIR）を指して動かす。PC に保存された `claude` のログインを使えないので、
これが通れば「専用の長期トークンだけで動く ＝ `claude` でどのアカウントにログインしていても動く」と言える。

出力（常に ASCII の1行）:
  OK auth=token isolated=1 model=haiku
  FAILED auth=cli_login isolated=0 model=haiku reason=claude_auth error=Not logged in ...
auth は、claude がどの資格情報で動いたか（token = 専用の長期トークン / cli_login = PC に保存された `claude` のログイン）。
終了コード: 0 動いた / 1 動かなかった / 2 設定の不備。
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import sys
import tempfile

from common import add_target_args, resolve  # noqa: E402
import claude_bin  # noqa: E402
import run_claude  # noqa: E402
from watcher import config as wconfig  # noqa: E402

CHECK_MODEL = "haiku"
CHECK_TIMEOUT_SEC = 120
CHECK_PROMPT = "Reply with the single word OK."


def check_command(claude: str, cfg, watcher, home) -> list:
    """本番と同じ引数。違いは、小さいモデルにすること・effort を付けないこと・記録を残さないこと、だけ。"""
    cmd = run_claude.build_command(
        claude,
        dataclasses.replace(cfg, keep_transcripts=False),
        dataclasses.replace(watcher, triage_model=CHECK_MODEL),
        home,
    )
    i = cmd.index("--effort")
    del cmd[i:i + 2]
    return cmd


def check(home, watcher, cfg, isolated: bool = False, command=None) -> dict:
    """結果の辞書を返す（ok / auth / isolated / reason / error）。例外は投げない。"""
    res = {"ok": False, "auth": "", "isolated": isolated, "reason": "", "error": ""}
    if command is None:
        claude = claude_bin.find_claude(cfg.claude_bin)
        if not claude:
            return dict(res, reason="claude_not_found")
        command = check_command(claude, cfg, watcher, home)
    env = run_claude.child_env(home)
    res["auth"] = run_claude.auth_source(env)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as empty:
        if isolated:
            env["CLAUDE_CONFIG_DIR"] = empty
        code, stdout, stderr, timed_out = run_claude.run_process(
            command, CHECK_PROMPT, home.root / "run", CHECK_TIMEOUT_SEC, env=env)
    if timed_out:
        return dict(res, reason="timeout")
    info = run_claude.parse_result(stdout)
    if code == 0 and info["is_error"] is False:
        return dict(res, ok=True, reason="done")
    blob = info["text"] + " " + stderr[:300]
    reason = ("claude_auth" if run_claude.AUTH_RE.search(blob) else "claude_limit" if run_claude.LIMIT_RE.search(blob)
              else "claude_not_started" if run_claude.never_ran(info) else "claude_exit_%s" % code)
    # 指示は固定の1行で、メンションは読んでいない。エラー文に本文は混ざらない
    error = " / ".join(x for x in (run_claude.clip_error(info["text"] or stderr), info["api_error"]) if x)
    return dict(res, reason=reason, error=error)


def format_line(res: dict) -> str:
    parts = ["OK" if res["ok"] else "FAILED", "auth=%s" % (res["auth"] or "none"),
             "isolated=%d" % (1 if res["isolated"] else 0), "model=%s" % CHECK_MODEL]
    if not res["ok"]:
        parts.append("reason=%s" % res["reason"])
        if res["error"]:
            parts.append("error=%s" % res["error"])
    return " ".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--isolated", action="store_true", help="`claude` のログインが無い状態で確かめる（専用トークンだけで動くか）")
    args = ap.parse_args(argv)
    try:
        home, watcher = resolve(args)
        if watcher is None:
            print("ERROR=need_config (check_claude needs --user/--config, not --home)")
            return 2
        cfg = wconfig.load(args.config)
    except wconfig.ConfigError as e:
        print("ERROR=config %s" % e)
        return 2
    res = check(home, watcher, cfg, isolated=args.isolated)
    print(format_line(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="ascii", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="ascii", errors="replace")
    sys.exit(main())
