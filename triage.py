#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""discord-mention-triage の、ただ 1 つのコマンド。初めての設定から、確認・実行・自動起動の登録・状態の表示まで。

  python triage.py init        質問に答えて、設定と鍵の置き場を作る（~/.discord-mention-triage/）
  python triage.py check       設定・ボットの鍵・Discord の読み取り・Webhook・Claude を、順に確かめる
  python triage.py run         1 回動かす（自動起動が 2 分おきにやっているのと同じ。--dry-run で空実行）
  python triage.py install     自動起動を登録する（解除は uninstall）
  python triage.py status      いまの状態（自動起動、Discord を読めているか、溜まっている件、最近の記録）

人が読むためのコマンドなので、日本語で出す。機械が読む 1 行（dispatch.py の出力、dispatch.log）は ASCII のまま。
鍵（ボットのトークン、Webhook の URL、Claude の合鍵）は、init で貼るときも画面に出さず、どのコマンドも表示しない。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
for _p in (REPO_ROOT, REPO_ROOT / "pipeline"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import check_claude  # noqa: E402
import dispatch  # noqa: E402
import notify  # noqa: E402
import run_claude  # noqa: E402
from watcher import config as wconfig  # noqa: E402
from watcher import search  # noqa: E402
from watcher.store import Home  # noqa: E402

TASK_NAME = "mention-triage"
LEGACY_TASKS = ("mention-triage-watcher", "mention-triage-dispatch")
ID_RE = re.compile(r"^\d{15,22}$")
WEBHOOK_RE = re.compile(r"^https://(?:[a-z]+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
MAX_TRIES = 3

# Discord を読めなかった理由（watcher/rest.py の RestError）→ 何をすればよいか
POLL_HINTS = (
    ("no_token", "ボットのトークンが読めない。.discord-mention-triage\\.env の DISCORD_BOT_TOKEN を確かめる"),
    ("http_401", "ボットのトークンが違う。Developer Portal で作り直して、.env を書き換える"),
    ("http_403", "ボットに、そのサーバーの「メッセージ履歴を読む」権限が無い"),
    ("http_202", "Discord が、検索の索引を作っている最中。しばらく待って、もう一度"),
    ("http_429", "Discord に、要求が多すぎると言われた。しばらく待って、もう一度"),
    ("network", "Discord に繋がらない。ネットワークを確かめる"),
)


@dataclass
class Deps:
    """外の世界との接点。テストでは、偽物に差し替える。"""

    ask: object = input
    ask_secret: object = getpass.getpass
    out: object = print
    state_root: Path = field(default_factory=lambda: Path.home() / ".discord-mention-triage")
    legacy_configs: tuple = (REPO_ROOT / "config" / "config.json",)  # 古い置き場。新しい置き場に無いときだけ読む
    poll: object = None  # (cfg, watcher, home, token) -> search.PollResult。検索を 1 回引くだけ（何も書かない）
    claude_check: object = None  # (home, watcher, cfg) -> dict（check_claude.check と同じ形）
    post_test: object = None  # (home) -> (成功したか, ASCII の 1 行)
    task_info: object = None  # (タスク名) -> dict | None
    powershell: object = None  # (引数の list) -> 終了コード
    now: object = time.time

    def __post_init__(self):
        self.poll = self.poll or (lambda cfg, w, home, token: search.poll_once(cfg, w, home, token, dry_run=True))
        self.claude_check = self.claude_check or (lambda home, w, cfg: check_claude.check(home, w, cfg))
        self.post_test = self.post_test or (lambda home: notify.send(
            home, "✅ discord-mention-triage の確認用の投稿です（`python triage.py check --post-test`）。", mention=True))
        self.task_info = self.task_info or query_task
        self.powershell = self.powershell or run_powershell


# --- Windows のタスクスケジューラ -----------------------------------------------------------------
def run_powershell(args: list) -> int:
    if os.name != "nt":
        return 127
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *args]).returncode


def query_task(name: str):
    """登録されていれば {"State", "LastRunTime", "LastTaskResult", "NextRunTime"}。無ければ None。Windows 以外は None。"""
    if os.name != "nt":
        return None
    script = (
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
        "if ($t) { $i = $t | Get-ScheduledTaskInfo; "
        "@{State=[string]$t.State; LastRunTime=[string]$i.LastRunTime; LastTaskResult=$i.LastTaskResult; "
        "NextRunTime=[string]$i.NextRunTime} | ConvertTo-Json -Compress }" % name
    )
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True, timeout=60)
        text = proc.stdout.decode("utf-8", "replace").strip()
        return json.loads(text) if text else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


# --- 設定を読む -------------------------------------------------------------------------------------
def find_config(deps: Deps, explicit) -> str:
    """--config か環境変数があれば、それ。無ければ、置き場（~/.discord-mention-triage/）→ 古い置き場（リポジトリの config/）。"""
    if explicit or os.environ.get("MENTION_TRIAGE_CONFIG"):
        return str(wconfig.find_config_path(explicit))
    candidates = [deps.state_root / "config.json", *deps.legacy_configs]
    for c in candidates:
        if c.is_file():
            return str(c)
    raise wconfig.ConfigError("config.json が見つからない。探した場所: " + " / ".join(str(c) for c in candidates))


def load(deps: Deps, args) -> tuple:
    """(cfg, 見張る人, Home)。"""
    cfg = wconfig.load(find_config(deps, getattr(args, "config", None)))
    name = getattr(args, "user", None)
    w = next((x for x in cfg.watchers if x.name == name), None) if name else cfg.watchers[0]
    if w is None:
        raise wconfig.ConfigError("watchers に %s が無い" % name)
    return cfg, w, Home(w.home)


# --- init -------------------------------------------------------------------------------------------
def _ask_valid(deps: Deps, prompt: str, valid, hint: str, default: str = "", secret: bool = False) -> str:
    """正しい形の答えが返るまで、MAX_TRIES 回まで聞き直す。空の答えは default（default が "" なら、空のまま通す）。

    答えを打つ人がいない（入力が閉じている）ときは、空の答えとして扱う。エラーで止めずに、何が足りないかを言えるようにするため。
    """
    for _ in range(MAX_TRIES):
        try:
            raw = (deps.ask_secret if secret else deps.ask)(prompt)
        except EOFError:
            raw = ""
        value = (raw or "").strip().strip('"').strip("'") or default
        if value == "" or valid(value):
            return value
        deps.out("  → " + hint)
    raise ValueError(hint)


def build_config(name: str, user_id: str, channel_id: str, read_dirs: list) -> dict:
    """init が書く config.json。項目の意味は docs/setup.md と config/config.example.json。"""
    return {
        "watchers": [{
            "name": name,
            "user_id": user_id,
            "guild_allowlist": [],
            "channel_blocklist": [channel_id],
            "context_before": 15,
            "context_after": 10,
            "include_everyone": False,
            "read_dirs": read_dirs,
            "channel_read_dirs": {},
            "pick_environment": False,
            "triage_model": "opus",
            "triage_effort": "xhigh",
            "pr_policy": {"allowed_orgs": [], "requester_role_ids": [], "allow_bot_requesters": False},
        }],
        "catchup_max_age_hours": 72,
        "search_overlap_minutes": 30,
        "claude_bin": "",
        "claude_timeout_sec": 1800,
        "keep_transcripts": True,
        "desktop_import": False,
        "desktop_link_page": "",
    }


def write_env(path: Path, title: str, pairs: list) -> bool:
    """.env を新しく作る。すでにあれば、触らない（鍵を上書きしない）。作ったら True。"""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# %s（discord-mention-triage）。チャットにも、リポジトリにも貼らない" % title]
    for key, value, note in pairs:
        if not value:
            lines.append("# %s  ← まだ書いていない。%s" % (key + "=", note))
        lines.append("%s=%s" % (key, value))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def cmd_init(args, deps: Deps) -> int:
    out = deps.out
    root = deps.state_root
    cfg_path = root / "config.json"
    if cfg_path.exists() and not args.force:
        out("すでに設定があります: %s" % cfg_path)
        out("作り直すときは `python triage.py init --force`（いまの config.json は、名前を変えて残します。鍵のファイルは触りません）。")
        return 1
    try:
        wconfig.parse({"state_root": str(root), "watchers": [{"name": "x", "user_id": "1"}]})
    except wconfig.ConfigError as e:
        out("この場所には置けません: %s" % e)
        return 2

    out("discord-mention-triage の設定を作ります。置き場: %s" % root)
    out("ID は、Discord の「ユーザー設定 → 開発者 → 開発者モード」を ON にして、右クリック →「ID をコピー」で取れます（古い画面では「詳細設定」の中）。")
    if args.no_input:
        # 何も聞かない。引数で渡されなかった項目は、空の答えとして扱う（名前は既定、フォルダは無し、鍵は空欄）
        deps = replace(deps, ask=lambda _prompt: "", ask_secret=lambda _prompt: "")
        out("質問はしません（--no-input）。鍵は、あとで、ファイルに書きます。")
    else:
        out("鍵（トークン・Webhook・合鍵）は、貼っても画面に出ません。あとで書くなら、何も入れずに Enter で飛ばせます。")
    out("")
    default_name = (getpass.getuser() or "").lower()
    default_name = default_name if wconfig.NAME_RE.match(default_name) else "me"
    try:
        name = args.name or _ask_valid(
            deps, "1/7 あなたの名前（英小文字と数字。フォルダ名に使う）[%s]: " % default_name,
            lambda v: bool(wconfig.NAME_RE.match(v)), "英小文字・数字・-_ で、32 文字以内にしてください", default_name)
        user_id = args.user_id or _ask_valid(
            deps, "2/7 あなたの Discord のユーザー ID（数字）: ", lambda v: bool(ID_RE.match(v)), "数字だけの ID を貼ってください")
        channel_id = args.channel_id or _ask_valid(
            deps, "3/7 結果を届ける、あなた専用チャンネルの ID（数字）: ", lambda v: bool(ID_RE.match(v)),
            "数字だけの ID を貼ってください")
        if not user_id or not channel_id:
            out("ユーザー ID と、専用チャンネルの ID は、省けません（専用チャンネルは、届いた結果を拾い直さないために要ります）。")
            if args.no_input:
                out("--no-input のときは、`--user-id 数字 --channel-id 数字` で渡してください。")
            return 2
        read_dir = args.read_dir if args.read_dir is not None else _ask_valid(
            deps, "4/7 秘書（Claude）が事実確認のために読んでよいフォルダ（無ければ Enter）: ",
            lambda v: Path(os.path.expandvars(os.path.expanduser(v))).is_dir(), "そのフォルダが見つかりません")
        token = webhook = claude_token = ""
        # 鍵のファイルが、すでにあれば、聞かない（上書きもしない）
        if (root / ".env").exists():
            out("5/7 ボットのトークン: %s が、すでにあるので、聞きません。" % (root / ".env"))
        else:
            token = _ask_valid(deps, "5/7 ボットのトークン: ", lambda v: len(v) >= 30 and " " not in v,
                               "トークンの形ではないようです（空白なしの長い文字列）", secret=True)
        if (root / name / ".env").exists():
            out("6/7・7/7 Webhook と Claude の合鍵: %s が、すでにあるので、聞きません。" % (root / name / ".env"))
        else:
            webhook = _ask_valid(deps, "6/7 専用チャンネルの Webhook の URL: ", lambda v: bool(WEBHOOK_RE.match(v)),
                                 "https://discord.com/api/webhooks/... の形で貼ってください", secret=True)
            claude_token = _ask_valid(deps, "7/7 Claude の合鍵（`claude setup-token` で出るもの）: ",
                                      lambda v: len(v) >= 30 and " " not in v, "合鍵の形ではないようです", secret=True)
    except ValueError:
        out("入力を確かめられなかったので、何も書かずに終わります。")
        return 2

    root.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        backup = cfg_path.with_name("config.json.bak-%s" % datetime.fromtimestamp(deps.now()).strftime("%Y%m%d-%H%M%S"))
        cfg_path.rename(backup)
        out("いままでの設定は、%s として残しました。" % backup.name)
    data = build_config(name, user_id, channel_id, [read_dir] if read_dir else [])
    wconfig.parse(dict(data, state_root=str(root)))  # 書く前に、読める形かを確かめる
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    made_bot = write_env(root / ".env", "ボットのトークン", [
        ("DISCORD_BOT_TOKEN", token, "Developer Portal → 対象アプリ → Bot → Reset Token")])
    made_user = write_env(root / name / ".env", "あなた専用の投稿先と、Claude の合鍵", [
        ("DIGEST_WEBHOOK", webhook, "専用チャンネルの設定 → 連携サービス → ウェブフック"),
        ("DIGEST_MENTION_USER_ID", user_id, ""),
        ("CLAUDE_CODE_OAUTH_TOKEN", claude_token, "黒い画面で `claude setup-token`")])

    out("")
    out("書きました:")
    out("  %s" % cfg_path)
    for made, path in ((made_bot, root / ".env"), (made_user, root / name / ".env")):
        out("  %s%s" % (path, "" if made else "  ← すでにあったので、触っていません"))
    gaps = [(path, label) for made, path, label, v in (
        (made_bot, root / ".env", "ボットのトークン", token),
        (made_user, root / name / ".env", "Webhook の URL", webhook),
        (made_user, root / name / ".env", "Claude の合鍵", claude_token)) if made and not v]
    if gaps:
        out("まだ書いていない鍵: %s。上のファイルをメモ帳で開いて、`=` の右に貼ってください。" % "、".join(label for _, label in gaps))
        for path in dict.fromkeys(path for path, _ in gaps):
            out('  notepad "%s"' % path)
    out("次は `python triage.py check` で確かめます。")
    return 0


# --- check ------------------------------------------------------------------------------------------
def poll_hint(error: str) -> str:
    for prefix, hint in POLL_HINTS:
        if error.startswith(prefix):
            return hint
    return "docs/setup.md の「止まったとき」を見る"


def cmd_check(args, deps: Deps) -> int:
    out = deps.out
    bad = 0

    def line(ok, text, hint=""):
        nonlocal bad
        bad += 0 if ok is not False else 1
        out("[%s] %s" % ({True: "OK", False: "NG", None: "--"}[ok], text))
        if hint:
            out("       → " + hint)

    try:
        cfg, w, home = load(deps, args)
    except wconfig.ConfigError as e:
        line(False, "設定: %s" % e, "まだ作っていなければ `python triage.py init`")
        return 1
    home.ensure()
    line(True, "設定: %s（見張る人: %s）" % (cfg.path, w.name))
    if not w.channel_blocklist:
        line(None, "channel_blocklist が空", "専用チャンネルの ID を入れないと、届いた結果を拾い直して、止まらなくなる")

    token = ""
    try:
        token = wconfig.load_token(cfg)
        line(True, "ボットのトークン: あり")
    except wconfig.ConfigError:
        line(False, "ボットのトークン: 無い", "%s に DISCORD_BOT_TOKEN=... を書く" % cfg.token_env_path)

    if token:
        res = deps.poll(cfg, w, home, token)
        if res.ok:
            line(True, "Discord を読める（検索を 1 回引いた。何も書いていない）")
        else:
            line(False, "Discord を読めない: %s" % res.error, poll_hint(res.error))

    url, mention, env_path = notify.read_target(home)
    if not url:
        line(False, "Webhook: 無い", "%s に DIGEST_WEBHOOK=... を書く" % env_path)
    else:
        env = notify.read_env_file(env_path)
        line(True, "Webhook: あり（メンションする相手: %s）" % (mention or "なし"))
        if not env.get("DIGEST_WEBHOOK"):
            line(None, "古い名前（DISCORD_WATCH_WEBHOOK）で書かれている", "いまは読めるが、DIGEST_WEBHOOK に書き換えておく")
        if args.post_test:
            ok, detail = deps.post_test(home)
            line(ok, "専用チャンネルへの確認用の投稿: %s" % detail)

    if args.no_claude:
        line(None, "Claude: 確かめていない（--no-claude）")
    else:
        res = deps.claude_check(home, w, cfg)
        if res["ok"] and res["auth"] == "token":
            line(True, "Claude: 動いた（専用の合鍵で）")
        elif res["ok"]:
            line(True, "Claude: 動いた（黒い画面の `claude` のログインで）")
            line(None, "専用の合鍵が無い", "`claude setup-token` の合鍵を %s に CLAUDE_CODE_OAUTH_TOKEN=... として書くと、"
                                   "ログインを切り替えても止まらなくなる" % env_path)
        else:
            hints = {
                "claude_not_found": "`claude` が見つからない。docs/setup.md の 0 章の手順で入れる",
                "claude_auth": "Claude にログインできていない。`claude setup-token` の合鍵を %s に書く" % env_path,
                "claude_limit": "Claude の利用枠に達している。時間をおいて、もう一度",
                "timeout": "時間切れ。ネットワークを確かめる",
            }
            line(False, "Claude: 動かなかった（%s）%s" % (res["reason"], " " + res["error"] if res.get("error") else ""),
                 hints.get(res["reason"], "docs/setup.md の「止まったとき」を見る"))

    info = deps.task_info(TASK_NAME)
    if info:
        line(True, "自動起動: 登録済み（%s）" % info.get("State", "?"))
    else:
        line(None, "自動起動: まだ登録していない", "確かめ終わったら `python triage.py install`")
    if home.resident_watcher_alive(deps.now(), dispatch.RESIDENT_FRESH_SEC) or any(deps.task_info(t) for t in LEGACY_TASKS):
        line(None, "古い版（常駐の受付）が、まだ動いている", "`python triage.py install` で、古い受付とタスクが止まり、新しい形に替わる")
    out("")
    out("すべて OK です。" if not bad else "NG が %d 件あります。上の「→」を直して、もう一度 `python triage.py check`。" % bad)
    return 0 if not bad else 1


# --- status -----------------------------------------------------------------------------------------
def _ago(deps: Deps, ts) -> str:
    try:
        sec = max(0, int(deps.now() - float(ts)))
    except (TypeError, ValueError):
        return "不明"
    if sec < 120:
        return "%d 秒前" % sec
    if sec < 2 * 3600:
        return "%d 分前" % (sec // 60)
    return "%d 時間前" % (sec // 3600)


def cmd_status(args, deps: Deps) -> int:
    out = deps.out
    try:
        cfg, w, home = load(deps, args)
    except wconfig.ConfigError as e:
        out("設定を読めません: %s" % e)
        return 1
    out("見張る人: %s（設定: %s）" % (w.name, cfg.path))

    info = deps.task_info(TASK_NAME)
    if info:
        out("自動起動: 登録済み（状態 %s / 前回 %s / 次回 %s）"
            % (info.get("State", "?"), info.get("LastRunTime") or "-", info.get("NextRunTime") or "-"))
    else:
        out("自動起動: 登録されていない（`python triage.py install`）")
    for old in LEGACY_TASKS:
        if deps.task_info(old):
            out("  古いタスクが残っている: %s（`python triage.py install` が片づける）" % old)
    if home.resident_watcher_alive(deps.now(), dispatch.RESIDENT_FRESH_SEC):
        out("  常駐の受付（古い版）が、まだ動いている。その間、新しい受付は検索を引かない（2 通届くのを防ぐため）。"
            "`python triage.py install` で、古い受付が止まり、新しい形に替わる")

    poll = home.read_poll()
    if poll is None:
        out("Discord: まだ 1 回も読んでいない")
    elif poll.get("fail_since"):
        out("Discord: 読めていない（%s から。理由 %s）→ %s"
            % (_ago(deps, poll["fail_since"]), poll.get("last_error") or "?", poll_hint(str(poll.get("last_error") or ""))))
    else:
        out("Discord: 読めている（最後に読めたのは %s）" % _ago(deps, poll.get("last_ok")))

    until, reason = run_claude.blocked_state(home)
    if until > deps.now():
        out("Claude: 止めている（%s。あと %d 分は呼ばない。届いた分は溜めている）" % (reason or "?", (until - deps.now()) // 60 + 1))
    counts = {name: len(list(getattr(home, name).glob("pending-*.json"))) if getattr(home, name).is_dir() else 0
              for name in ("inbox", "claims", "failed", "processed")}
    out("件数: 未開封 %(inbox)d / 仕分け中 %(claims)d / 失敗で退避 %(failed)d / 処理済み %(processed)d" % counts)
    if counts["failed"]:
        out("  失敗で退避した件は、%s を見て、やり直すなら inbox へ戻す" % home.failed)

    log = cfg.state_root / "dispatch.log"
    if log.is_file():
        tail = log.read_text(encoding="ascii", errors="replace").splitlines()[-args.lines:]
        out("最近の記録（%s。新着が無い回は、残らない）:" % log.name)
        for row in tail:
            out("  " + row)
    return 0


# --- run / install / uninstall ----------------------------------------------------------------------
def cmd_run(args, deps: Deps) -> int:
    cfg, w, _ = load(deps, args)
    argv = ["--user", w.name, "--config", str(cfg.path)]
    if args.dry_run:
        argv.append("--dry-run")
    if args.backfill_hours:
        argv += ["--backfill-hours", str(args.backfill_hours)]
    return dispatch.main(argv)


def cmd_install(args, deps: Deps) -> int:
    cfg, w, _ = load(deps, args)
    ps = ["-File", str(REPO_ROOT / "scripts" / "install-task.ps1"), "-User", w.name,
          "-IntervalMinutes", str(args.interval_minutes), "-Config", str(cfg.path)]
    code = deps.powershell(ps)
    if code == 127:
        deps.out("自動起動の登録は、Windows のタスクスケジューラを使います。この OS では、cron などで "
                 "`python -X utf8 pipeline/dispatch.py --user %s` を数分おきに動かしてください。" % w.name)
    elif code == 0:
        deps.out("登録しました。`python triage.py status` で、動いているかを見られます。")
    else:
        deps.out("登録できませんでした（終了コード %d）。上に出たメッセージを見てください。" % code)
    return code


def cmd_uninstall(args, deps: Deps) -> int:
    return deps.powershell(["-File", str(REPO_ROOT / "scripts" / "install-task.ps1"), "-Uninstall"])


# --- 入口 -------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="triage.py", description="discord-mention-triage の設定・確認・実行・自動起動")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--user", help="config.json の watchers[].name（省略時は最初の 1 人）")
        p.add_argument("--config", help="config.json のパス（省略時は ~/.discord-mention-triage/config.json）")
        return p

    p = sub.add_parser("init", help="質問に答えて、設定と鍵の置き場を作る")
    p.add_argument("--force", action="store_true", help="すでに設定があっても作り直す（古い config.json は名前を変えて残す）")
    p.add_argument("--name")
    p.add_argument("--user-id")
    p.add_argument("--channel-id")
    p.add_argument("--read-dir", help="秘書が読んでよいフォルダ（無ければ空文字）")
    p.add_argument("--no-input", action="store_true",
                   help="何も聞かない。--name などで渡さなかった項目は、既定か空にする。鍵は、空の欄つきのファイルを作るので、あとでメモ帳で書く")
    p.set_defaults(func=cmd_init)

    p = common(sub.add_parser("check", help="設定・鍵・Discord・Webhook・Claude を確かめる"))
    p.add_argument("--no-claude", action="store_true", help="Claude を動かさない（利用枠を使わない）")
    p.add_argument("--post-test", action="store_true", help="専用チャンネルへ、確認用の投稿を 1 通送る")
    p.set_defaults(func=cmd_check)

    p = common(sub.add_parser("run", help="1 回動かす"))
    p.add_argument("--dry-run", action="store_true", help="検索は引くが、状態を進めず、Claude も起動しない")
    p.add_argument("--backfill-hours", type=float, default=0, help="過去 N 時間ぶんのメンションも拾う")
    p.set_defaults(func=cmd_run)

    p = common(sub.add_parser("install", help="自動起動を登録する"))
    p.add_argument("--interval-minutes", type=int, default=2)
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="自動起動を解除する")
    p.set_defaults(func=cmd_uninstall)

    p = common(sub.add_parser("status", help="いまの状態を見る"))
    p.add_argument("--lines", type=int, default=5, help="最近の記録を何行出すか")
    p.set_defaults(func=cmd_status)
    return ap


def main(argv=None, deps: Deps | None = None) -> int:
    args = build_parser().parse_args(argv)
    if deps is None:
        deps = Deps()
        if not sys.stdin.isatty():
            deps.ask_secret = deps.ask  # 画面に繋がっていないとき（パイプ）は、鍵も標準入力から読む。getpass は、キーボードを待って止まる
    try:
        return args.func(args, deps)
    except wconfig.ConfigError as e:
        deps.out("設定を読めません: %s" % e)
        deps.out("まだ作っていなければ `python triage.py init`、作ってあれば `python triage.py check`。")
        return 2
    except KeyboardInterrupt:
        deps.out("")
        deps.out("中断しました。")
        return 130


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty():  # 画面でなければ（ファイルやパイプ）、UTF-8 で出す。画面には、そのまま日本語が出る
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
