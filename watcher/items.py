"""inbox に書く 1 件ぶん（item）の組み立て。形は、これまでの pending-*.json と互換。

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


def slim(s: Snap, user_names: dict, role_names: dict) -> dict:
    return {
        "author": s.author_name,
        "author_id": str(s.author_id),
        "time": fmt_time(s.created_at),
        "text": render_text(s.content, user_names, role_names),
        "attachments": list(s.attachments),
    }


def context(s: Snap, user_names: dict, role_names: dict) -> dict:
    """前後の会話の 1 件。宛先の発言（slim）に、メッセージ ID を足したもの。前の仕分けの続きかを見分けるのに使う（pipeline/continuation.py）。"""
    return dict(slim(s, user_names, role_names), id=str(s.id))


def build_item(
    *,
    watcher_name: str,
    kind: str,
    guild_id: int,
    guild_name: str,
    channel_id: int,
    channel_name: str,
    parent_name: str | None,
    parent_id: int | None = None,
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
        "thread_parent_id": str(parent_id) if parent_id else None,  # 場所ごとのフォルダ（channel_read_dirs）を、親の ID でも当てる
        "guild_id": str(guild_id),
        "guild_name": guild_name,
        "jump_url": jump_url(guild_id, channel_id, message.id),
        "direct": kind == "direct",
        "pull_requests": pull_requests,
        "pull_requests_blocked": pull_requests_blocked,
        "mention": slim(message, user_names, role_names),
        "context_before": [context(s, user_names, role_names) for s in before],
        "context_after": [context(s, user_names, role_names) for s in after],
    }
