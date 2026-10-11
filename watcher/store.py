"""ディスク上の状態。受付（watcher/search.py）と、pipeline の両方が使う。標準ライブラリのみ。

置き場（<state_root>/<名前>/）:
  inbox/      受付が書き出した未処理分 pending-*.json
  claims/     処理中（inbox から mv して先取り）
  processed/  処理済み
  drafts/     出来上がった返信ドラフト（本文を含むのでリポジトリには置かない）
  failed/     何度やっても落ちた分
  state.json  重複防止用の既出メッセージ ID
  poll.json   受付の続きの位置（どこから検索を引くか）と、読めているかどうか（本文は書かない）

常駐版（Gateway）が使っていた holding/・heartbeat.json・skips.log・cursors.json は、もう使わない（残っていても害は無い）。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

SEEN_LIMIT = 2000


def read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Home:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.inbox = self.root / "inbox"
        self.claims = self.root / "claims"
        self.processed = self.root / "processed"
        self.drafts = self.root / "drafts"
        self.failed = self.root / "failed"
        self.state_path = self.root / "state.json"
        self.poll_path = self.root / "poll.json"
        self.meta_path = self.root / "claims_meta.json"
        self.alerts_path = self.root / "alerts.json"

    def ensure(self) -> "Home":
        for d in (self.inbox, self.claims, self.processed, self.drafts, self.failed):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # --- 既出の管理（二重に拾わない） -------------------------------------------
    def is_known(self, message_id: int) -> bool:
        return str(message_id) in set(read_json(self.state_path, {}).get("seen", []))

    def mark_seen(self, message_id: int) -> None:
        st = read_json(self.state_path, {})
        seen = [s for s in st.get("seen", []) if s != str(message_id)]
        seen.append(str(message_id))
        st["seen"] = seen[-SEEN_LIMIT:]
        write_json_atomic(self.state_path, st)

    # --- inbox --------------------------------------------------------------------
    def write_inbox(self, items: list, now: datetime) -> Path:
        """1 ファイル 1 件。PR レビュー（最大 1 時間）が他の返信ドラフトを待たせないため。"""
        self.inbox.mkdir(parents=True, exist_ok=True)
        tail = str(items[0]["message_id"])[-6:]
        path = self.inbox / f"pending-{now:%Y%m%d-%H%M%S}-{tail}.json"
        write_json_atomic(path, {"generated_at": now.isoformat(), "items": items})
        return path

    # --- 受付の続きの位置 -----------------------------------------------------------
    def read_poll(self):
        """poll.json。まだ 1 回も引いていなければ None（＝初回）。"""
        d = read_json(self.poll_path, None)
        return d if isinstance(d, dict) else None

    def write_poll(self, data: dict) -> None:
        write_json_atomic(self.poll_path, data)

    def read_legacy_heartbeat(self):
        """常駐版（Gateway）の受付が、最後に生きていた時刻。切り替えた最初の回が、そこから引くために読む。"""
        hb = read_json(self.root / "heartbeat.json", None)
        try:
            return float(hb["ts"]) if isinstance(hb, dict) else None
        except (KeyError, TypeError, ValueError):
            return None

    def resident_watcher_alive(self, now: float, fresh_sec: int) -> bool:
        """常駐版（Gateway）の受付が、いまも動いているか。30 秒おきに書く heartbeat.json が、新しければ動いている。"""
        ts = self.read_legacy_heartbeat()
        return ts is not None and 0 <= now - ts < fresh_sec

    # --- 同じ警告を連打しないための記録 ------------------------------------------
    def should_alert(self, key: str, cooldown_sec: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        alerts = read_json(self.alerts_path, {})
        last = alerts.get(key, 0)
        if now - last < cooldown_sec:
            return False
        alerts[key] = now
        write_json_atomic(self.alerts_path, alerts)
        return True

    def clear_alert(self, key: str) -> None:
        alerts = read_json(self.alerts_path, {})
        if key in alerts:
            alerts.pop(key)
            write_json_atomic(self.alerts_path, alerts)
