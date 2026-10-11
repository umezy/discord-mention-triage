"""メッセージが「見張り対象の人宛て」かを判定する。純粋関数（通信しない）。"""

from __future__ import annotations

from dataclasses import dataclass

from .config import WatchedUser

# dispatch.py の SKIP= に出す理由。own / dm / none は件数に出さない（ノイズなので）。
COUNTED_SKIPS = ("blocked", "guild", "reacted", "replied", "deleted", "emit_failed")


@dataclass(frozen=True)
class MessageView:
    id: int
    channel_id: int
    guild_id: int | None
    author_id: int
    author_is_bot: bool
    content: str
    mention_user_ids: tuple = ()
    mention_role_ids: tuple = ()
    mention_everyone: bool = False
    parent_channel_id: int | None = None  # スレッドの親（ブロックリスト判定用）


@dataclass(frozen=True)
class Detection:
    kind: str | None  # "direct" | "role" | "everyone" | None
    skip: str | None = None  # 対象外にした理由。None なら対象外ではない（kind が None なら単に宛先ではない）


def classify(view: MessageView, w: WatchedUser, my_role_ids: frozenset = frozenset()) -> Detection:
    """直接メンション > ロールメンション > @everyone の順に判定する。

    返信のピン（Discord が返信先の著者を mentions に入れる）は直接メンションとして扱う。
    元の `GET /users/@me/mentions` と同じ範囲。
    """
    if view.guild_id is None:
        return Detection(None, "dm")
    if view.author_id == w.user_id:
        return Detection(None, "own")

    direct = w.user_id in view.mention_user_ids
    role = bool(my_role_ids) and any(r in my_role_ids for r in view.mention_role_ids)
    every = w.include_everyone and view.mention_everyone
    if not (direct or role or every):
        return Detection(None, None)

    if view.channel_id in w.channel_blocklist or (
        view.parent_channel_id is not None and view.parent_channel_id in w.channel_blocklist
    ):
        return Detection(None, "blocked")
    if w.guild_allowlist and view.guild_id not in w.guild_allowlist:
        return Detection(None, "guild")

    return Detection("direct" if direct else "role" if role else "everyone")
