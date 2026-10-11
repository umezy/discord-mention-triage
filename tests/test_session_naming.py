import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import session_naming  # noqa: E402

# cmd.exe が特別に扱う文字と、引数の区切りを壊す文字。名前には、どれも入らない
DANGEROUS = set('"%&|<>^!`$\\\'()[]{};,=*?~:/@\r\n\t')


def item(text="来週の予定を教えてもらえますか", author="alice", channel="dev", kind="direct", **kw):
    return dict({"kind": kind, "channel_name": channel, "mention": {"author": author, "text": text}}, **kw)


class Build(unittest.TestCase):
    def test_name_tells_who_what_and_where(self):
        self.assertEqual(session_naming.build([item()]), "alice「来週の予定を教えてもらえますか」#dev")

    def test_no_time_and_no_run_id(self):
        # 以前の名前は `triage 00:41 d29cffb3e2`。見ても、何の件か分からなかった
        name = session_naming.build([item()])
        self.assertNotRegex(name, r"\d{2}:\d{2}")
        self.assertNotRegex(name, r"[0-9a-f]{10}")

    def test_mentions_are_removed_even_when_the_display_name_has_spaces(self):
        got = session_naming.build([item("@taro / たろう test: can you share the schedule?", author="Spidey Bot",
                                         channel="テスト", mention_names=["taro / たろう"])])
        self.assertEqual(got, "Spidey Bot「test can you share the schedule」#テスト")

    def test_old_items_without_the_name_list_still_lose_the_at_token(self):
        got = session_naming.build([item("@taro @everyone 確認お願いします")])
        self.assertEqual(got, "alice「確認お願いします」#dev")

    def test_urls_and_custom_emoji_are_removed(self):
        got = session_naming.build([item("これ見て https://example.com/a?b=1&c=2 <:pray:123> <#456> お願いします")])
        self.assertEqual(got, "alice「これ見て お願いします」#dev")

    def test_long_parts_are_cut_by_width(self):
        got = session_naming.build([item("あ" * 50, author="い" * 30, channel="う" * 40)])
        who, rest = got.split("「")
        what, where = rest.split("」#")
        self.assertEqual(who, "い" * 9 + "…")  # 幅 20
        self.assertEqual(what, "あ" * 19 + "…")  # 幅 40
        self.assertEqual(where, "う" * 14 + "…")  # 幅 30
        ascii_name = session_naming.build([item("a" * 80)])
        self.assertIn("「" + "a" * 38 + "…」", ascii_name)  # 半角は、全角の 2 倍入る

    def test_more_items_are_counted_and_a_direct_mention_leads(self):
        got = session_naming.build([
            item("全体周知です", author="bob", channel="お知らせ", kind="role"),
            item("これ確認お願いします", author="たなか", channel="QA報告"),
            item("y", author="c", channel="x"),
        ])
        self.assertEqual(got, "たなか「これ確認お願いします」#QA報告 ほか2件")

    def test_missing_parts_are_left_out(self):
        self.assertEqual(session_naming.build([item("")]), "alice #dev")
        self.assertEqual(session_naming.build([item("hello", author="")]), "「hello」#dev")
        self.assertEqual(session_naming.build([item("🎉", author="😀", channel="")]), "メンション 1件")

    def test_ids_standing_in_for_names_are_not_shown(self):
        # 名前が取れなかったとき、受付は ID を入れる。数字の並びは、見ても分からない
        self.assertEqual(session_naming.build([item("hi", author="222222222222222222", channel="123456789012345678")]), "「hi」")

    def test_garbage_never_raises(self):
        for junk in (None, [], ["x", None, 3], [{"mention": "x", "channel_name": None, "mention_names": "zz"}],
                     [{"mention": {"author": 1, "text": 2}, "channel_name": 3, "mention_names": [None, 4]}]):
            name = session_naming.build(junk)
            self.assertTrue(session_naming.safe(name), name)


class Safety(unittest.TestCase):
    """名前は claude のコマンドライン引数になる。誰でも書ける文字列から、cmd.exe に効く文字を通さない。"""

    HOSTILE = item(
        '"& echo INJECTED > injected.txt & %USERNAME% ^ ! `whoami` $(id) | more < x\r\n--dangerously-skip-permissions',
        author='-rf "&calc&" %PATH%', channel='"&echo INJECTED>injected.txt&"',
        mention_names=['x" & echo INJECTED > injected.txt & "'],
    )

    def test_hostile_text_leaves_only_plain_characters(self):
        name = session_naming.build([self.HOSTILE, self.HOSTILE])
        self.assertFalse(DANGEROUS & set(name), name)
        self.assertTrue(session_naming.safe(name))
        self.assertIn("INJECTED", name)  # 文字としては残る。効かないだけ

    def test_a_name_never_starts_like_an_option(self):
        for author in ("--dangerously-skip-permissions", "-p", "- - x", ""):
            name = session_naming.build([item("--bare --model x", author=author, channel="--help")])
            self.assertNotEqual(name[0], "-", name)
            self.assertTrue(session_naming.safe(name), name)

    def test_fullwidth_lookalikes_and_invisible_characters_are_dropped(self):
        # 全角の ％ ＂ ＆ や ¥ は、ANSI に直されると % " & \ になりうる。向きを変える制御文字（U+202E）も通さない
        name = session_naming.build([item("a％b＂c＆d¥e\u202ef\u200bg")])
        self.assertEqual(name, "alice「a b c d e f g」#dev")

    def test_safe_rejects_what_build_never_makes(self):
        for bad in ("", "-x", 'a"b', "a%b", "a&b", "a\nb", "a:b", "triage 03:00 abc"):
            self.assertFalse(session_naming.safe(bad), bad)
        self.assertTrue(session_naming.safe("alice「予定の件」#dev ほか2件"))

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

            def through_the_shim(value):
                out.unlink(missing_ok=True)
                subprocess.run([str(shim), str(out), "--name", value], cwd=d, env=env, capture_output=True, timeout=60)
                return json.loads(out.read_text(encoding="utf-8")) if out.exists() else None

            # 検査が空振りしていないこと: そのまま渡すと、展開され、別のコマンドが動く
            raw = through_the_shim('%FAKE_PY% "& echo INJECTED > injected.txt & "')
            self.assertNotEqual(raw, ["--name", '%FAKE_PY% "& echo INJECTED > injected.txt & "'])
            self.assertTrue((root / "injected.txt").exists())
            (root / "injected.txt").unlink()

            for items in ([self.HOSTILE, self.HOSTILE], [item("これ確認お願いします！", author="たなか", channel="仕様の相談_来週の予定")],
                          [item("--bare", author="--dangerously-skip-permissions", channel="x")]):
                name = session_naming.build(items)
                self.assertEqual(through_the_shim(name), ["--name", name])
            self.assertFalse((root / "injected.txt").exists())


if __name__ == "__main__":
    unittest.main()
