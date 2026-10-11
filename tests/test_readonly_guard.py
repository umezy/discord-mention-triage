"""「Discord へ書き込まない」を、コードの形で担保するテスト。

watcher/ 配下は、Discord の読み取りしかしてはいけない。AST で次を検査する:
  1. Discord への要求を組み立てる場所は、watcher/rest.py の 1 か所だけ。そこは `method="GET"` と決め打ちで、本文（data）を渡さない
  2. 通信の入口（urlopen など）に触るのも、その 1 か所だけ。ほかのモジュールは、HTTP を自分で話さない
  3. HTTP クライアント・ソケット・Gateway（常駐の接続）・子プロセスを、import していない
  4. GET 以外のメソッド名や、Webhook の URL を、文字列で持っていない

失敗したら「書き込めるコードが入った」ということ。テストを緩めて通すのではなく、コードを直すこと。
(本人専用チャンネルへ Webhook で投稿するのは pipeline/notify.py。受付とは別のモジュールで、ボットのトークンを使わない。)

実際に出る要求がすべて GET であることは、tests/test_search_flow.py が、偽の Discord で確かめている。
"""

import ast
import unittest
from pathlib import Path

WATCHER_DIR = Path(__file__).resolve().parent.parent / "watcher"
REST = "rest.py"

FORBIDDEN_IMPORTS = {"requests", "urllib3", "http", "httpx", "aiohttp", "socket", "websockets", "subprocess",
                     "discord", "asyncio", "ssl", "ftplib", "smtplib"}
NETWORK_ENTRY_POINTS = {"urlopen", "urlretrieve", "build_opener", "install_opener", "OpenerDirector"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
FORBIDDEN_FRAGMENTS = ("/webhooks/", "wss://", "ws://", "/typing")


def _sources():
    files = sorted(WATCHER_DIR.glob("*.py"))
    assert files, "watcher/*.py が見つからない"
    for f in files:
        yield f, ast.parse(f.read_text(encoding="utf-8"), filename=str(f))


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.lineno, node.module or ""


def _request_calls(tree):
    """`...Request(...)` の呼び出し（urllib.request.Request を、別名で使われても拾えるよう、名前だけで見る）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name == "Request":
                yield node


def _violations(tree, filename: str) -> list:
    """1 ファイルぶんの違反（行番号つき）。rest.py だけ、urllib.request / urllib.error を使ってよい。"""
    bad = []
    for lineno, name in _imports(tree):
        top = name.split(".")[0]
        if top in FORBIDDEN_IMPORTS:
            bad.append("%s:%d import %s" % (filename, lineno, name))
        if top == "urllib" and name != "urllib.parse" and filename != REST:
            bad.append("%s:%d import %s（通信してよいのは rest.py だけ）" % (filename, lineno, name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in NETWORK_ENTRY_POINTS and filename != REST:
            bad.append("%s:%d .%s" % (filename, node.lineno, node.attr))
        if isinstance(node, ast.Name) and node.id in NETWORK_ENTRY_POINTS:
            bad.append("%s:%d %s" % (filename, node.lineno, node.id))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.strip().upper() in WRITE_METHODS or any(s in node.value for s in FORBIDDEN_FRAGMENTS):
                bad.append("%s:%d %r" % (filename, node.lineno, node.value[:40]))
    for call in _request_calls(tree):
        kw = {k.arg: k.value for k in call.keywords}
        method = kw.get("method")
        if filename != REST:
            bad.append("%s:%d Request(...)（要求を組み立ててよいのは rest.py だけ）" % (filename, call.lineno))
        elif not (isinstance(method, ast.Constant) and method.value == "GET"):
            bad.append("%s:%d Request の method が、決め打ちの \"GET\" でない" % (filename, call.lineno))
        elif "data" in kw or len(call.args) != 1 or None in kw:
            bad.append("%s:%d Request に本文（data）か、余分な引数を渡している" % (filename, call.lineno))
    return bad


class ReadOnlyGuard(unittest.TestCase):
    def test_watcher_only_reads(self):
        bad = []
        for f, tree in _sources():
            bad += _violations(tree, f.name)
        self.assertEqual(bad, [], "書き込める／勝手に通信できるコードがある: " + ", ".join(bad))

    def test_there_is_exactly_one_place_that_talks_to_discord(self):
        built, opened = [], []
        for f, tree in _sources():
            built += [(f.name, c.lineno) for c in _request_calls(tree)]
            opened += [(f.name, n.lineno) for n in ast.walk(tree)
                       if isinstance(n, ast.Attribute) and n.attr in NETWORK_ENTRY_POINTS]
        self.assertEqual([name for name, _ in built], [REST], "要求を組み立てる場所は、rest.py に 1 つだけ")
        self.assertEqual([name for name, _ in opened], [REST], "通信の入口に触るのは、rest.py の 1 か所だけ")

    def test_the_client_has_no_way_to_choose_a_method(self):
        """ReadOnlyDiscord の公開メソッドは、読むためのものだけ。メソッド名や本文を受け取る引数が無い。"""
        tree = ast.parse((WATCHER_DIR / REST).read_text(encoding="utf-8"))
        (cls,) = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ReadOnlyDiscord"]
        public = {n.name: [a.arg for a in n.args.args] for n in cls.body
                  if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
        self.assertEqual(set(public), {"get", "need"})
        for name, args in public.items():
            self.assertFalse({"method", "data", "body", "json", "payload"} & set(args), name)

    def test_guard_actually_detects_violations(self):
        """検査自体が空振りしていないことの確認（自己テスト）。"""
        cases = {
            "import urllib.request\nurllib.request.Request(u, method='POST', data=b'x')\n": 3,  # import・POST・method
            "import urllib.request\nurllib.request.Request(u, data=b'x', method='GET')\n": 2,
            "import urllib.request\nurllib.request.Request(u, b'x', method='GET')\n": 2,
            "import urllib.request\nurllib.request.urlopen(u)\n": 2,
            "from urllib.request import urlopen\nurlopen(u)\n": 2,
            "import discord\n": 1,
            "import aiohttp\n": 1,
            "import subprocess\n": 1,
            "URL = 'https://discord.com/api/webhooks/1/x'\n": 1,
            "GATEWAY = 'wss://gateway.discord.gg'\n": 1,
            "import urllib.parse\nq = urllib.parse.quote('a')\n": 0,  # 文字の変換だけなら、通信しない
        }
        for src, expected in cases.items():
            found = _violations(ast.parse(src), "search.py")
            self.assertEqual(len(found), expected, "%r -> %s" % (src, found))
        # rest.py の中でも、GET 以外と、本文つきは許さない
        self.assertTrue(_violations(ast.parse("import urllib.request\nurllib.request.Request(u, method=m)\n"), REST))
        self.assertTrue(_violations(ast.parse("import urllib.request\nurllib.request.Request(u, method='GET', **kw)\n"), REST))
        self.assertEqual(_violations(ast.parse("import urllib.request\nurllib.request.Request(u, method='GET', headers=h)\n"), REST), [])

    def test_no_resident_connection(self):
        """常駐の接続（Gateway）を持たない。特権の Intent も要求しない（要求する場所が無い）。"""
        for f, _ in _sources():
            text = f.read_text(encoding="utf-8")
            for word in ("intents.", "Intents", "import discord", "websocket"):
                self.assertNotIn(word, text, "%s に %r がある" % (f.name, word))


if __name__ == "__main__":
    unittest.main()
