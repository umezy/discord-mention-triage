"""Claude Code の実行ファイル（claude.exe）を探す。

優先順: 設定 claude_bin → 環境変数 CLAUDE_BIN → PATH 上の claude → 公式インストーラーの置き場（~/.local/bin）→
デスクトップアプリ同梱の最新版。公式インストーラーは PATH を更新しないことがあり、更新後もタスクスケジューラが起動した
プロセスには反映が遅れることがあるので、置き場も直接見る。

デスクトップアプリ同梱の場所は版名つき（%APPDATA%/Claude/claude-code/2.1.284/claude.exe）で、アプリの更新で
変わる。そのため版を固定せず、毎回いちばん新しいものを探す。アプリの内部フォルダなので、いつか形が変わる
かもしれない。安定させたいときは公式の CLI を別に入れて、PATH か claude_bin で指す。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path


def _version_key(name: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", name)) or (0,)


def bundled_candidates(appdata: str | None = None) -> list:
    appdata = appdata if appdata is not None else os.environ.get("APPDATA", "")
    if not appdata:
        return []
    root = Path(appdata) / "Claude" / "claude-code"
    if not root.is_dir():
        return []
    found = []
    for d in root.iterdir():
        exe = d / "claude.exe"
        if d.is_dir() and exe.is_file():
            found.append((_version_key(d.name), exe))
    return [exe for _, exe in sorted(found, reverse=True)]


def native_install_candidates(home: str | None = None) -> list:
    home_dir = Path(home) if home else Path.home()
    return [home_dir / ".local" / "bin" / name for name in ("claude.exe", "claude")]


def find_claude(configured: str = "", appdata: str | None = None, env: dict | None = None,
                home: str | None = None) -> str | None:
    env = os.environ if env is None else env
    for cand in (configured, env.get("CLAUDE_BIN", "")):
        if cand and Path(cand).is_file():
            return str(Path(cand))
    on_path = shutil.which("claude", path=env.get("PATH"))
    if on_path:
        return on_path
    for exe in native_install_candidates(home):
        if exe.is_file():
            return str(exe)
    bundled = bundled_candidates(appdata)
    return str(bundled[0]) if bundled else None
