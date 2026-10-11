"""Discord の REST を、GET だけで読む。Discord と通信するのは、このモジュールの `ReadOnlyDiscord.get()` だけ。

読み取り専用: メソッドを選ぶ引数が無く、GET 以外（送信・リアクション・編集・削除など）を出す手段を持たない。
tests/test_readonly_guard.py が、コードの形（要求を組み立てる場所が 1 つ、`method="GET"`、本文なし）で担保している。
標準ライブラリだけで動く。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/umezy/discord-mention-triage, 0.2)"
REQUEST_TIMEOUT_SEC = 30
MAX_WAIT_SEC = 30  # 1 回の待ちの上限。これより長く待てと言われたら、この回は諦めて、次の回に任せる


class RestError(Exception):
    """読み取りに失敗した。メッセージは ASCII の短い理由だけ（トークンも本文も入れない）。"""

    def __init__(self, status: int, what: str = ""):
        self.status = status
        self.reason = ("http_%d" % status if status else "network") + ("_" + what if what else "")
        super().__init__(self.reason)


class ReadOnlyDiscord:
    def __init__(self, token: str, opener=None, sleep=time.sleep):
        self._token = token
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self.requests = 0
        self.waited_sec = 0.0

    def get(self, path: str, params=None, max_tries: int = 4) -> tuple:
        """(状態コード, JSON)。JSON でない応答は None。例外は投げない（状態コード 0 = 通信できなかった）。
        429（レート制限）と 202（検索の索引がまだ無い）は、指示された秒数だけ待って、やり直す。"""
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        status, data = 0, None
        for _ in range(max_tries):
            req = urllib.request.Request(
                url, method="GET", headers={"Authorization": "Bot " + self._token, "User-Agent": USER_AGENT})
            self.requests += 1
            try:
                with self._open(req, timeout=REQUEST_TIMEOUT_SEC) as res:
                    status, body, headers = res.status, res.read(), res.headers
            except urllib.error.HTTPError as e:
                status, body, headers = e.code, e.read(), e.headers
            except (urllib.error.URLError, OSError):
                return 0, None
            try:
                data = json.loads(body.decode("utf-8")) if body else None
            except ValueError:
                data = None
            if status not in (202, 429):
                self._pace(headers)
                return status, data
            wait = 1.0
            try:
                wait = float((data or {}).get("retry_after") or (headers.get("Retry-After") if headers else 0) or 1.0)
            except (AttributeError, TypeError, ValueError):
                pass
            if wait > MAX_WAIT_SEC:
                return status, data
            wait = max(wait, 0.5)
            self.waited_sec += wait
            self._sleep(wait)
        return status, data

    def _pace(self, headers) -> None:
        """この種類の要求の、残り回数が 0 になったら、戻るまで待つ（次の要求で 429 を出させない）。

        Discord は、種類ごとの上限を、応答のヘッダーで知らせる（実測: 検索は 6 回、前後の会話は 5 秒に 5 回）。"""
        try:
            if headers is None or str(headers.get("X-RateLimit-Remaining")) != "0":
                return
            wait = float(headers.get("X-RateLimit-Reset-After") or 0)
        except (AttributeError, TypeError, ValueError):
            return
        if 0 < wait <= MAX_WAIT_SEC:
            self.waited_sec += wait
            self._sleep(wait)

    def need(self, path: str, params=None, what: str = ""):
        """200 の JSON を返す。それ以外は RestError。"""
        status, data = self.get(path, params)
        if status != 200 or data is None:
            raise RestError(status, what)
        return data
