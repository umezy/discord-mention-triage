"""仕分けのセッションに付ける名前を、届いた件から作る。AI は使わない。通信もしない。

名前は、デスクトップ版の左側の一覧に出る（`desktop_import`）。開かなくても、誰の、何の件かが分かるようにする。

  <送り主>「<用件の頭>」#<場所> ほかN件
  例: alice「来週の予定を教えてもらえますか」#dev ほか2件

  * 送り主・用件・場所は、代表の 1 件から取る（本人への直接のメンションがあれば、それ。無ければ、最初の件）。
  * 用件は、宛先になった発言の頭。メンション（@名前）・URL・カスタム絵文字は除く。
  * 場所は、チャンネル名。スレッドなら、スレッドの題。

**名前は、claude のコマンドライン引数になる。** 送り主の名前も、スレッドの題も、本文も、サーバーの誰でも書ける文字列で、
claude が npm の `claude.cmd` のときは、引数が cmd.exe を通る。`"` と `&` があれば別のコマンドが動き、`%NAME%` は環境変数に
展開される（2026-10-10 に、この PC で確かめた）。そこで、**通す文字を決め打ちにする**（文字・数字・空白と、下の記号だけ）。
名前に入る文字列は、命令ではない（鉄則 2）。実行記録（`runs/`）には、名前を書かない（本文が入るため）。
"""

from __future__ import annotations

import re
import unicodedata

# 長さの上限。半角を 1、全角を 2 と数える（一覧は幅で切れるので、文字数ではなく幅でそろえる）。
WHO_MAX = 20
GIST_MAX = 40
PLACE_MAX = 30
FALLBACK = "メンション"

# 部品（送り主・用件・場所）に残す記号。これと、文字（L*）・結合文字（M*）・数字（N*）・空白のほかは、空白に置き換える。
# cmd.exe が特別に扱う文字（" % & | < > ^ ! など）と、\ は、どれも入らない。
KEEP_PUNCT = frozenset("_-.、。・（）！？…〜～")
# 組み立てるときに、この部品の外で足す文字。部品の中には残さないので、「」の中は必ず用件、# の後ろは必ず場所になる。
FRAME = frozenset("「」#")

URL_RE = re.compile(r"https?://\S+")
TAG_RE = re.compile(r"<[^<>\s]{1,100}>")  # カスタム絵文字 <:name:id>、チャンネル <#id>、時刻 <t:...> など
AT_RE = re.compile(r"@\S+")
ID_RE = re.compile(r"\d{15,}")  # 名前が取れなかったときの ID（数字の並び）は、見ても分からないので出さない


def _keep(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LMN" or ch in KEEP_PUNCT


def _width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in "WFA" else 1


def clean(text, limit: int) -> str:
    """通してよい文字だけにして、空白をまとめ、幅 limit に切る。先頭は、文字か数字で始める（- で始まると、オプションに見える）。"""
    s = "".join(ch if _keep(ch) else " " for ch in str(text or ""))
    s = " ".join(s.split()).strip(" -_.")
    if ID_RE.fullmatch(s):
        return ""
    if sum(map(_width, s)) <= limit:
        return s
    used, cut = 2, 0  # 末尾に付ける … のぶんを、先に取っておく
    for ch in s:
        used += _width(ch)
        if used > limit:
            break
        cut += 1
    return s[:cut].rstrip(" -_.") + "…"


def gist(item: dict) -> str:
    """宛先になった発言の頭。メンション・URL・カスタム絵文字を除く。"""
    mention = item.get("mention")
    text = str(mention.get("text") or "") if isinstance(mention, dict) else ""
    names = item.get("mention_names")
    # 表示名には空白が入りうる（"@taro / たろう"）。受付が残した名前の一覧で、長い順に、そのまま取り除く
    for name in sorted({str(n) for n in names if n} if isinstance(names, list) else (), key=len, reverse=True):
        text = text.replace("@" + name, " ")
    text = URL_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    text = AT_RE.sub(" ", text)  # 一覧が無い古い件と、@everyone / @here
    return clean(text, GIST_MAX)


def safe(name: str) -> bool:
    """コマンドライン引数に載せてよい名前か。build が作る名前は、必ず通る。"""
    return bool(name) and name[0] != "-" and all(ch == " " or ch in FRAME or _keep(ch) for ch in name)


def build(items) -> str:
    """届いた件（pending-*.json の items[] を、ファイルをまたいで並べたもの）から、名前を作る。例外は投げない。"""
    items = [i for i in items or [] if isinstance(i, dict)]
    if not items:
        return FALLBACK
    lead = next((i for i in items if i.get("kind") == "direct"), items[0])
    mention = lead.get("mention")
    who = clean(mention.get("author") if isinstance(mention, dict) else "", WHO_MAX)
    what = gist(lead)
    where = clean(lead.get("channel_name"), PLACE_MAX)
    name = who
    if what:
        name += "「%s」" % what
    if where:
        name += ("#%s" if what or not who else " #%s") % where
    if not name:
        return "%s %d件" % (FALLBACK, len(items))
    if len(items) > 1:
        name += " ほか%d件" % (len(items) - 1)
    return name
