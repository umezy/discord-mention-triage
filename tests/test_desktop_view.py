"""取り込んだあと、見ていたセッションへ表示を戻す（desktop_view.py）のテスト。

本物のデスクトップ版の記録（%APPDATA%\\Claude）は読まない。一時フォルダに、同じ形の記録を作って試す。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import desktop_view  # noqa: E402

VIEWED = "local_11111111-1111-1111-1111-111111111111"
OTHER = "local_22222222-2222-2222-2222-222222222222"
CLI = "33333333-3333-3333-3333-333333333333"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "claude-code-sessions"
        self.opened = []
        self.slept = 0

    def tearDown(self):
        self._tmp.cleanup()

    def record(self, session_id, focused=None, org="acct-a/org-a", **extra):
        d = {"sessionId": session_id, "cliSessionId": session_id[len("local_"):], "cwd": "C:\\x"}
        if focused is not None:
            d["lastFocusedAt"] = focused
        d.update(extra)
        path = self.root / org / (session_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d), encoding="utf-8")
        return path

    def view(self, on_sleep=None, wait_sec=30.0):
        clock = [0.0]

        def sleep(sec):
            self.slept += 1
            clock[0] += sec
            if on_sleep:
                on_sleep(self.slept)

        return desktop_view.DesktopView(root=self.root, opener=self.opened.append, sleep=sleep,
                                        clock=lambda: clock[0], wait_sec=wait_sec)


class Viewed(Base):
    def test_the_most_recently_focused_session_across_accounts_and_orgs(self):
        self.record(OTHER, focused=1000)
        self.record(VIEWED, focused=3000, org="acct-b/org-b")
        self.record("local_44444444-4444-4444-4444-444444444444")  # 一度も開かれていない
        self.assertEqual(self.view().remember(), VIEWED)

    def test_deleted_and_other_files_are_not_sessions(self):
        self.record(VIEWED, focused=1000)
        (self.root / "acct-a" / "org-a" / "deleted_x").mkdir()
        (self.root / "acct-a" / "org-a" / "deleted_x" / "local_9.json").write_text('{"lastFocusedAt": 9999}', encoding="utf-8")
        (self.root / "acct-a" / "org-a" / "archived-sessions.idx").write_text('"lastFocusedAt": 9999', encoding="utf-8")
        self.assertEqual(self.view().remember(), VIEWED)

    def test_only_the_head_of_a_record_is_read(self):
        # 本文の入った大きい記録を丸ごと読まない。lastFocusedAt は頭の方にある
        self.record(VIEWED, focused=1000)
        self.record(OTHER, transcript="x" * (desktop_view.HEAD_BYTES + 10), lastFocusedAt=5000)
        self.assertEqual(self.view().remember(), VIEWED)

    def test_nothing_when_there_is_no_record(self):
        self.assertEqual(self.view().remember(), "")
        self.assertEqual(desktop_view.viewed_session(None), "")

    def test_the_place_is_under_appdata_on_windows_only(self):
        with mock.patch.object(desktop_view.os, "name", "nt"):
            self.assertEqual(desktop_view.sessions_root({"APPDATA": "C:/AD"}), Path("C:/AD") / "Claude" / "claude-code-sessions")
            self.assertIsNone(desktop_view.sessions_root({}))
        with mock.patch.object(desktop_view.os, "name", "posix"):
            self.assertIsNone(desktop_view.sessions_root({"APPDATA": "C:/AD"}))


class GoBack(Base):
    def test_waits_until_the_imported_session_opens_then_returns_to_the_viewed_one(self):
        self.record(VIEWED, focused=1000)

        def desktop_opens_it(n):
            if n == 3:
                self.record("local_" + CLI)  # 取り込まれた（まだ開いていない）
            if n == 5:
                self.record("local_" + CLI, focused=2000)  # 開いた
                self.assertEqual(self.opened, [])  # 開く前には、戻さない（戻しても、あとから上書きされる）

        self.assertEqual(self.view(desktop_opens_it).go_back(VIEWED, CLI), "ok")
        self.assertEqual(self.opened, ["claude://code/continue?session=" + VIEWED])
        self.assertEqual(self.slept, 5)

    def test_gives_up_when_the_imported_session_never_opens(self):
        self.record(VIEWED, focused=1000)
        self.assertEqual(self.view(wait_sec=1.0).go_back(VIEWED, CLI), "not_opened")
        self.assertEqual(self.opened, [])

    def test_nothing_to_return_to(self):
        self.record("local_" + CLI, focused=2000)
        self.assertEqual(self.view().go_back("", CLI), "nothing_viewed")
        self.assertEqual(self.view().go_back("local_" + CLI, CLI), "nothing_viewed")
        self.assertEqual(self.opened, [])

    def test_other_platforms_do_nothing(self):
        with mock.patch.object(desktop_view, "sessions_root", return_value=None):
            v = desktop_view.DesktopView(opener=self.opened.append)
            self.assertEqual(v.remember(), "")
            self.assertEqual(v.go_back(VIEWED, CLI), "unsupported")
        self.assertEqual(self.opened, [])

    def test_the_link_is_handed_to_the_os(self):
        if os.name != "nt":
            self.skipTest("os.startfile は Windows だけ")
        with mock.patch.object(desktop_view.os, "startfile") as start:
            desktop_view.open_link("claude://code/continue?session=" + VIEWED)
        start.assert_called_once_with("claude://code/continue?session=" + VIEWED)


class Listed(Base):
    """同じ流れの続きを書き足す前に、そのセッションがデスクトップ版の一覧に出ているかを見る（ADR-0038）。"""

    def test_listed_archived_missing(self):
        v = self.view()
        self.assertEqual(v.listed(CLI), "missing")  # 取り込まれていない、消された
        self.record("local_" + CLI, isArchived=False)
        self.assertEqual(v.listed(CLI), "listed")
        self.record("local_" + CLI, isArchived=True)
        self.assertEqual(v.listed(CLI), "archived")
        self.record("local_" + CLI, isArchived=False, org="acct-b/org-b")  # どこかの一覧に出ていれば、出ている
        self.assertEqual(v.listed(CLI), "listed")

    def test_an_unreadable_record_is_unknown(self):
        self.record("local_" + CLI)  # isArchived が無い（形が変わった）
        self.assertEqual(self.view().listed(CLI), "unknown")
        path = self.record("local_" + CLI)
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.view().listed(CLI), "unknown")

    def test_other_platforms_are_unknown(self):
        with mock.patch.object(desktop_view, "sessions_root", return_value=None):
            self.assertEqual(desktop_view.DesktopView().listed(CLI), "unknown")


if __name__ == "__main__":
    unittest.main()
