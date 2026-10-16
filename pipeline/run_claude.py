#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仕分けを1回実行する。新着があるときだけ、dispatch.py が、その場で呼ぶ（別プロセスには切り離さない）。

流れ:
  1. claim 済みの pending-*.json をまとめて、`claude -p`（画面なしの Claude Code）に渡す
  2. Claude は「ドラフトの控え」と「投稿本文」の2ファイルを書くだけ（Discord にも Webhook にも触れない）
  3. このスクリプトが2ファイルを確かめ、Webhook で本人専用チャンネルへ投稿する
  4. 成功したら claim を processed/ へ。失敗したら claim を残して、数分後に tick が戻して再試行させる

Claude に与える権限は最小限:
  * 使える道具は Read / Grep / Glob / Edit / Write だけ（--tools）。Bash・ネットワーク・MCP・スキルは無し
  * 読めるのは claims/ と、設定した read_dirs と、届いた件の場所に当てた channel_read_dirs だけ（--add-dir）。.env は明示的に拒否
  * 書けるのは drafts/ と out/ の下だけ（Edit の許可ルール）
  * 許可が無い操作は聞かずに拒否（--permission-mode dontAsk --permission-prompts none）
  * ただし、デスクトップ版へ取り込む設定のときは、「自動」モード（auto）で動かす。取り込んだセッションは、記録に残った
    モードで開くので、続きを話すときに断られないように（ADR-0033）。このときは、上の範囲の外の読み書きを、断らずに、
    自動モードの判定に任せる。道具・許可ルール・.env の拒否は同じ
Webhook URL は Claude には渡さない。投稿はこのスクリプトが行う。

注意: --bare は付けない。付けるとサブスクのログインを使わず、API キーが必要になる。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import REPO_ROOT, add_target_args, p, resolve  # noqa: E402
from watcher.store import read_json, write_json_atomic  # noqa: E402
import claude_bin  # noqa: E402
import desktop_view  # noqa: E402
import notify  # noqa: E402
import session_naming  # noqa: E402
import tick  # noqa: E402
from watcher import config as wconfig  # noqa: E402

PROMPT_PATH = REPO_ROOT / "pipeline" / "prompts" / "triage.md"
JST = timezone(timedelta(hours=9))
RETRY_AFTER_SEC = 5 * 60  # 失敗した claim を戻すまでの待ち（tick の掃除が戻す）
FAIL_ALERT_COOLDOWN_SEC = 30 * 60
COUNTS_RE = re.compile(r"COUNTS\s+draft=(\d+)\s+stamp=(\d+)\s+skip=(\d+)\s+pr=(\d+)")
# 対応不要でも、本人に見てもらう印（triage.md の「不審な指示」）。本文にあれば、通知する
ATTENTION_MARKS = ("⚠",)

PR_NOTE = (
    "**PR について**: この実行では PR レビューは自動では走りません（無人の PR レビューは未有効）。"
    "`pull_requests` / `pull_requests_blocked` を持つ要素も、通常どおり仕分けてください。そのうえで、"
    "ダイジェストのその件に「PRレビュー依頼: <URL>」と1行加えます。"
    "PRレビュー依頼の件数は、ほかの区分と重ねて数えてかまいません。"
)


def rule_path(path) -> str:
    """許可ルール用の絶対パス。Windows の C:\\Users\\x は //c/Users/x、POSIX の /home/x は //home/x。"""
    s = str(Path(path).resolve()).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    if m:
        return "//%s/%s" % (m.group(1).lower(), m.group(2))
    return "/" + s if s.startswith("/") else s


def expand_dir(d) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(d))))


def channel_dirs(watcher, files) -> list:
    """届いた件に当たる、場所ごとのフォルダ（channel_read_dirs）。[(ID, (フォルダ, ...)), ...]。
    件のチャンネルと、スレッドの親の、どちらの ID でも当てる。"""
    table = dict(watcher.channel_read_dirs)
    hit = []
    for f in files:
        for item in tick.read_items(Path(f)):
            for key in ("channel_id", "thread_parent_id"):
                try:
                    cid = int(item.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if cid in table and cid not in hit:
                    hit.append(cid)
    return [(cid, tuple(expand_dir(d) for d in table[cid])) for cid in hit]


def build_command(claude: str, cfg, watcher, home, session_name: str = "", extra_dirs=()) -> list:
    """claude -p の引数。長い日本語の指示は引数に載せず、標準入力で渡す（Windows の引数は日本語が壊れる）。

    session_name は、記録を残すときだけ付ける。デスクトップ版の左側で、誰の、何の件かが分かるようにする（session_naming.py）。
    誰でも書ける文字列が入るので、決めた文字だけの名前でなければ、付けない（claude.cmd のときは、引数が cmd.exe を通る）。
    名前は、引数でしか渡せない。短い日本語は、この経路（Python → claude.cmd → claude.exe）では壊れなかった（2026-10-10 の実測）。

    モードは、デスクトップ版へ取り込む設定のときだけ "auto"（ADR-0033）。デスクトップ版は、取り込んだセッションを、記録に残った
    モードで開く。`claude --desktop --resume` には、ほかの指定を一緒に渡せない（2026-10-16 に確かめた）。
    extra_dirs は、この回の件の場所に当てたフォルダ（channel_dirs）。read_dirs と同じく、読むためだけに足す。"""
    cmd = [
        claude, "-p", "Follow the task written on stdin.",
        "--model", watcher.triage_model,
        "--effort", watcher.triage_effort,
        "--permission-mode", "auto" if cfg.desktop_import else "dontAsk",
        "--permission-prompts", "none",
        "--tools", "Read,Grep,Glob,Edit,Write",
        "--allowedTools",
        "Edit(%s/**)" % rule_path(home.drafts),
        "Edit(%s/**)" % rule_path(home.root / "out"),
        "--disallowedTools", "Read(//**/.env)", "Read(//**/*.env)",
        "--add-dir", str(home.claims),
    ]
    for d in dict.fromkeys(expand_dir(x) for x in (*watcher.read_dirs, *extra_dirs)):
        cmd += ["--add-dir", d]
    cmd += [
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--setting-sources", "project,local",
        "--output-format", "json",
    ]
    if session_name and session_naming.safe(session_name):
        cmd += ["--name", session_name]
    if not cfg.keep_transcripts:
        cmd.append("--no-session-persistence")
    return cmd


# claude に渡さない環境変数。-p では ANTHROPIC_API_KEY があると必ずそちらが使われ、サブスクではなく API 課金になる。
# クラウド事業者向けの切り替え変数も、意図せず別の経路に流れないよう外す。Discord のボットトークンも渡さない。
SCRUBBED_ENV = (
    "DISCORD_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)
SCRUBBED_PREFIXES = ("CLAUDE_CODE_", "ANTHROPIC_")
AUTH_RE = re.compile(r"authenticat|oauth|not logged in|please run .*login|invalid api key|401", re.I)
# 利用枠に達したときのエラーを見分ける。サブスクの枠は5時間／週の単位で戻るので、数えずに待つ。
LIMIT_RE = re.compile(r"usage limit|limit reached|rate.?limit|too many requests|quota|\b429\b|weekly limit|5-hour", re.I)

# 直るまで失敗を数えない種類。理由 → (新しい仕分けを始めない秒数, 通知の間隔秒, 通知文)
BLOCKS = {
    "claude_auth": (
        10 * 60, 60 * 60,
        "⚠ Claude にログインできていないため、メンションの仕分けを止めています（届いた分は溜めてあります）。"
        "`claude` でログインし直すか、`claude setup-token` の長期トークンを <ホーム>/.env に CLAUDE_CODE_OAUTH_TOKEN として書くと再開します。"
        "docs/setup.md の「Claude のログイン」を見てください。",
    ),
    "claude_limit": (
        30 * 60, 2 * 60 * 60,
        "⚠ Claude の利用枠に達したため、メンションの仕分けを待っています（届いた分は溜めてあります）。"
        "枠が戻れば自動で再開します。頻繁に出るときは、モデルや effort を軽くする（config の triage_model / triage_effort）か、"
        "間隔を空けてください。",
    ),
    # 上の2つの文言に当たらないが、モデルが一度も動かずにエラーで終わった（never_ran）。契約・組織の設定、API の障害、
    # モデル名の誤りなど。届いた件の中身は関係しないので、数えずに待つ。
    "claude_not_started": (
        10 * 60, 60 * 60,
        "⚠ Claude が仕分けを始める前にエラーで止まったため、メンションの仕分けを待っています（届いた分は溜めてあります）。"
        "モデルは動いていません。直れば自動で再開します。下のエラー文を手がかりに、Claude のログイン・契約・Claude Code の更新を"
        "確認してください。docs/setup.md の「止まったとき」を見てください。",
    ),
}
CLI_ERROR_MAX = 200
KEY_LIKE_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def never_ran(info: dict) -> bool:
    """Claude Code がエラーを返し、モデルが一度も動いていない（費用 0）。

    指示文に入るのはファイルのパスだけで、メンションの本文は、モデルが動き出してから道具で読む（build_prompt）。
    なので、費用 0 のエラーは、届いた件の中身のせいではない。失敗として数えると、直るまでに failed/ へ退避してしまう。"""
    cost = info.get("cost")
    return bool(info.get("is_error")) and isinstance(cost, (int, float)) and cost == 0


def clip_error(text: str) -> str:
    """Claude Code のエラー文を、実行記録と通知に載せられる形にする（1行・短く・鍵らしい並びは伏せる）。
    never_ran のときだけ使う。モデルが動いた回の結果には、メンションの本文が混ざりうるので載せない。"""
    s = KEY_LIKE_RE.sub("<hidden>", " ".join(str(text).split()))
    return s.replace("`", "'")[:CLI_ERROR_MAX]


# 専用の長期トークンが無いとき、claude は PC に保存された対話用のログインで動く。そのログインは PC に1つしか無いので、
# アカウントを切り替えると、無人実行も一緒に切り替わる（2026-10-04 の停止の原因）。
NO_TOKEN_HINT = (
    "いまは専用の長期トークンが無く、`claude` のログイン状態にそのまま左右されます。`claude setup-token` のトークンを "
    "<ホーム>/.env に CLAUDE_CODE_OAUTH_TOKEN として書くと、ログインを切り替えても止まらなくなります。"
)


def auth_source(env: dict) -> str:
    """claude がどの資格情報で動くか。"token" = 専用の長期トークン（`claude` のログインより優先される）/
    "cli_login" = PC に保存された `claude` のログイン。"""
    return "token" if env.get("CLAUDE_CODE_OAUTH_TOKEN") else "cli_login"


DESKTOP_IMPORT_TIMEOUT_SEC = 60


def desktop_import_command(claude: str, session_id: str) -> list:
    """記録したセッションを、デスクトップ版の左側の一覧に取り込む（`claude --desktop --resume <ID>`）。"""
    return [claude, "--desktop", "--resume", session_id]


def import_to_desktop(claude, session_id: str, home, env=None, timeout: int = DESKTOP_IMPORT_TIMEOUT_SEC) -> str:
    """取り込みを試みる。**失敗しても仕分けは成功のまま**（見やすさのための機能なので、本筋を止めない）。
    戻り値は実行記録に書く短い文字列: ok / claude_not_found / timeout / error / exit_<コード>。

    `--desktop` は、出力が画面（コンソール）に向いていないと動かない（リダイレクトされていると、すぐ終了コード 1 で終わる）。
    無人の実行は入出力が NUL やログファイルなので、Windows では **非表示の本物のコンソールを新しく作って**、その中で動かす
    （入出力は渡さない）。`conhost --headless` は、入出力が NUL やファイルだと中身を起動せずに成功（0）を返す、
    終了コードも返さない、ので使わない。"""
    if not claude:
        return "claude_not_found"
    cwd = home.root / "run"
    cwd.mkdir(parents=True, exist_ok=True)
    kw = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE: 黒い画面を出さない
        kw = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startup}
    try:
        proc = subprocess.run(desktop_import_command(claude, session_id), cwd=str(cwd), env=env, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return "error"
    return "ok" if proc.returncode == 0 else "exit_%s" % proc.returncode


def child_env(home) -> dict:
    """claude に渡す環境変数。Discord ボットのトークンは渡さない。
    無人実行用の長期トークン（`claude setup-token` で作る）が <ホーム>/.env にあれば、それだけを足す。"""
    # CLAUDE_CODE_* / ANTHROPIC_* は丸ごと外す。デスクトップアプリ配下の端末から手で動かしたときに引き継がれる
    # 変数（セッション ID・ホスト連携・ベース URL など）が、タスクスケジューラ経由の本番と挙動を変えないように。
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV and not k.startswith(SCRUBBED_PREFIXES)}
    envfile = home.root / ".env"
    tok = ""
    if envfile.exists():
        tok = notify.read_env_file(envfile).get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if not tok:
        tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def blocked_state(home) -> tuple:
    """(いつまで止めるか, 理由)。止めていなければ (0.0, "")。"""
    d = read_json(home.root / "runs" / "blocked.json", {})
    if not isinstance(d, dict):
        return 0.0, ""
    try:
        return float(d.get("until", 0)), str(d.get("reason", ""))
    except (TypeError, ValueError):
        return 0.0, ""


def blocked_until(home) -> float:
    return blocked_state(home)[0]


def has_pr(files) -> bool:
    for f in files:
        for item in tick.read_items(Path(f)):
            if item.get("pull_requests") or item.get("pull_requests_blocked"):
                return True
    return False


def dirs_note(located) -> str:
    """場所ごとのフォルダを、指示文に書く。届いた件の名前は書かない（スレッド名は誰でも付けられるので、指示文に混ぜない）。"""
    if not located:
        return ""
    lines = ["**場所ごとのフォルダ**: 次の ID のチャンネル（`channel_id` か `thread_parent_id` が一致する件）は、"
             "事実の裏取りに、まず右のフォルダを読んでください（読み取りのみ）。"]
    lines += ["- %s: %s" % (cid, "、".join(dirs)) for cid, dirs in located]
    return "\n".join(lines)


def build_prompt(watcher, files, drafts_file: Path, digest_file: Path, now: datetime) -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    subs = {
        "{{USER_NAME}}": watcher.name,
        "{{NOW}}": now.astimezone(JST).strftime("%Y/%m/%d %H:%M"),
        "{{ITEM_FILES}}": "\n".join("- " + p(f) for f in files),
        "{{DRAFTS_FILE}}": p(drafts_file),
        "{{DIGEST_FILE}}": p(digest_file),
        "{{PR_NOTE}}": PR_NOTE if has_pr(files) else "",
        "{{DIRS_NOTE}}": dirs_note(channel_dirs(watcher, files)),
    }
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def session_name_for(files) -> str:
    """届いた件から、セッションの名前を作る。名前は見やすさのためのものなので、作れなくても仕分けは止めない。"""
    try:
        return session_naming.build([item for f in files for item in tick.read_items(Path(f))])
    except Exception:  # noqa: BLE001
        return session_naming.FALLBACK


def run_id_for(files) -> str:
    names = "|".join(sorted(Path(f).name for f in files))
    return hashlib.sha1(names.encode("utf-8")).hexdigest()[:10]


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def no_window_flags() -> int:
    """子プロセス（claude、taskkill）を起動するときのフラグ。Windows では、窓のないコンソールを持たせる。

    付けないと、親がコンソールを持たないとき（別の経路から呼ばれたとき）に、Windows が、子のために見える窓を作る。
    入出力はパイプで受け渡すので、窓が無くても、動きは変わらない。"""
    return CREATE_NO_WINDOW if os.name == "nt" else 0


def kill_tree(proc) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30,
                           creationflags=no_window_flags())
        else:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


def run_process(cmd, prompt: str, cwd: Path, timeout: int, env: dict | None = None) -> tuple:
    """(終了コード, stdout, stderr, タイムアウトしたか)。"""
    cwd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(cwd), env=env,
        creationflags=no_window_flags(),
    )
    try:
        out, err = proc.communicate(input=prompt.encode("utf-8"), timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        return -1, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), True


def parse_result(stdout: str) -> dict:
    """claude -p --output-format json の結果から、本文を含まない指標だけ取り出す。"""
    info = {"is_error": None, "cost": None, "denials": 0, "counts": None, "text": "", "session_id": None, "api_error": ""}
    try:
        data = json.loads(stdout)
    except ValueError:
        return info
    if isinstance(data, list):  # 形式が配列のときは最後の result を見る
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
    if not isinstance(data, dict):
        return info
    info["is_error"] = data.get("is_error")
    info["session_id"] = data.get("session_id")
    info["cost"] = data.get("total_cost_usd")
    denials = data.get("permission_denials")
    info["denials"] = len(denials) if isinstance(denials, list) else 0
    info["text"] = str(data.get("result", ""))[:300]  # 失敗の分類に使う。記録に書くのは never_ran のときだけ
    # API が返したエラーの種類（例: "403 oauth_not_allowed_for_organization"）。決まった識別子で、本文は含まない
    info["api_error"] = " ".join(
        str(data[k]) for k in ("api_error_status", "api_error_code") if data.get(k) not in (None, ""))[:80]
    m = COUNTS_RE.search(str(data.get("result", "")))
    if m:
        info["counts"] = dict(zip(("draft", "stamp", "skip", "pr"), map(int, m.groups())))
    return info


def needs_mention(counts, body: str) -> bool:
    """配達に、本人へのメンション（通知）を付けるか。対応不要だけの配達には、付けない（手を止めなくてよい回で、通知を出さない）。

    迷ったら付ける。件数が分からないとき、返信ドラフト・スタンプ・PR が 1 件でもあるとき、本文に見てもらう印があるときは、付ける。
    """
    if not isinstance(counts, dict):
        return True
    try:
        todo = sum(int(counts.get(k) or 0) for k in ("draft", "stamp", "pr"))
        skipped = int(counts.get("skip") or 0)
    except (TypeError, ValueError):
        return True
    if todo > 0 or skipped <= 0:
        return True
    return any(mark in body for mark in ATTENTION_MARKS)


def age_claims(files, now: float) -> None:
    """失敗した claim の mtime を古くして、RETRY_AFTER_SEC 後に tick の掃除が inbox へ戻すようにする。"""
    t = now - (tick.TRIAGE_TIMEOUT_SEC - RETRY_AFTER_SEC)
    for f in files:
        try:
            os.utime(f, (t, t))
        except OSError:
            pass


def triage(home, watcher, cfg, files, *, command=None, now: datetime | None = None, poster=notify.post,
           import_fn=import_to_desktop, view=None) -> dict:
    """仕分けを1回実行する。例外は投げず、結果の辞書を返す（ok / reason / counts / ...）。"""
    now = now or datetime.now(JST)
    files = [Path(f) for f in files if Path(f).exists()]
    if not files:
        return {"ok": True, "reason": "no_files"}
    home.ensure()
    out_dir = home.root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id_for(files)
    digest_file = out_dir / f"{rid}.digest.txt"
    meta_file = out_dir / f"{rid}.meta.json"
    started = time.time()
    summary = {"ok": False, "reason": "", "run_id": rid, "files": len(files), "counts": None, "cost": None, "denials": 0}

    claude = None
    session_id = ""
    reuse = digest_file.exists() and digest_file.stat().st_size > 0 and meta_file.exists()
    if reuse:  # 前回は Claude まで成功し、投稿だけ失敗した。もう一度 Claude を動かさず投稿だけやり直す
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        drafts_file = Path(meta["drafts_file"])
        session_id = str(meta.get("session_id") or "")
        if session_id:
            summary["session_id"] = session_id
        name = str(meta.get("session_name") or "")  # 古い形の控えには無い（そのときは名前の 1 通を送らない）
        summary["counts"] = meta.get("counts")  # 通知するかどうかの判定に使う。古い形の控えには無い（そのときは通知する）
        summary["reason"] = "reused_digest"
    else:
        drafts_file = home.drafts / f"{now.astimezone(JST):%Y%m%d-%H%M%S}-{rid}.md"
        for f in (digest_file, drafts_file):
            f.unlink(missing_ok=True)
        name = session_name_for(files) if cfg.keep_transcripts else ""
        if command is None:
            claude = claude_bin.find_claude(cfg.claude_bin)
            if not claude:
                return finish_failure(home, files, summary, "claude_not_found", poster, started)
            extra = [d for _, dirs in channel_dirs(watcher, files) for d in dirs]
            command = build_command(claude, cfg, watcher, home, session_name=name, extra_dirs=extra)
        prompt = build_prompt(watcher, files, drafts_file, digest_file, now)
        env = child_env(home)
        summary["auth"] = auth_source(env)
        code, stdout, stderr, timed_out = run_process(
            command, prompt, home.root / "run", cfg.claude_timeout_sec, env=env
        )
        info = parse_result(stdout)
        summary.update(counts=info["counts"], cost=info["cost"], denials=info["denials"])
        if cfg.keep_transcripts and info["session_id"]:
            # 記録を残す設定のときだけ。`claude --resume <ID>` や `claude --desktop --resume <ID>` で中身を見られる
            session_id = str(info["session_id"])
            summary["session_id"] = session_id
        if timed_out:
            return finish_failure(home, files, summary, "timeout", poster, started)
        if code != 0 or info["is_error"]:
            blob = info["text"] + " " + stderr[:300]
            reason = "claude_auth" if AUTH_RE.search(blob) else "claude_limit" if LIMIT_RE.search(blob) else ""
            if never_ran(info):
                # 文言によらず待つ。文言は、通知文の出し分けにだけ使う
                summary.update(cli_error=clip_error(info["text"]), api_error=clip_error(info["api_error"]))
                return finish_blocked(home, files, summary, poster, started, reason or "claude_not_started")
            if reason:
                return finish_blocked(home, files, summary, poster, started, reason)
            return finish_failure(home, files, summary, "claude_exit_%s" % code, poster, started)
        if not digest_file.exists() or digest_file.stat().st_size == 0:
            return finish_failure(home, files, summary, "no_digest", poster, started)
        if not drafts_file.exists():
            return finish_failure(home, files, summary, "no_drafts", poster, started)
        meta_file.write_text(json.dumps({"drafts_file": str(drafts_file), "session_id": session_id, "session_name": name,
                                         "counts": info["counts"]}), encoding="utf-8")

    body = digest_file.read_text(encoding="utf-8")
    ok, line = notify.send(home, body, mention=needs_mention(summary["counts"], body), poster=poster)
    if not ok:
        summary["post_error"] = line
        return finish_failure(home, files, summary, "post_failed", poster, started, alert=False)

    # 成功: claim を処理済みへ。投稿本文の一時ファイルは消す（本文は drafts/ の控えに残る）
    home.processed.mkdir(parents=True, exist_ok=True)
    for f in files:
        try:
            f.rename(home.processed / f.name)
        except OSError:
            pass
    digest_file.unlink(missing_ok=True)
    meta_file.unlink(missing_ok=True)
    (home.root / "runs" / "blocked.json").unlink(missing_ok=True)
    for reason in BLOCKS:
        home.clear_alert(reason)
    summary.update(ok=True, reason=summary["reason"] or "done", post=line)
    if cfg.desktop_import and cfg.keep_transcripts and session_id and session_naming.safe(name):
        # 取り込むセッションの名前だけを、もう 1 通送る（通知は付けない）。長押しでそのまま写せるので、出先から「この件の続き」と
        # 頼むときに、どのセッションかを正確に言える。失敗しても仕分けは成功のまま。名前には本文の頭が入るので、記録には結果だけ書く
        sent, name_line = notify.send(home, name, mention=False, poster=poster)
        summary["name_post"] = "ok" if sent else name_line
    if cfg.desktop_import and cfg.keep_transcripts and session_id:
        # 投稿のあと、成功した回だけ。結果は記録に残すが、失敗しても仕分けの成否には影響させない。
        # 取り込むと、デスクトップ版の表示がそのセッションへ移るので、見ていたセッションへ戻す（ADR-0035）
        view = view or desktop_view.DesktopView()
        try:
            viewed = view.remember()
        except Exception:  # noqa: BLE001 - 見ていたセッションが分からなくても、取り込みはする（戻さないだけ）
            viewed = ""
        try:
            summary["desktop_import"] = import_fn(
                claude or claude_bin.find_claude(cfg.claude_bin), session_id, home, child_env(home))
        except Exception:  # noqa: BLE001
            summary["desktop_import"] = "error"
        if summary["desktop_import"] == "ok":
            try:
                summary["desktop_return"] = view.go_back(viewed, session_id)
            except Exception:  # noqa: BLE001
                summary["desktop_return"] = "error"
    write_summary(home, summary, started)
    return summary


def finish_failure(home, files, summary, reason, poster, started, alert=True) -> dict:
    summary.update(ok=False, reason=reason)
    age_claims(files, time.time())
    if alert and home.should_alert("run_failed", FAIL_ALERT_COOLDOWN_SEC):
        notify.send(
            home,
            "⚠ メンション仕分けの実行に失敗しました（理由: %s）。数分後に自動で再試行します。繰り返すときは docs/setup.md の「止まったとき」を見てください。" % reason,
            mention=True,
            poster=poster,
        )
    write_summary(home, summary, started)
    return summary


def finish_blocked(home, files, summary, poster, started, reason: str) -> dict:
    """認証切れ・利用枠切れ・モデルが動く前のエラー。仕分けは失敗扱いにせず、claim を inbox に戻して（試行回数も戻して）しばらく止める。
    直る・戻るまで同じ失敗を繰り返すと、3回で failed/ に退避されて取りこぼしてしまうので。"""
    block_sec, alert_cooldown, message = BLOCKS[reason]
    if reason == "claude_not_started" and summary.get("auth") == "cli_login":
        message += "\n" + NO_TOKEN_HINT
    if "cli_error" in summary:  # never_ran のときだけ入っている。何が起きたかを、通知でそのまま見せる
        detail = " / ".join(x for x in (summary["cli_error"], summary.get("api_error")) if x)
        message += "\nClaude Code のエラー: `%s`" % (detail or "(no message)")
    summary.update(ok=False, reason=reason)
    meta = tick.load_meta(home)
    home.inbox.mkdir(parents=True, exist_ok=True)
    for f in files:
        try:
            f.rename(home.inbox / f.name)
        except OSError:
            continue
        if f.name in meta:
            meta[f.name]["attempts"] = max(0, meta[f.name].get("attempts", 1) - 1)
    tick.save_meta(home, meta)
    write_json_atomic(home.root / "runs" / "blocked.json", {"reason": reason, "until": time.time() + block_sec})
    if home.should_alert(reason, alert_cooldown):
        notify.send(home, message, mention=True, poster=poster)
    write_summary(home, summary, started)
    return summary


def write_summary(home, summary: dict, started: float) -> None:
    """実行の記録。本文は含めない（理由・件数・費用の目安・拒否された操作の数だけ）。
    モデルが動く前のエラー（never_ran）に限り、Claude Code のエラー文の先頭も残す。"""
    try:
        runs = home.root / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        rec = dict(summary, seconds=int(time.time() - started), at=datetime.now(JST).isoformat())
        (runs / f"{datetime.now(JST):%Y%m%d-%H%M%S}-{summary.get('run_id', 'x')}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--files", required=True, help="claim 済みファイルの絶対パス（| 区切り）")
    args = ap.parse_args(argv)
    home, watcher = resolve(args)
    if watcher is None:
        print("ERROR=need_config (run_claude needs --user/--config, not --home)")
        return 2
    cfg = wconfig.load(args.config)
    res = triage(home, watcher, cfg, [f for f in args.files.split("|") if f])
    print("%s reason=%s counts=%s cost=%s denials=%s" % (
        "OK" if res["ok"] else "FAILED", res.get("reason"), res.get("counts"), res.get("cost"), res.get("denials")))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
