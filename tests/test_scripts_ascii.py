"""PowerShell スクリプトは ASCII だけで書く。

BOM なしの UTF-8 を Windows PowerShell 5.1 は ANSI コードページ（日本語環境では cp932）で読むため、日本語の
コメントや文字列が構文エラーや文字化けの原因になる（実際に run-dispatch.ps1 で構文エラーになった）。
"""

import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class ScriptsAreAscii(unittest.TestCase):
    def test_all_powershell_scripts_are_ascii(self):
        files = sorted(SCRIPTS.glob("*.ps1"))
        self.assertTrue(files)
        for f in files:
            data = f.read_bytes()
            bad = [i for i, b in enumerate(data) if b > 127]
            self.assertEqual(bad, [], "%s に ASCII 以外の文字がある（先頭のバイト位置 %s）" % (f.name, bad[:3]))


class Installer(unittest.TestCase):
    def setUp(self):
        self.text = (SCRIPTS / "install-task.ps1").read_text(encoding="ascii")

    def test_there_is_one_installer_and_one_task(self):
        self.assertEqual(sorted(f.name for f in SCRIPTS.glob("install-*.ps1")), ["install-task.ps1"])
        self.assertEqual(self.text.count("Register-ScheduledTask"), 1)

    def test_launches_powershell_through_headless_conhost(self):
        """タスクスケジューラが、起動のたびに黒い画面を出さないこと。

        `powershell.exe -WindowStyle Hidden` だけでは、黒い画面が一瞬出る。`conhost.exe --headless` 経由で起動する。"""
        self.assertIn("--headless powershell.exe", self.text)
        self.assertIn("conhost.exe", self.text)

    def test_one_run_at_a_time_and_long_enough_for_claude(self):
        # dispatch は、Claude が終わるまで動き続ける。途中で打ち切られず、重ねて起動もされないこと
        self.assertIn("-MultipleInstances IgnoreNew", self.text)
        self.assertIn("-ExecutionTimeLimit (New-TimeSpan -Minutes 60)", self.text)

    def test_removes_the_two_tasks_of_the_resident_watcher_era(self):
        for old in ("mention-triage-watcher", "mention-triage-dispatch"):
            self.assertIn(old, self.text)
        self.assertIn("Unregister-ScheduledTask -TaskName $old", self.text)

    def test_a_failed_registration_is_reported_and_exits_non_zero(self):
        # 以前の登録スクリプトは、登録に失敗しても "registered" と出していた
        self.assertIn("-ErrorAction Stop", self.text)
        self.assertIn("FAILED to register", self.text)
        failed = self.text.index("FAILED to register")
        self.assertIn("exit 1", self.text[failed:failed + 200])
        self.assertLess(self.text.rindex("exit 1"), self.text.rindex('Write-Host "registered:'))

    def test_the_idle_line_matches_what_dispatch_prints(self):
        """新着が無い回を、ログに残さない。run-dispatch.ps1 の判定と、dispatch.py の出力が、ずれていないこと。"""
        import re

        text = (SCRIPTS / "run-dispatch.ps1").read_text(encoding="ascii")
        (pattern,) = re.findall(r"\$idle = '([^']+)'", text)
        for idle in ("OK POLL=ok FOUND=0 EMITTED=0 CLAIMED=0 RAN=0", "OK SKIPPED=locked",
                     "OK POLL=resident_watcher CLAIMED=0 RAN=0"):
            self.assertRegex(idle, pattern)
        for busy in ("OK POLL=ok FOUND=1 EMITTED=1 CLAIMED=1 RAN=1 RESULT=done",
                     "OK POLL=down:http_401_guilds CLAIMED=0 RAN=0",
                     "OK POLL=ok FOUND=1 EMITTED=0 RETRY=1 CLAIMED=0 RAN=0",
                     "OK POLL=resident_watcher CLAIMED=1 RAN=1 RESULT=done",
                     "ERROR=dispatch KeyError"):
            self.assertNotRegex(busy, pattern)


if __name__ == "__main__":
    unittest.main()
