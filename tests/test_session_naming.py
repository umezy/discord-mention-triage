import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import session_naming  # noqa: E402

# cmd.exe が特別に扱う文字と、引数の区切りを壊す文字。名前には、どれも入らない
DANGEROUS = set('"%&|<>^!`$\\\'()[]{};,=*?~:/@#\r\n\t')
NOW = datetime(2026, 10, 3, 9, 5, tzinfo=timezone(timedelta(hours=9)))


class Names(unittest.TestCase):
    """名前は、返答を書いた Claude が付ける（ADR-0043）。ここでは、通してよい形にするだけ。"""

    def test_a_plain_name_is_kept_as_it_is(self):
        for name in ("来週の打ち合わせの日程", "検証環境の URL の確認", "資料のお礼", "「仕様の相談」の続き"):
            self.assertEqual(session_naming.final(name), name)

    def test_a_continued_session_says_so(self):
        self.assertEqual(session_naming.final("資料のお礼", continued=True), "資料のお礼（続き）")
        self.assertEqual(session_naming.final("", continued=True), "")  # 名前が無ければ、付け直さない

    def test_a_long_name_is_cut_by_width(self):
        self.assertEqual(session_naming.final("あ" * 30), "あ" * 19 + "…")  # 幅 40（全角 20 字ぶん）
        self.assertEqual(session_naming.final("a" * 60), "a" * 38 + "…")  # 半角は、全角の 2 倍入る

    def test_spaces_are_squeezed_and_nothing_left_means_no_name(self):
        self.assertEqual(session_naming.final("  予定の   確認  "), "予定の 確認")
        for empty in ("", "   ", "🎉", "@#!", None):
            self.assertEqual(session_naming.final(empty), "", empty)

    def test_the_provisional_name_tells_when(self):
        # 付け直せなかったときに残る名前。いつの件かは分かるようにする
        self.assertEqual(session_naming.provisional(NOW), "メンション 10月3日 09時05分")
        self.assertEqual(session_naming.provisional(NOW, continued=True), "メンション 10月3日 09時05分（続き）")
        self.assertTrue(session_naming.safe(session_naming.provisional(NOW, continued=True)))


class Safety(unittest.TestCase):
    """名前は claude のコマンドライン引数になる。Claude が付けた名前でも、届いた本文に引きずられうるので、cmd.exe に効く文字を通さない。"""

    HOSTILE = '"& echo INJECTED > injected.txt & %USERNAME% ^ ! `whoami` $(id) | more < x\r\n--dangerously-skip-permissions'

    def test_hostile_text_leaves_only_plain_characters(self):
        name = session_naming.clean(self.HOSTILE, 200)
        self.assertFalse(DANGEROUS & set(name), name)
        self.assertTrue(session_naming.safe(name))
        self.assertIn("INJECTED", name)  # 文字としては残る。効かないだけ

    def test_a_name_never_starts_like_an_option(self):
        for said in ("--dangerously-skip-permissions", "-p", "- - x", "--bare --model x"):
            name = session_naming.final(said)
            self.assertNotEqual(name[:1], "-", name)
            self.assertTrue(session_naming.safe(name), name)

    def test_fullwidth_lookalikes_and_invisible_characters_are_dropped(self):
        # 全角の ％ ＂ ＆ や ¥ は、ANSI に直されると % " & \ になりうる。向きを変える制御文字（U+202E）も通さない
        self.assertEqual(session_naming.final("a％b＂c＆d¥e‮f​g"), "a b c d e f g")

    def test_safe_rejects_what_clean_never_makes(self):
        for bad in ("", "-x", 'a"b', "a%b", "a&b", "a\nb", "a:b", "a/b", "#dev"):
            self.assertFalse(session_naming.safe(bad), bad)
        self.assertTrue(session_naming.safe("「予定」の確認（続き）"))

    def test_the_name_survives_a_cmd_shim_unchanged(self):
        # npm で入れた claude は claude.cmd で、引数が cmd.exe を通る。" と & があれば別のコマンドが動き、%NAME% は展開される
        if os.name != "nt":
            self.skipTest("cmd.exe を通る経路の検査")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "echo_args.py").write_text(
                "import json, sys\n"
                "json.dump(sys.argv[2:], open(sys.argv[1], 'w', encoding='utf-8'), ensure_ascii=False)\n", encoding="utf-8")
            shim = root / "claude.cmd"
            shim.write_bytes(b'@ECHO off\r\nSETLOCAL\r\n"%FAKE_PY%" "%~dp0echo_args.py" %*\r\n')
            out = root / "argv.json"
            env = dict(os.environ, FAKE_PY=sys.executable)

            def through_the_shim(*args):
                out.unlink(missing_ok=True)
                subprocess.run([str(shim), str(out), *args], cwd=d, env=env, capture_output=True, timeout=60)
                return json.loads(out.read_text(encoding="utf-8")) if out.exists() else None

            # 検査が空振りしていないこと: そのまま渡すと、展開され、別のコマンドが動く
            raw = through_the_shim("--name", '%FAKE_PY% "& echo INJECTED > injected.txt & "')
            self.assertNotEqual(raw, ["--name", '%FAKE_PY% "& echo INJECTED > injected.txt & "'])
            self.assertTrue((root / "injected.txt").exists())
            (root / "injected.txt").unlink()

            # 起動するときの仮の名前（--name）と、付け直す名前（/rename の引数。run_claude.rename_command）
            for said in (self.HOSTILE, "「仕様の相談」の続き！", "--bare", "%FAKE_PY% & calc"):
                name = session_naming.final(said, continued=True)
                self.assertEqual(through_the_shim("-p", "/rename " + name), ["-p", "/rename " + name])
            temp = session_naming.provisional(NOW, continued=True)
            self.assertEqual(through_the_shim("--name", temp), ["--name", temp])
            self.assertFalse((root / "injected.txt").exists())


if __name__ == "__main__":
    unittest.main()
