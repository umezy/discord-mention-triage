import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import same_topic  # noqa: E402


def item(before=(), text="来週の打ち合わせの資料、見てもらえますか"):
    return {"channel_id": "20", "message_id": "300",
            "mention": {"author": "carol", "text": text},
            "context_before": [dict({"author": "bob", "text": "x"}, **b) for b in before]}


class Command(unittest.TestCase):
    def test_no_tools_no_settings_no_transcript(self):
        cmd = same_topic.command("claude")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, cmd)
        for flag in ("--resume", "--add-dir", "--allowedTools", "--bare"):
            self.assertNotIn(flag, cmd)

    def test_the_system_prompt_survives_cmd_exe(self):
        self.assertTrue(same_topic.SYSTEM.isascii())
        for ch in '<>&|^%"':
            self.assertNotIn(ch, same_topic.SYSTEM)


class Prompt(unittest.TestCase):
    def test_shows_the_earlier_mention_what_came_after_and_the_new_one(self):
        before = [{"id": "90", "text": "ずっと前の話"}, {"id": "100", "author": "alice", "text": "ビルドが落ちます"},
                  {"id": "110", "author": "me", "text": "見てみます"}]
        text = same_topic.prompt("ビルドの失敗の調査", [item(before)], ["100"])
        body = text.split("<items>", 1)[1]
        self.assertIn("前のセッションの名前: ビルドの失敗の調査", body)
        self.assertIn("前に仕分けたメンション（alice）: ビルドが落ちます", body)
        self.assertIn("そのあとの発言（me）: 見てみます", body)
        self.assertIn("新しいメンション（carol）: 来週の打ち合わせの資料、見てもらえますか", body)
        self.assertNotIn("ずっと前の話", text)  # 前の件より前の発言は、読ませない
        self.assertTrue(text.rstrip().endswith("</items>"))

    def test_long_text_is_clipped_and_only_the_last_few_replies_are_shown(self):
        before = [{"id": "100", "text": "a" * 2000}] + [{"id": str(200 + i), "text": "r%d" % i} for i in range(9)]
        text = same_topic.prompt("n", [item(before)], ["100"])
        self.assertIn("a" * same_topic.TEXT_MAX, text)
        self.assertNotIn("a" * (same_topic.TEXT_MAX + 1), text)
        self.assertEqual(text.count("そのあとの発言"), same_topic.AFTER_MAX)
        self.assertNotIn("r3", text)
        self.assertIn("r8", text)


class Parse(unittest.TestCase):
    def out(self, result, **extra):
        return json.dumps(dict({"result": result, "total_cost_usd": 0.001}, **extra))

    def test_only_same_or_new_is_taken(self):
        self.assertEqual(same_topic.parse(self.out("same")), ("same", 0.001))
        self.assertEqual(same_topic.parse(self.out("考えました\n**New**。")), ("new", 0.001))
        self.assertEqual(same_topic.parse(self.out("maybe")), ("error", 0.001))
        self.assertEqual(same_topic.parse(self.out("same", is_error=True)), ("error", 0.001))
        self.assertEqual(same_topic.parse("not json"), ("error", None))
        self.assertEqual(same_topic.parse("[]"), ("error", None))


class Ask(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = Path(self._tmp.name) / "run"

    def test_the_prompt_goes_on_stdin(self):
        seen = {}

        def run(cmd, input=None, cwd=None, **kw):
            seen.update(cmd=cmd, input=input.decode("utf-8"), cwd=cwd)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "new", "total_cost_usd": 0.002}).encode(), b"")

        res = same_topic.ask("claude", "n", [item([{"id": "100", "text": "前の件"}])], ["100"], self.cwd, run=run)
        self.assertEqual(res, ("new", 0.002))
        self.assertIn("前の件", seen["input"])
        self.assertNotIn("前の件", " ".join(seen["cmd"]))  # 本文は引数に載せない
        self.assertEqual(seen["cwd"], str(self.cwd))

    def test_failures_are_errors_not_exceptions(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("claude", 1)

        self.assertEqual(same_topic.ask("claude", "n", [item()], [], self.cwd, run=boom), ("error", None))
        self.assertEqual(same_topic.ask("", "n", [item()], [], self.cwd), ("error", None))


if __name__ == "__main__":
    unittest.main()
