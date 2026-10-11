"""テスト用の偽 Discord。urllib の opener として振る舞い、受付が使う GET だけに答える。

実際の Discord には繋がない。受けた要求をすべて覚えておくので、「GET しか出していない」ことを、テストが確かめられる。
GET 以外が来たら、その場でテストを落とす。
"""

import io
import json
import urllib.error
import urllib.parse
from datetime import datetime, timezone

from watcher import items

ME, ALICE, BOB, BOT = 111, 222, 333, 444
GUILD = 1
STAFF_ROLE, MY_ROLE, OTHER_ROLE = 500, 7, 8
DEV, DIGEST, FORUM, FORUM_POST = 10, 999, 15, 20
NOW = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc).timestamp()  # JST 12:00


def sf(seconds_ago: float, now: float = NOW) -> int:
    """「いまから seconds_ago 秒前」に投稿されたメッセージの ID。"""
    return items.time_to_snowflake(datetime.fromtimestamp(now - seconds_ago, tz=timezone.utc))


class FakeResponse:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDiscord:
    def __init__(self):
        self.requests = []
        self.paths = []
        self.guilds = [{"id": str(GUILD), "name": "Example"}]
        self.users = {
            ME: {"id": str(ME), "username": "taro", "global_name": "たろう"},
            ALICE: {"id": str(ALICE), "username": "alice", "global_name": "Alice"},
            BOB: {"id": str(BOB), "username": "bob", "global_name": None},
            BOT: {"id": str(BOT), "username": "ci-bot", "bot": True},
        }
        self.members = {
            ME: {"nick": None, "roles": [str(MY_ROLE)]},
            ALICE: {"nick": "アリス（開発）", "roles": [str(STAFF_ROLE)]},
            BOB: {"nick": None, "roles": []},
        }
        self.roles = [{"id": str(MY_ROLE), "name": "Nebula"}, {"id": str(STAFF_ROLE), "name": "社員"},
                      {"id": str(OTHER_ROLE), "name": "Other"}]
        self.channels = {
            DEV: {"id": str(DEV), "type": 0, "name": "dev"},
            DIGEST: {"id": str(DIGEST), "type": 0, "name": "digest"},
            FORUM: {"id": str(FORUM), "type": 15, "name": "相談フォーラム"},
            FORUM_POST: {"id": str(FORUM_POST), "type": 11, "name": "困りごと", "parent_id": str(FORUM)},
        }
        self.messages = {}
        self.reactions = {}  # message id → {emoji: [user id]}
        self.unindexed = set()  # 投稿されたが、まだ検索に出ない
        self.ghosts = set()  # 検索には出るが、取りに行くと 404（消された）
        self.fail = {}  # 種類（guilds / member / search / message / history / reactions / channel / roles）→ 状態コード
        self.queue = []  # 次の検索に、先に返す (状態コード, 本文)
        self.headers = {}  # 種類 → 応答に付けるヘッダー（レート制限の残り回数など）

    # --- 中身を足す -------------------------------------------------------------------
    def add(self, mid, content="", author=ALICE, channel=DEV, mentions=(), roles=(), reply_to=None, mtype=None,
            everyone=False, attachments=(), webhook=False):
        m = {
            "id": str(mid), "channel_id": str(channel), "content": content,
            "type": mtype if mtype is not None else (19 if reply_to else 0),
            "author": dict(self.users.get(author, {"id": str(author), "username": "user%d" % author})),
            "mentions": [dict(self.users[u]) for u in mentions], "mention_roles": [str(r) for r in roles],
            "mention_everyone": everyone, "attachments": [{"filename": a} for a in attachments],
            "timestamp": "2026-10-09T03:00:00+00:00", "edited_timestamp": None,
        }
        if webhook:
            m["webhook_id"] = "777"
        self.messages[int(mid)] = m
        return int(mid)

    def mention(self, seconds_ago, text="お願いします", **kw):
        """ME 宛ての直接メンション。"""
        return self.add(sf(seconds_ago), "<@%d> %s" % (ME, text), mentions=[ME], **kw)

    # --- urllib の opener ---------------------------------------------------------------
    def __call__(self, req, timeout=None):
        if req.get_method() != "GET" or req.data is not None:
            raise AssertionError("GET 以外の要求が出た: %s %s" % (req.get_method(), req.full_url))
        self.requests.append(req)
        url = urllib.parse.urlsplit(req.full_url)
        path = url.path.replace("/api/v10", "", 1)
        self.paths.append(path)
        status, payload = self.route(path, urllib.parse.parse_qs(url.query))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(json.dumps(payload).encode()))
        return FakeResponse(status, payload, self.headers.get(self.kind_of(path.strip("/").split("/")), {}))

    def count(self, fragment: str) -> int:
        return sum(1 for p in self.paths if fragment in p)

    def route(self, path, q):
        parts = path.strip("/").split("/")
        kind = self.kind_of(parts)
        if kind in self.fail:
            return self.fail[kind], {"message": "forced"}
        if kind == "guilds":
            return 200, self.guilds
        if kind == "member":
            m = self.members.get(int(parts[3]))
            return (200, dict(m, user=self.users[int(parts[3])])) if m else (404, {"message": "Unknown Member"})
        if kind == "roles":
            return 200, self.roles
        if kind == "search":
            if self.queue:
                return self.queue.pop(0)
            return 200, self.search(q)
        if kind == "channel":
            ch = self.channels.get(int(parts[1]))
            return (200, ch) if ch else (403, {"message": "Missing Access"})
        if kind == "history":
            return 200, self.history(int(parts[1]), q)
        if kind == "message":
            mid = int(parts[3])
            if mid not in self.messages or mid in self.ghosts:
                return 404, {"message": "Unknown Message"}
            msg = dict(self.messages[mid])
            msg["reactions"] = [{"count": len(u), "me": False, "emoji": {"id": None, "name": e}}
                                for e, u in self.reactions.get(mid, {}).items()]
            return 200, msg
        if kind == "reactions":
            users = sorted(self.reactions.get(int(parts[3]), {}).get(urllib.parse.unquote(parts[5]), []))
            after = int(q.get("after", ["0"])[0])
            limit = int(q.get("limit", ["25"])[0])
            return 200, [{"id": str(u)} for u in users if u > after][:limit]
        return 404, {"message": "not routed: " + path}

    @staticmethod
    def kind_of(parts) -> str:
        if parts == ["users", "@me", "guilds"]:
            return "guilds"
        if parts[0] == "guilds" and len(parts) == 4 and parts[2] == "members":
            return "member"
        if parts[0] == "guilds" and parts[2:] == ["roles"]:
            return "roles"
        if parts[0] == "guilds" and parts[2:] == ["messages", "search"]:
            return "search"
        if parts[0] == "channels" and len(parts) == 2:
            return "channel"
        if parts[0] == "channels" and len(parts) == 3:
            return "history"
        if parts[0] == "channels" and len(parts) == 4:
            return "message"
        if parts[0] == "channels" and len(parts) == 6 and parts[4] == "reactions":
            return "reactions"
        return "?"

    def search(self, q):
        min_id = int(q.get("min_id", ["0"])[0])
        limit = int(q.get("limit", ["25"])[0])
        out = []
        for mid, m in sorted(self.messages.items()):
            if mid <= min_id or mid in self.unindexed:
                continue
            if "mentions" in q and not ({u["id"] for u in m["mentions"]} & set(q["mentions"])):
                continue
            if "mentions_role_id" in q and not (set(m["mention_roles"]) & set(q["mentions_role_id"])):
                continue
            if "mention_everyone" in q and not m["mention_everyone"]:
                continue
            out.append(m)
        if q.get("sort_order", ["desc"])[0] != "asc":
            out.reverse()
        page = out[:limit]
        threads = {m["channel_id"]: self.channels[int(m["channel_id"])] for m in page
                   if self.channels.get(int(m["channel_id"]), {}).get("type") in (10, 11, 12)}
        # 検索の結果に reactions は入らない（公式ドキュメント）。メッセージは 1 件ずつ、配列に包まれて返る
        return {"total_results": len(out), "messages": [[dict(m)] for m in page], "threads": list(threads.values()),
                "doing_deep_historical_index": False}

    def history(self, channel_id, q):
        """本物と同じく、指定した ID に近い順に limit 件を選び、新しい順に並べて返す。"""
        limit = int(q.get("limit", ["50"])[0])
        ms = sorted((m for m in self.messages.values() if int(m["channel_id"]) == channel_id), key=lambda m: int(m["id"]))
        if "after" in q:
            ms = [m for m in ms if int(m["id"]) > int(q["after"][0])][:limit]
        elif "before" in q:
            ms = [m for m in ms if int(m["id"]) < int(q["before"][0])][-limit:]
        return [dict(m) for m in reversed(ms)]
