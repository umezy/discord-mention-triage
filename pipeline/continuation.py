"""同じ流れの続きのメンションを、前の仕分けの会話を引き継いで仕分ける（ADR-0036、ADR-0038）。AI は使わない。通信もしない。

仕分けのセッションは、起動 1 回ぶん（dispatch.py が claim した分）ごとに作られる。そのままでは、同じスレッドの続きでも、
別の回に届けば、前の回の会話（本人がデスクトップ版で続けて話した分も）を知らない、別のセッションになる。

  * 仕分けに成功したら、場所（チャンネル。スレッドならスレッド）ごとに、セッション ID と名前と、仕分けたメッセージの ID を
    <名前>/sessions.json に控える（record）。本文は書かない（名前には、デスクトップ版の一覧と同じく、本文の頭が入る）。
  * 次の回で、claim した件がぜんぶ同じ場所にあり、控えたメッセージのどれかが、件の前の会話（context_before）に入っていれば、
    続きと見なす（pick）。前のセッションの記録が消えていれば、続けない。
  * 続けるときは、前のセッションを、いま動いている Claude Code が開いていなければ（held）、`claude -p --resume <前の ID>` で、
    そのセッションに書き足す。デスクトップ版の一覧は、流れごとに 1 件のまま増えない（ADR-0038）。
  * 開いている間に書き足すと、デスクトップ版は書き足した分を読まず、本人が次に話すと記録が枝分かれする（2026-10-16、10-21、
    10-22 に確かめた）。開いているのが、デスクトップ版の待機中の裏の処理だけなら、それを終わらせてから書き足す（release。ADR-0039）。
    デスクトップ版は、次に話すときに処理を起動し直し、書き足した分まで読み直す（2026-10-22 に確かめた）。
  * 終わらせられなければ、`claude -p --resume <前の ID> --fork-session` で、前の会話を写した新しいセッションを作る。
  * 続きと見なしても、前のセッションが 24 時間より前に使われたものか、前の件と別の話なら、続けない（same_topic.py。ADR-0044）。

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


def last_used(session_id: str, env=None) -> float | None:
    """そのセッションが最後に使われた時刻（記録の最終更新。仕分けで書き足した分も、本人がデスクトップ版で話した分も入る）。
    記録が無ければ None。"""
    if not valid_id(session_id):
        return None
    times = []
    for path in (config_dir(env) / "projects").glob("*/%s.jsonl" % session_id):
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(times, default=None)


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


WORD_RE = re.compile(r"[a-z_-]{1,20}")


def holders(session_id: str, env=None, alive=pid_alive) -> list:
    """そのセッションを開いている、動いている Claude Code の印（<設定フォルダ>/sessions/<プロセス ID>.json の中身）。

    Claude Code は、動いている間、この印に、開いているセッションの ID（sessionId）・プロセス ID（pid）・状態（status）・
    どこから動いているか（entrypoint）・作業フォルダ（cwd）・プロセスの開始時刻（procStart）を書く。
    デスクトップ版は、セッションを開いただけ・取り込んだだけでも裏の処理を始め、終了・自動更新での再起動まで、何時間も動かしたままにする
    （2026-10-21 のデスクトップ版のログで確かめた。2.1.304）。
    Claude Code の中の形なので、変わって読めなくなったら、開いていないと見なす（書き足して、枝分かれしうる。ADR-0038）。"""
    found = []
    for path in (config_dir(env) / "sessions").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            continue
        try:
            if alive(int(data.get("pid") or 0)):
                found.append(data)
        except (TypeError, ValueError):
            continue
    return found


def holder(session_id: str, env=None, alive=pid_alive, now: float | None = None) -> dict:
    """そのセッションを、いま動いている Claude Code（デスクトップ版の裏の処理や、端末の claude）が開いていれば、その様子。
    開いていなければ {}。様子は、実行記録（runs/）に書く: 状態（idle / busy など）・どこから動いているか・動き出してからの分・
    状態が変わってからの分。決まった短い語と数だけで、本文や名前は入れない。"""
    now = time.time() if now is None else now
    for data in holders(session_id, env, alive):
        by = {k: str(data.get(k)) for k in ("status", "entrypoint") if WORD_RE.fullmatch(str(data.get(k) or ""))}
        for key, field in (("minutes", "startedAt"), ("status_minutes", "statusUpdatedAt")):
            at = data.get(field)
            if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0:
                by[key] = max(0, int((now - at / 1000) // 60))  # ミリ秒
        return by or {"status": "unknown"}
    return {}


def held(session_id: str, env=None, alive=pid_alive) -> bool:
    """そのセッションを、いま動いている Claude Code が開いているか（holder）。"""
    return bool(holders(session_id, env, alive))


def created_at(pid: int) -> int | None:
    """そのプロセスの開始時刻（Windows の FILETIME。100 ナノ秒単位）。Claude Code の印の procStart と同じ値。読めなければ None。
    印が古いまま残り、同じプロセス ID が別のプロセスに使い回されたときに、そのプロセスを止めないために見る。"""
    if os.name != "nt" or pid <= 0:
        return None
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]  # 開始・終了・カーネル・ユーザー
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def end_tree(pid: int) -> bool:
    """そのプロセスと、その子（MCP サーバーなど）を終わらせる。Windows だけ。"""
    import subprocess
    try:
        proc = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def same_dir(a, b) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))
    except (TypeError, ValueError):
        return False


RELEASE_WAIT_SEC = 10


def release(session_id: str, run_dir, env=None, alive=pid_alive, created=created_at, end=end_tree,
            sleep=time.sleep, clock=time.monotonic, wait_sec: float = RELEASE_WAIT_SEC, windows: bool | None = None) -> str:
    """そのセッションを開いている、デスクトップ版の待機中の裏の処理を終わらせる（ADR-0039）。結果は実行記録に書く短い語:
    ok（開いている処理は、もう無い）/ not_desktop / busy / other_dir / changed / end_failed / still_held / unsupported。

    終わらせるのは、印が次をすべて満たす処理だけ。1 つでも満たさなければ、どれも終わらせない（呼んだ側は、写す）。
      * デスクトップ版の裏の処理（entrypoint が claude-desktop）。端末で本人が動かしている claude は止めない。
      * 待機中（status が idle）。返答の途中や、許可の確認を待っている処理は止めない。
      * 作業フォルダ（cwd）が、仕分けの作業フォルダ（run_dir。環境で動かしたセッションなら、その環境の作業フォルダ。ADR-0042）。
        ほかのフォルダのセッションには触らない。
      * 印のプロセスの開始時刻（procStart）が、いま動いているプロセスの開始時刻と同じ（プロセス ID の使い回しでない）。
    デスクトップ版は、外から終わった処理を、そのセッションの query error としてログに残し、次に話すときに起動し直す。"""
    if not (os.name == "nt" if windows is None else windows):
        return "unsupported"
    rows = holders(session_id, env, alive)
    for data in rows:
        if data.get("entrypoint") != "claude-desktop":
            return "not_desktop"
        if data.get("status") != "idle":
            return "busy"
        if not same_dir(data.get("cwd") or "", run_dir):
            return "other_dir"
        try:
            pid, start = int(data.get("pid") or 0), int(str(data.get("procStart") or ""))
        except (TypeError, ValueError):
            return "changed"
        if created(pid) != start:
            return "changed"
    for data in rows:
        if not end(int(data["pid"])):
            return "end_failed"
    deadline = clock() + wait_sec
    while holders(session_id, env, alive):
        if clock() >= deadline:
            return "still_held"
        sleep(0.2)
    return "ok"


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
    """続ける前のセッションの名前（書き足すとき、各件のセッションの行に使う）。古い形の控えには無い（""）。"""
    entry = table.get(next(iter(places(items)), ""))
    return str(entry.get("name") or "") if isinstance(entry, dict) else ""


def known_of(table: dict, items) -> list:
    """続ける前のセッションで、仕分けたメッセージの ID（同じ話かを聞くとき、前の会話から前の件を拾うのに使う）。"""
    entry = table.get(next(iter(places(items)), ""))
    return [str(m) for m in entry.get("message_ids") or []] if isinstance(entry, dict) else []


def environment_of(table: dict, items):
    """続ける前のセッションを動かした環境（environments.Environment.to_json の形）。run で動かしたとき・古い形の控えには無い（None）。"""
    entry = table.get(next(iter(places(items)), ""))
    return entry.get("environment") if isinstance(entry, dict) else None


def record(home, items, session_id: str, continued_from: str = "", now: float | None = None, name: str = "",
           environment: dict | None = None) -> None:
    """仕分けに成功したセッションを、場所ごとに控える。続けた回は、前の控えのメッセージ ID も引き継ぐ。
    environment は、そのセッションを動かした環境（ADR-0042）。続きの回も、同じ環境で動かすために控える。"""
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
        if environment:
            table[place]["environment"] = environment
    table = {k: v for k, v in table.items() if isinstance(v, dict) and now - float(v.get("at") or 0) < KEEP_SEC}
    write_json_atomic(Path(home.root) / FILE, table)


def forget(home, items) -> None:
    """続けた回が失敗したら、その場所の控えを消す。やり直しの回は、新しいセッションで仕分ける（続ける先が壊れていても止まらない）。"""
    table = load(home)
    gone = places(items) & set(table)
    if gone:
        write_json_atomic(Path(home.root) / FILE, {k: v for k, v in table.items() if k not in gone})
