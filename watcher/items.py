"""inbox に書く 1 件ぶん（item）の組み立て。形は旧 poll.py の pending-*.json と互換。

通信しない。search.py が、REST で取ったメッセージを Snap に詰め替えてから渡す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
DISCORD_EPOCH_MS = 1420070400000
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


@dataclass(frozen=True)
class Snap:
    """メッセージ 1 件の、仕分けに要る分だけの写し。"""

    id: int
    author_id: int
    author_name: str
    created_at: datetime
    content: str
    attachments: tuple = ()


def snowflake_time(mid: int) -> datetime:
    return datetime.fromtimestamp(((mid >> 22) + DISCORD_EPOCH_MS) / 1000, tz=timezone.utc)


def time_to_snowflake(dt: datetime) -> int:
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%Y/%m/%d %H:%M")


def jump_url(guild_id, channel_id, message_id) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def render_text(text: str, user_names: dict, role_names: dict) -> str:
    """生の <@123> は下書き用のモデルに読めないので、名前に置き換える。"""
    text = USER_MENTION_RE.sub(lambda m: "@" + user_names.get(int(m.group(1)), m.group(1)), text or "")
    return ROLE_MENTION_RE.sub(lambda m: "@" + role_names.get(int(m.group(1)), m.group(1)), text)


def mention_names(text: str, user_names: dict, role_names: dict) -> list:
    """render_text が本文の中で置き換えた名前（出てきた順、重複なし）。表示名には空白が入りうるので、
    あとから「@名前」の切れ目を当てることはできない。セッションの名前を作るときに、これで取り除く（pipeline/session_naming.py）。"""
    names = [user_names.get(int(i), i) for i in USER_MENTION_RE.findall(text or "")]
    names += [role_names.get(int(i), i) for i in ROLE_MENTION_RE.findall(text or "")]
    return list(dict.fromkeys(names))


def slim(s: Snap, user_names: dict, role_names: dict) -> dict:
    return {
        "author": s.author_name,
        "author_id": str(s.author_id),
        "time": fmt_time(s.created_at),
        "text": render_text(s.content, user_names, role_names),
        "attachments": list(s.attachments),
    }


def build_item(
    *,
    watcher_name: str,
    kind: str,
    guild_id: int,
    guild_name: str,
    channel_id: int,
    channel_name: str,
    parent_name: str | None,
    message: Snap,
    before: list,
    after: list,
    user_names: dict,
    role_names: dict,
    pull_requests: list,
    pull_requests_blocked: list,
) -> dict:
    return {
        "watcher": watcher_name,
        "kind": kind,
        "message_id": str(message.id),
        "channel_id": str(channel_id),
        "channel_name": channel_name,
        "thread_parent": parent_name,
        "guild_id": str(guild_id),
        "guild_name": guild_name,
        "jump_url": jump_url(guild_id, channel_id, message.id),
        "direct": kind == "direct",
        "pull_requests": pull_requests,
        "pull_requests_blocked": pull_requests_blocked,
        "mention": slim(message, user_names, role_names),
        "mention_names": mention_names(message.content, user_names, role_names),
        "context_before": [slim(s, user_names, role_names) for s in before],
        "context_after": [slim(s, user_names, role_names) for s in after],
    }
