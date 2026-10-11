"""セットアップのスキル（.claude/skills/triage-setup）が、実際のコマンドとずれていないかを確かめる。

スキルは文章なので、動きそのものは試せない。ここで見るのは、書いてあるコマンドと指定が triage.py に本当にあること、
リンク先があること、鍵らしいものが書かれていないこと、README から呼び方が分かること。
"""

import argparse
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "pipeline"))

import triage  # noqa: E402

SKILL = REPO / ".claude" / "skills" / "triage-setup" / "SKILL.md"
# 鍵と ID を集める手順（Claude in Chrome で、相手の画面を開いて進める）。SKILL.md の手順 2 と 4 が読む
COLLECT = SKILL.parent / "references" / "collect-keys.md"
# コマンドを載せている文書。スキルと同じ手順を、人向けに書いている所
DOCS = (SKILL, COLLECT, REPO / "README.md", REPO / "docs" / "setup.md", REPO / "docs" / "troubleshooting.md",
        REPO / "docs" / "get-the-keys.md")
COMMAND_RE = re.compile(r"python triage\.py ([a-z]+)((?:[ \t]+(?:--[a-z-]+|<[^>\n]+>|\"[^\"\n]*\"|\d+))*)")
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def subcommands() -> dict:
    ap = triage.build_parser()
    return next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction)).choices


class SetupSkill(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")

    def test_it_has_a_name_and_a_description_that_says_when_to_use_it(self):
        self.assertTrue(self.text.startswith("---\n"))
        head = self.text.split("---\n")[1]
        self.assertIn("name: triage-setup\n", head)
        description = next(line for line in head.splitlines() if line.startswith("description: "))
        self.assertIn("セットアップして", description)
        self.assertLess(len(description), 1024)

    def test_every_command_it_mentions_exists(self):
        subs = subcommands()
        seen = set()
        for doc in DOCS:
            for sub, tail in COMMAND_RE.findall(doc.read_text(encoding="utf-8")):
                self.assertIn(sub, subs, "%s に、無いコマンド: python triage.py %s" % (doc.name, sub))
                for flag in re.findall(r"--[a-z-]+", tail):
                    self.assertIn(flag, subs[sub]._option_string_actions,
                                  "%s に、無い指定: python triage.py %s %s" % (doc.name, sub, flag))
                seen.add(sub)
        self.assertLessEqual({"init", "check", "run", "install", "status", "uninstall"}, seen)

    def test_it_creates_the_setup_without_asking_for_keys(self):
        # スキルは、鍵を会話に通さないために、質問なしの init を使う。その形が、書いてあること
        self.assertIn("--no-input", self.text)
        self.assertRegex(self.text, r"python triage\.py init --name <[^>]+> --user-id <[^>]+> --channel-id <[^>]+> --no-input")

    def test_its_links_point_to_files_that_exist(self):
        for doc in (SKILL, COLLECT):
            links = [x for x in LINK_RE.findall(doc.read_text(encoding="utf-8")) if not x.startswith(("http://", "https://"))]
            self.assertTrue(links)
            for link in links:
                target = (doc.parent / link.split("#")[0]).resolve()
                self.assertTrue(target.exists(), "%s のリンク切れ: %s" % (doc.name, link))
                self.assertIn(REPO, (target, *target.parents))

    def test_no_key_is_written_in_it(self):
        for doc in (SKILL, COLLECT):
            text = doc.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"discord(?:app)?\.com/api/webhooks/\d", text))
            self.assertIsNone(re.search(r"(?:TOKEN|WEBHOOK)=[^\s`|]", text))  # `名前=` の右に、何も書いていない

    def test_collecting_keys_keeps_the_keys_out_of_the_conversation(self):
        # 画面を開いて進める手順でも、鍵が出るボタンは人が押し、その間は画面を見ない。この 2 つが、手順書から消えていないこと
        self.assertIn("references/collect-keys.md", self.text)
        text = COLLECT.read_text(encoding="utf-8")
        for phrase in ("鍵が出るボタンは、相手が押す", "画面を見ない", "先に別のページへ移動してから", "クリップボードは、読まない",
                       "ほかの人がいるサーバーには、何もしない", "送るのは、相手"):
            self.assertIn(phrase, text)

    def test_the_readme_tells_how_to_call_it(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("/triage-setup", readme)
        self.assertIn("セットアップして", readme)


if __name__ == "__main__":
    unittest.main()
