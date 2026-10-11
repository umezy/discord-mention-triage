"""`python -m watcher`（受付を手で 1 回動かす入口）のテスト。Discord には繋がない。"""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.fake_discord import ME, FakeDiscord, sf
from watcher import __main__ as cli
from watcher.store import Home

TOKEN = "TOKEN-MUST-NOT-LEAK"


class WatcherCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg_path = self.root / "config.json"
        self.cfg_path.write_text(json.dumps({
            "state_root": str(self.root / "state"),
            "watchers": [{"name": "me", "user_id": str(ME)}, {"name": "other", "user_id": "5"}],
        }), encoding="utf-8")
        (self.root / "state").mkdir()
        (self.root / "state" / ".env").write_text("DISCORD_BOT_TOKEN=%s\n" % TOKEN, encoding="utf-8")
        self.home = Home(self.root / "state" / "me")
        self.fake = FakeDiscord()
        self._old_token = os.environ.pop("DISCORD_BOT_TOKEN", None)

    def tearDown(self):
        if self._old_token is not None:
            os.environ["DISCORD_BOT_TOKEN"] = self._old_token
        self._tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("watcher.rest.urllib.request.urlopen", self.fake), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--config", str(self.cfg_path), *argv])
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_check_does_not_touch_the_network(self):
        code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OK config="))
        self.assertTrue(out.endswith("watchers=me,other token=present"))
        self.assertEqual(self.fake.requests, [])
        self.assertNotIn(TOKEN, out)

    def test_missing_token_is_a_config_error(self):
        (self.root / "state" / ".env").unlink()
        code, out, err = self.run_cli("--check")
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("ERROR=config"))

    def test_unknown_user_is_a_config_error(self):
        code, _, err = self.run_cli("--user", "nobody", "--check")
        self.assertEqual(code, 2)
        self.assertIn("nobody", err)

    def test_one_poll_prints_one_ascii_line_and_writes_the_inbox(self):
        self.home.ensure().write_poll({"since": time.time() - 600})
        self.fake.add(sf(300, time.time()), "<@%d> お願いします" % ME, mentions=[ME])
        code, out, _ = self.run_cli("--user", "me")
        self.assertEqual((code, out), (0, "OK FOUND=1 EMITTED=1"))
        out.encode("ascii")
        self.assertEqual(len(list(self.home.inbox.glob("pending-*.json"))), 1)

    def test_dry_run_on_a_new_pc_only_proves_that_discord_can_be_read(self):
        code, out, _ = self.run_cli("--dry-run")
        self.assertEqual((code, out), (0, "OK FOUND=0 EMITTED=0 FIRST=1 dry=1"))
        self.assertGreater(self.fake.count("/messages/search"), 0)
        self.assertFalse(self.home.poll_path.exists())  # 何も書かない。本番の初回は、まだ来ていない

    def test_a_rejected_token_is_reported_with_exit_3(self):
        self.fake.fail["guilds"] = 401
        code, out, _ = self.run_cli()
        self.assertEqual((code, out), (3, "ERROR=poll http_401_guilds"))
        self.assertNotIn(TOKEN, out)


if __name__ == "__main__":
    unittest.main()
