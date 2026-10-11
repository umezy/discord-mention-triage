"""本人専用チャンネルへの Webhook 投稿（分割・メンション・送信）。

post_digest.py（手動・診断用の CLI）と、dispatch.py / run_claude.py（自動）が共有する。
Webhook URL を読めるのは、このモジュールを使う Python だけ。Claude には渡さない。

設定は <ホーム>/.env から読む（コミットしない）:
    DIGEST_WEBHOOK=https://discord.com/api/webhooks/...        必須
    DIGEST_MENTION_USER_ID=1234567890                          任意。あればその人にメンションを付ける
（旧 discord-watch の DISCORD_WATCH_WEBHOOK / DISCORD_WATCH_MENTION_USER_ID も読む）
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

DISCORD_LIMIT = 2000
CHUNK_LIMIT = 1900  # メンション行の余地を残す
SPLIT_MARK = "%%SPLIT%%"
KEY_SETS = (
    ("DIGEST_WEBHOOK", "DIGEST_MENTION_USER_ID"),
    ("DISCORD_WATCH_WEBHOOK", "DISCORD_WATCH_MENTION_USER_ID"),
)


def read_env_file(path) -> dict:
    env = {}
    # utf-8-sig: Windows のメモ帳が付ける BOM を読み飛ばす（付いたままだと、先頭の 1 行のキーが読めない）
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def read_target(home) -> tuple:
    """(webhook_url | None, mention_user_id, env_path)。無ければ url は None（終了はしない）。"""
    env_path = home.root / ".env"
    if env_path.exists():
        env = read_env_file(env_path)
        for wk, mk in KEY_SETS:
            if env.get(wk):
                return env[wk], env.get(mk, "").strip(), env_path
    return None, "", env_path


def split_body(body: str) -> list:
    """`%%SPLIT%%` で区切り、それでも長い塊は行単位で割る。コードブロックの途中で切れないよう、
    ``` の内側で切るときは閉じて、次の塊の先頭で開き直す。"""
    chunks = []
    for block in (b.strip() for b in body.split(SPLIT_MARK)):
        if not block:
            continue
        cur, cur_len, fence_open = [], 0, False
        for line in block.split("\n"):
            add = len(line) + 1
            if cur and cur_len + add > CHUNK_LIMIT:
                text = "\n".join(cur)
                if fence_open:
                    text += "\n```"
                chunks.append(text)
                cur = ["```"] if fence_open else []
                cur_len = len(cur[0]) + 1 if cur else 0
            if line.startswith("```"):
                fence_open = not fence_open
            cur.append(line)
            cur_len += add
        if cur:
            text = "\n".join(cur)
            if fence_open:
                text += "\n```"
            chunks.append(text)
    return chunks


def post(url: str, content: str, mention: str) -> int:
    allowed = {"parse": [], "users": [mention]} if mention else {"parse": []}
    data = json.dumps({"content": content, "allowed_mentions": allowed}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", "User-Agent": "mention-triage/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.status


def send(home, body: str, mention: bool = True, poster=post, sleep=time.sleep) -> tuple:
    """分割して連投する。(成功したか, ASCII の結果1行)。例外は投げない。"""
    url, mention_id, env_path = read_target(home)
    if not url:
        return False, "ERROR=no_webhook path=%s" % env_path
    mention_id = mention_id if mention else ""
    chunks = split_body(body)
    if not chunks:
        return False, "ERROR=empty_body"
    if mention_id:
        chunks[0] = "<@%s>\n%s" % (mention_id, chunks[0])
    if any(len(c) > DISCORD_LIMIT for c in chunks):
        return False, "ERROR=chunk_too_long"
    for i, c in enumerate(chunks):
        try:
            status = poster(url, c, mention_id if i == 0 else "")
        except urllib.error.HTTPError as e:
            return False, "ERROR=http chunk=%d status=%d" % (i + 1, e.code)
        except Exception as e:  # noqa: BLE001
            return False, "ERROR=network chunk=%d detail=%s" % (i + 1, type(e).__name__)
        if status >= 300:
            return False, "ERROR=status chunk=%d status=%d" % (i + 1, status)
        if i + 1 < len(chunks):
            sleep(1)  # 連投でレート制限に当たらないように
    return True, "OK chunks=%d mention=%s" % (len(chunks), mention_id or "none")
