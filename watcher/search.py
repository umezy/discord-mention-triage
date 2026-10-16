"""受付。Discord の検索 API を引いて、自分宛てのメンションを inbox/pending-*.json に書き出す。常駐しない。

読み取り専用: Discord とは `rest.ReadOnlyDiscord`（GET だけ）を通してしか話さない。

1 回の流れ（dispatch.py が、数分おきに 1 回呼ぶ）:
  検索（前回の続きから、少し重ねて引く）──(宛先判定)──> 候補
  候補 ──(本体・スタンプ・前後の会話を取りに行く。本人がスタンプ済み・返信済みなら捨てる)──> inbox/pending-*.json

「前回の続き」は poll.json の since。検索の索引は、投稿より少し遅れ、順番も前後しうるので、毎回 search_overlap_minutes ぶん
重ねて引き、既出の ID（state.json）で二重を捨てる。PC がスリープしていた間の分は、起きた最初の回が、同じ検索で拾う
（最大 catchup_max_age_hours）。
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import detect, items, pr_policy
from .config import Config, WatchedUser
from .rest import ReadOnlyDiscord, RestError
from .store import Home

UTC = timezone.utc
MSG_TYPES = (0, 19)  # default / reply
SEARCH_PAGE = 25  # 検索 API の上限
MAX_SEARCH_PAGES = 40  # 1 回の検索でたどる上限（1000 件）。超えた分は、次の回が続きから拾う
MAX_EMIT_ATTEMPTS = 5
REACTION_USER_CAP = 500
DIRECTORY_TTL_SEC = 600  # サーバーの一覧と、本人のロールを覚えておく時間


@dataclass
class PollResult:
    ok: bool = True
    error: str = ""  # 読めなかった理由（ASCII。最初の 1 つ）
    found: int = 0  # 自分宛てと判定した、まだ出していない件
    emitted: int = 0  # inbox/ に書き出した件
    retrying: int = 0  # 読めなかったので、次の回でやり直す件
    skips: dict = field(default_factory=dict)  # 捨てた理由 → 件数（本文は持たない）
    first_run: bool = False

    def skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def fail(self, reason: str) -> None:
        self.ok = False
        self.error = self.error or reason


def display(user: dict, nick=None) -> str:
    return nick or user.get("global_name") or user.get("username") or str(user.get("id", "?"))


def emoji_path(emoji: dict) -> str:
    name = emoji.get("name") or ""
    return urllib.parse.quote("%s:%s" % (name, emoji["id"]) if emoji.get("id") else name, safe="")


def queries_for(w: WatchedUser, role_ids) -> list:
    """検索の条件。直接のメンション（返信の ping を含む）、自分が持つロール宛て、設定していれば @everyone。"""
    out = [{"mentions": str(w.user_id)}]
    if role_ids:
        out.append({"mentions_role_id": [str(r) for r in sorted(role_ids)[:100]]})
    if w.include_everyone:
        out.append({"mention_everyone": "true"})
    return out


def search_all(api: ReadOnlyDiscord, guild_id: int, params: dict, min_id: int) -> tuple:
    """min_id より新しい一致を、古い順にたどる。(ヒット {id: message}, スレッド {id: channel})。

    返る件数は上限より少ないことがあるので（公式の注意）、件数ではなく「新しいヒットが無くなった」で止める。"""
    hits, threads, cursor = {}, {}, int(min_id)
    for _ in range(MAX_SEARCH_PAGES):
        q = dict(params, min_id=str(cursor), sort_by="timestamp", sort_order="asc", limit=SEARCH_PAGE,
                 include_nsfw="true")
        data = api.need("/guilds/%d/messages/search" % guild_id, q, what="search")
        for th in data.get("threads") or []:
            threads[int(th["id"])] = th
        page = [m for group in data.get("messages") or [] for m in (group if isinstance(group, list) else [group])]
        fresh = [m for m in page if int(m["id"]) not in hits]
        if not fresh:
            break
        for m in fresh:
            hits[int(m["id"])] = m
        cursor = max(int(m["id"]) for m in page)
    return hits, threads


def view_of(m: dict, guild_id: int, threads: dict) -> detect.MessageView:
    cid = int(m["channel_id"])
    th = threads.get(cid) or {}
    author = m.get("author") or {}
    return detect.MessageView(
        id=int(m["id"]),
        channel_id=cid,
        parent_channel_id=int(th["parent_id"]) if th.get("parent_id") else None,
        guild_id=guild_id,
        author_id=int(author.get("id", 0)),
        author_is_bot=bool(author.get("bot")),
        content=m.get("content") or "",
        mention_user_ids=tuple(int(u["id"]) for u in m.get("mentions") or []),
        mention_role_ids=tuple(int(r) for r in m.get("mention_roles") or []),
        mention_everyone=bool(m.get("mention_everyone")),
    )


class Poller:
    def __init__(self, api: ReadOnlyDiscord, cfg: Config, w: WatchedUser, home: Home, now=time.time):
        self.api, self.cfg, self.w, self.home, self.now = api, cfg, w, home, now
        self._members: dict = {}
        self._channels: dict = {}
        self._roles: dict = {}

    # --- 1 回ぶん -------------------------------------------------------------------
    def run(self, backfill_hours: float = 0, dry_run: bool = False) -> PollResult:
        w, home, res = self.w, self.home, PollResult()
        started = self.now()
        state = home.read_poll()
        res.first_run = state is None
        state = state or {}
        since, overlap, origin = self._window(state, started, backfill_hours, res.first_run)
        floor = max(started - self.cfg.catchup_max_age_hours * 3600, origin)
        min_id = items.time_to_snowflake(datetime.fromtimestamp(max(since - overlap, floor), tz=UTC))

        candidates: dict = {}  # id → (サーバー, ヒット, 種類)
        try:
            guilds, roles_by_guild = self._directory(state, started)
        except RestError as e:
            res.fail(e.reason)
            guilds, roles_by_guild = [], {}
        for g in guilds:
            gid = int(g["id"])
            roles = frozenset(roles_by_guild.get(str(gid), []))
            for params in queries_for(w, roles):
                try:
                    hits, threads = search_all(self.api, gid, params, min_id)
                except RestError as e:
                    res.fail(e.reason)
                    continue
                for mid, m in hits.items():
                    if int(m.get("type", 0)) not in MSG_TYPES or mid in candidates:
                        continue
                    det = detect.classify(view_of(m, gid, threads), w, roles)
                    if det.skip in detect.COUNTED_SKIPS and items.snowflake_time(mid).timestamp() >= since:
                        res.skip(det.skip)  # 重ねて引く分は、何度も出てくる。初めて見えた回だけ数える
                    if det.kind is None or home.is_known(mid):
                        continue
                    candidates[mid] = (g, m, det.kind)

        res.found = len(candidates)
        retries = {k: v for k, v in (state.get("retries") or {}).items() if int(k) in candidates}
        unresolved = []
        throttled = False  # レート制限か通信断。この回は、残りの件を取りに行かない
        for mid in sorted(candidates):
            posted = items.snowflake_time(mid).timestamp()
            if dry_run:
                continue
            if throttled:
                res.retrying += 1
                unresolved.append(posted)
                continue
            g, m, kind = candidates[mid]
            try:
                outcome = self._emit(g, m, kind)
            except RestError as e:
                if e.status in (0, 429):
                    throttled = True  # この件のせいではない。試した回数に数えない
                    res.retrying += 1
                    unresolved.append(posted)
                    continue
                n = int(retries.get(str(mid), 0)) + 1
                if n >= MAX_EMIT_ATTEMPTS:
                    retries.pop(str(mid), None)
                    home.mark_seen(mid)
                    res.skip("emit_failed")
                else:
                    retries[str(mid)] = n
                    res.retrying += 1
                    unresolved.append(posted)
                continue
            retries.pop(str(mid), None)
            home.mark_seen(mid)
            if outcome == "emitted":
                res.emitted += 1
            else:
                res.skip(outcome)

        if not dry_run:
            state["origin"] = origin
            if res.ok:
                # 次の回は、この回の開始時刻から（重ねて）引く。まだ出していない件があれば、そこより先へは進めない
                state["since"] = min([started] + unresolved)
                state["last_ok"] = started
                state["fail_since"] = None
                state["last_error"] = ""
            else:
                state.setdefault("since", since)
                state["fail_since"] = state.get("fail_since") or started
                state["last_error"] = res.error
            state["retries"] = retries
            home.write_poll(state)
        return res

    def _window(self, state: dict, started: float, backfill_hours: float, first: bool) -> tuple:
        """(どこから引くか, 重ねる秒数, これより前は見ない時刻)。

        初回は「いま」から（過去ログを下書きの山にしない）。その時刻を origin として覚え、2 回目以降の重ね引きも、
        そこより前へは戻らない。--backfill-hours を付けた回だけ、origin を過去へ動かす。"""
        overlap = max(0, self.cfg.search_overlap_minutes) * 60
        if first:
            # 常駐版（Gateway）からの切り替え: 受付が最後に生きていた時刻（heartbeat.json）から引く
            legacy = self.home.read_legacy_heartbeat()
            since = legacy - overlap if legacy else started
            origin = since
        else:
            try:
                since = float(state.get("since") or started)
                origin = float(state.get("origin") or 0)
            except (TypeError, ValueError):
                since, origin = started, started
        if backfill_hours:
            since = min(since, started - backfill_hours * 3600)
            origin = min(origin, since)
        return min(since, started), overlap, min(origin, started)

    def _directory(self, state: dict, started: float) -> tuple:
        """(見張るサーバーの一覧, {サーバー ID: 本人のロール ID})。10 分は、前に取ったものを使う。"""
        d = state.get("directory") or {}
        if d.get("guilds") is not None and started - float(d.get("at", 0)) < DIRECTORY_TTL_SEC:
            return d["guilds"], d.get("roles", {})
        w = self.w
        guilds = [{"id": str(g["id"]), "name": g.get("name", "")}
                  for g in self.api.need("/users/@me/guilds", what="guilds")
                  if not w.guild_allowlist or int(g["id"]) in w.guild_allowlist]
        roles, members_of = {}, []
        for g in guilds:
            status, member = self.api.get("/guilds/%s/members/%d" % (g["id"], w.user_id))
            if status == 404:
                continue  # 本人がいないサーバー。メンションは来ない
            if status != 200 or not isinstance(member, dict):
                raise RestError(status, "member")
            roles[g["id"]] = [int(r) for r in member.get("roles") or []]
            members_of.append(g)
        state["directory"] = {"at": started, "guilds": members_of, "roles": roles}
        return members_of, roles

    # --- 1 件を inbox へ -------------------------------------------------------------
    def _emit(self, guild: dict, hit: dict, kind: str) -> str:
        """戻り値: "emitted" か、捨てた理由（reacted / replied / deleted）。読めなければ RestError。"""
        w, api = self.w, self.api
        gid, cid, mid = int(guild["id"]), int(hit["channel_id"]), int(hit["id"])
        base = "/channels/%d/messages" % cid
        status, msg = api.get("%s/%d" % (base, mid))
        if status == 404:
            return "deleted"
        if status != 200 or not isinstance(msg, dict):
            raise RestError(status, "message")

        # 本人がすでにスタンプを付けた／返信した分は、下書きもダイジェストも不要。
        if self._reacted_by_me(base, msg):
            return "reacted"
        after = self._history(base, {"after": mid, "limit": min(w.context_after, 100)}) if w.context_after > 0 else []
        if any(int(x["author"]["id"]) == w.user_id for x in after):
            return "replied"
        before = self._history(base, {"before": mid, "limit": min(w.context_before, 100)}) if w.context_before > 0 else []

        author = msg.get("author") or {}
        is_bot = bool(author.get("bot") or msg.get("webhook_id"))
        member = None if is_bot else self._member(gid, int(author["id"]))
        everything = [*before, msg, *after]
        user_names = {w.user_id: w.name}
        for x in everything:
            a = x.get("author") or {}
            if a.get("id"):
                user_names[int(a["id"])] = display(a)
            for u in x.get("mentions") or []:
                user_names[int(u["id"])] = display(u, (u.get("member") or {}).get("nick"))
        if member and author.get("id"):
            user_names[int(author["id"])] = display(author, member.get("nick"))
        role_names = self._role_names(gid) if any(x.get("mention_roles") for x in everything) else {}

        prs = pr_policy.extract(msg.get("content") or "", w.pr_policy)
        author_roles = frozenset(int(r) for r in (member or {}).get("roles") or [])
        eligible, blocked = pr_policy.split(prs, w.pr_policy, author_roles, is_bot)

        ch = self._channel(cid)
        parent = self._channel(int(ch["parent_id"])) if ch.get("parent_id") and int(ch.get("type", 0)) in (10, 11, 12) else None
        item = items.build_item(
            watcher_name=w.name,
            kind=kind,
            guild_id=gid,
            guild_name=guild.get("name") or str(gid),
            channel_id=cid,
            channel_name=ch.get("name") or str(cid),
            parent_name=parent.get("name") if parent else None,
            parent_id=int(ch["parent_id"]) if parent else None,
            message=self._snap(msg, user_names),
            before=[self._snap(x, user_names) for x in before],
            after=[self._snap(x, user_names) for x in after],
            user_names=user_names,
            role_names=role_names,
            pull_requests=eligible,
            pull_requests_blocked=blocked,
        )
        self.home.write_inbox([item], datetime.now(UTC))
        return "emitted"

    @staticmethod
    def _snap(m: dict, user_names: dict) -> items.Snap:
        a = m.get("author") or {}
        aid = int(a.get("id", 0))
        return items.Snap(
            id=int(m["id"]),
            author_id=aid,
            author_name=user_names.get(aid) or display(a),
            created_at=items.snowflake_time(int(m["id"])),
            content=m.get("content") or "",
            attachments=tuple(x.get("filename", "") for x in m.get("attachments") or []),
        )

    def _history(self, base: str, params: dict) -> list:
        data = self.api.need(base, params, what="history")
        return sorted((m for m in data if isinstance(m, dict)), key=lambda m: int(m["id"]))  # API は新しい順で返す

    def _reacted_by_me(self, base: str, msg: dict) -> bool:
        """リアクションした人の一覧から、本人を探す（検索の結果には reactions が入らないので、ここで取りに行く）。"""
        me = str(self.w.user_id)
        for r in msg.get("reactions") or []:
            path = "%s/%s/reactions/%s" % (base, msg["id"], emoji_path(r.get("emoji") or {}))
            after, seen = None, 0
            while seen < REACTION_USER_CAP:
                params = {"limit": 100}
                if after:
                    params["after"] = after
                users = self.api.need(path, params, what="reactions")
                if any(str(u.get("id")) == me for u in users):
                    return True
                seen += len(users)
                if len(users) < 100:
                    break
                after = users[-1]["id"]
        return False

    def _member(self, guild_id: int, user_id: int):
        """サーバー内の名前と、ロール。取れなければ None（名前はアカウント名に、ロールは「無し」に倒れる）。"""
        key = (guild_id, user_id)
        if key not in self._members:
            status, member = self.api.get("/guilds/%d/members/%d" % key)
            self._members[key] = member if status == 200 and isinstance(member, dict) else None
        return self._members[key]

    def _channel(self, channel_id: int) -> dict:
        if channel_id not in self._channels:
            self._channels[channel_id] = self.api.need("/channels/%d" % channel_id, what="channel")
        return self._channels[channel_id]

    def _role_names(self, guild_id: int) -> dict:
        if guild_id not in self._roles:
            status, roles = self.api.get("/guilds/%d/roles" % guild_id)
            self._roles[guild_id] = ({int(r["id"]): r.get("name", "") for r in roles}
                                     if status == 200 and isinstance(roles, list) else {})
        return self._roles[guild_id]


def poll_once(cfg: Config, w: WatchedUser, home: Home, token: str, *, backfill_hours: float = 0,
              dry_run: bool = False, opener=None, now=time.time, sleep=time.sleep) -> PollResult:
    api = ReadOnlyDiscord(token, opener=opener, sleep=sleep)
    return Poller(api, cfg, w, home, now=now).run(backfill_hours=backfill_hours, dry_run=dry_run)
