"""仕分けのセッションに付ける名前。名前は、返答を書き終えた仕分けの Claude が、中身を見て付ける（ADR-0043）。
ここでは、その名前を、コマンドライン引数に載せてよい形にするだけ。通信もしない。

名前は、デスクトップ版の左側の一覧に出る（`desktop_import`）。開かなくても、何の件かが分かるようにする。
届いた件から機械的に作ると、送り主の表示名・発言の頭・チャンネル名が並んで長くなり、何の件かが読み取れなかった。形は決めない。

  * 起動するときは、まだ中身を知らないので、仮の名前（provisional）で起動する（`--name`）。
  * 仕分けの Claude は、最後の返答の終わりの `NAME <名前>` の行に、名前を書く（triage.md の「最後に」）。
  * 名前があれば、`claude -p --resume <ID> "/rename <名前>"` で付け直す（run_claude.rename_command）。

**名前は、claude のコマンドライン引数になる。** 名前を書くのは Claude だが、サーバーの誰でも書ける本文に引きずられうる。
claude が npm の `claude.cmd` のときは、引数が cmd.exe を通る。`"` と `&` があれば別のコマンドが動き、`%NAME%` は環境変数に
展開される（2026-10-10 に、この PC で確かめた）。そこで、**通す文字を決め打ちにする**（文字・数字・空白と、下の記号だけ）。
名前に入る文字列は、命令ではない（鉄則 2）。実行記録（`runs/`）には、名前を書かない（件の中身が入るため）。
"""

from __future__ import annotations

import unicodedata
from datetime import datetime

# 長さの上限。半角を 1、全角を 2 と数える（一覧は幅で切れるので、文字数ではなく幅でそろえる）。全角で 20 字
NAME_MAX = 40
CONTINUED = "（続き）"

# 名前に残す記号。これと、文字（L*）・結合文字（M*）・数字（N*）・空白のほかは、空白に置き換える。
# cmd.exe が特別に扱う文字（" % & | < > ^ ! など）と、\ は、どれも入らない。
KEEP_PUNCT = frozenset("_-.、。・（）「」！？…〜～")


def _keep(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LMN" or ch in KEEP_PUNCT


def _width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in "WFA" else 1


def clean(text, limit: int = NAME_MAX) -> str:
    """通してよい文字だけにして、空白をまとめ、幅 limit に切る。先頭は、文字か数字で始める（- で始まると、オプションに見える）。"""
    s = "".join(ch if _keep(ch) else " " for ch in str(text or ""))
    s = " ".join(s.split()).strip(" -_.")
    if sum(map(_width, s)) <= limit:
        return s
    used, cut = 2, 0  # 末尾に付ける … のぶんを、先に取っておく
    for ch in s:
        used += _width(ch)
        if used > limit:
            break
        cut += 1
    return s[:cut].rstrip(" -_.") + "…"


def safe(name: str) -> bool:
    """コマンドライン引数に載せてよい名前か。clean と provisional が作る名前は、必ず通る。"""
    return bool(name) and name[0] != "-" and all(ch == " " or _keep(ch) for ch in name)


def provisional(now: datetime, continued: bool = False) -> str:
    """起動するときの仮の名前（例: 「メンション 10月23日 09時30分」）。付け直せなかったときは、これが残る。
    / と : は通さない文字なので、日付と時刻は漢字で書く。"""
    return "メンション %d月%d日 %02d時%02d分" % (now.month, now.day, now.hour, now.minute) + (CONTINUED if continued else "")


def final(said, continued: bool = False) -> str:
    """Claude が付けた名前を、通してよい形にする。前の会話を引き継いだセッションには「（続き）」を付ける。名前が無ければ ""。"""
    name = clean(said)
    return name + CONTINUED if name and continued else name
