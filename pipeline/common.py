"""pipeline の各スクリプトが共有する、見張り対象（ホームディレクトリ）の解決。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from watcher import config as wconfig  # noqa: E402
from watcher.store import Home  # noqa: E402


def add_target_args(ap) -> None:
    ap.add_argument("--user", help="config.json の watchers[].name（省略時は最初の1人）")
    ap.add_argument("--home", help="ホームディレクトリを直接指定（テスト用。config を読まない）")
    ap.add_argument("--config", help="config.json のパス")


def resolve(args) -> tuple:
    """(Home, WatchedUser | None)。--home 指定時は watcher=None。"""
    home_arg = args.home or os.environ.get("MENTION_TRIAGE_HOME")
    if home_arg:
        return Home(Path(home_arg)), None
    cfg = wconfig.load(args.config)
    if args.user:
        for w in cfg.watchers:
            if w.name == args.user:
                return Home(w.home), w
        raise wconfig.ConfigError("watchers に %s が無い" % args.user)
    w = cfg.watchers[0]
    return Home(w.home), w


def ascii_hint(error) -> str:
    """設定の不備（日本語の文）から、ASCII の部分だけを残す。項目名は ASCII なので、どこが悪いかの手がかりになる。
    全文は `python triage.py check` が、日本語のまま出す。"""
    kept = "".join(ch if 32 < ord(ch) < 127 else " " for ch in str(error))
    return (" ".join(kept.split()) or "unreadable")[:160] + " hint=run:python_triage.py_check"


def p(path) -> str:
    """出力用: スラッシュ区切りの絶対パス。"""
    return str(Path(path).resolve()).replace("\\", "/")
