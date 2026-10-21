"""同じ流れの続きのメンションを、前の仕分けの会話を引き継いで仕分ける（ADR-0036、ADR-0038）。AI は使わない。通信もしない。

仕分けのセッションは、起動 1 回ぶん（dispatch.py が claim した分）ごとに作られる。そのままでは、同じスレッドの続きでも、
別の回に届けば、前の回の会話（本人がデスクトップ版で続けて話した分も）を知らない、別のセッションになる。

  * 仕分けに成功したら、場所（チャンネル。スレッドならスレッド）ごとに、セッション ID と名前と、仕分けたメッセージの ID を
    <名前>/sessions.json に控える（record）。本文は書かない（名前には、デスクトップ版の一覧と同じく、本文の頭が入る）。
  * 次の回で、claim した件がぜんぶ同じ場所にあり、控えたメッセージのどれかが、件の前の会話（context_before）に入っていれば、
    続きと見なす（pick）。前のセッションの記録が消えていれば、続けない。
  * 続けるときは、前のセッションを、いま動いている Claude Code が開いていなければ（held）、`claude -p --resume <前の ID>` で、
    そのセッションに書き足す。デスクトップ版の一覧は、流れごとに 1 件のまま増えない（ADR-0038）。
  * 開いていれば、`claude -p --resume <前の ID> --fork-session` で、前の会話を写した新しいセッションを作る。開いている間に
    書き足すと、デスクトップ版は書き足した分を読まず、本人が次に話すと記録が枝分かれする（2026-10-16 と 2026-10-21 に確かめた）。

セッション ID は、claude のコマンドライン引数になる（claude.cmd のときは、cmd.exe を通る）。決めた文字だけの ID しか使わない。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import time
from pathlib import Path

from watcher.store import read_json, write_json_atomic

FILE = "sessions.json"
KEEP_IDS = 50  # 場所ごとに控える、仕分けたメッセージの数。前の会話（既定 15 件）より十分に多く
KEEP_SEC = 30 * 24 * 3600  # Claude Code は、セッションの記録を 30 日で消す（cleanupPeriodDays の既定）。それより古い控えは捨てる
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")  # - で始まる値は、オプションに見える


def valid_id(session_id) -> bool:
    return bool(SESSION_ID_RE.fullmatch(str(session_id or "")))


def load(home) -> dict:
    data = read_json(Path(home.root) / FILE, {})
    return data if isinstance(data, dict) else {}


def places(items) -> set:
    return {str(i.get("channel_id") or "") for i in items if isinstance(i, dict)}


def config_dir(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def transcript_exists(session_id: str, env=None) -> bool:
    """claude がそのセッションの記録を持っているか（~/.claude/projects/<作業フォルダ>/<ID>.jsonl）。"""
    if not valid_id(session_id):
        return False
    return any((config_dir(env) / "projects").glob("*/%s.jsonl" % session_id))


def pid_alive(pid: int) -> bool:
    """そのプロセスが動いているか。Windows では os.kill を使わない（シグナル 0 が Ctrl+C の送信になる）。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: 権限の高いプロセスとして動いている
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def held(session_id: str, env=None, alive=pid_alive) -> bool:
    """そのセッションを、いま動いている Claude Code（デスクトップ版の裏の処理や、端末の claude）が開いているか。

    Claude Code は、動いている間、<設定フォルダ>/sessions/<プロセス ID>.json に、開いているセッションの ID（sessionId）を書く。
    デスクトップ版は、話しかけたセッションの裏の処理を、しばらく（30 分近く経っても）動かしたままにする（2026-10-21 に確かめた。2.1.302）。
    Claude Code の中の形なので、変わって読めなくなったら、開いていないと見なす（書き足して、枝分かれしうる。ADR-0038）。"""
    for path in (config_dir(env) / "sessions").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("sessionId") == session_id:
            try:
                if alive(int(data.get("pid") or 0)):
                    return True
            except (TypeError, ValueError):
                continue
    return False


def pick(table: dict, items, exists=transcript_exists) -> str:
    """続ける前のセッションの ID。続きでなければ ""。"""
    where = places(items)
    if len(where) != 1 or "" in where:
        return ""  # 場所がまたがる回は、新しいセッション
    entry = table.get(where.pop())
    if not isinstance(entry, dict):
        return ""
    session_id = str(entry.get("session_id") or "")
    known = {str(m) for m in entry.get("message_ids") or []}
    seen = {str(c.get("id")) for i in items for c in i.get("context_before") or [] if isinstance(c, dict) and c.get("id")}
    if not valid_id(session_id) or not known & seen or not exists(session_id):
        return ""
    return session_id


def name_of(table: dict, items) -> str:
    """続ける前のセッションの名前（書き足すとき、名前の 1 通に使う）。古い形の控えには無い（""）。"""
    entry = table.get(next(iter(places(items)), ""))
    return str(entry.get("name") or "") if isinstance(entry, dict) else ""


def record(home, items, session_id: str, continued_from: str = "", now: float | None = None, name: str = "") -> None:
    """仕分けに成功したセッションを、場所ごとに控える。続けた回は、前の控えのメッセージ ID も引き継ぐ。"""
    if not valid_id(session_id):
        return
    now = time.time() if now is None else now
    table = load(home)
    for place in sorted(places(items) - {""}):
        prev = table.get(place) if continued_from else None
        ids = list(prev.get("message_ids") or []) if isinstance(prev, dict) and prev.get("session_id") == continued_from else []
        ids += [str(i.get("message_id")) for i in items if str(i.get("channel_id") or "") == place and i.get("message_id")]
        table[place] = {"session_id": session_id, "name": name, "message_ids": list(dict.fromkeys(ids))[-KEEP_IDS:],
                        "at": int(now)}
    table = {k: v for k, v in table.items() if isinstance(v, dict) and now - float(v.get("at") or 0) < KEEP_SEC}
    write_json_atomic(Path(home.root) / FILE, table)


def forget(home, items) -> None:
    """続けた回が失敗したら、その場所の控えを消す。やり直しの回は、新しいセッションで仕分ける（続ける先が壊れていても止まらない）。"""
    table = load(home)
    gone = places(items) & set(table)
    if gone:
        write_json_atomic(Path(home.root) / FILE, {k: v for k, v in table.items() if k not in gone})
