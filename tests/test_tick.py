import json
import os
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import tick  # noqa: E402
from watcher.store import Home  # noqa: E402


def item(pr=False):
    return {
        "message_id": "1", "channel_id": "2", "channel_name": "dev", "guild_id": "3", "jump_url": "u",
        "direct": True, "pull_requests": [{"url": "x"}] if pr else [], "mention": {}, "context_before": [],
        "context_after": [],
    }


class TickTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Home(Path(self._tmp.name) / "me").ensure()
        self.now = time.time()

    def tearDown(self):
        self._tmp.cleanup()

    def args(self, dry=False):
        return Namespace(user=None, home=str(self.home.root), config=None, dry_run=dry)

    def put(self, name, pr=False, n=1):
        path = self.home.inbox / name
        path.write_text(json.dumps({"items": [item(pr) for _ in range(n)]}), encoding="utf-8")
        return path

    def run_tick(self, dry=False):
        return tick.run(self.args(dry), now=self.now)

    # --- 通常の流れ ---------------------------------------------------------------
    def test_idle(self):
        self.assertEqual(self.run_tick(), ("OK NEW=0 CLAIMED=0", 0))

    def test_claims_inbox_files(self):
        self.put("pending-a.json", n=2)
        line, code = self.run_tick()
        self.assertEqual(code, 0)
        self.assertIn("NEW=2 CLAIMED=1", line)
        for key in ("FILES=", "HOME="):
            self.assertIn(key, line)
        self.assertTrue((self.home.claims / "pending-a.json").exists())
        self.assertFalse((self.home.inbox / "pending-a.json").exists())

    def test_collect_respects_the_claim_limit(self):
        for n in "abc":
            self.put("pending-%s.json" % n)
        got = tick.collect(self.home, self.now, dry_run=False, limit=2)
        self.assertEqual([c.name for c in got["claimed"]], ["pending-a.json", "pending-b.json"])
        self.assertTrue((self.home.inbox / "pending-c.json").exists())  # 上限を超えた分は inbox に残る
        self.assertEqual(tick.collect(self.home, self.now, dry_run=False, limit=0)["claimed"], [])

    def test_output_is_one_ascii_line(self):
        self.put("pending-a.json")
        line, _ = self.run_tick()
        self.assertNotIn("\n", line)
        line.encode("ascii")

    def test_dry_run_changes_nothing(self):
        self.put("pending-a.json")
        line, code = self.run_tick(dry=True)
        self.assertEqual(code, 0)
        self.assertIn("dry=1", line)
        self.assertTrue((self.home.inbox / "pending-a.json").exists())
        self.assertFalse(self.home.claims.exists() and list(self.home.claims.glob("*.json")))

    # --- 滞留した claim の復旧 --------------------------------------------------------
    def age(self, path, seconds):
        t = self.now - seconds
        os.utime(path, (t, t))

    def test_stale_triage_claim_is_requeued_and_reclaimed_in_the_same_run(self):
        path = self.home.claims / "pending-a.json"
        path.write_text(json.dumps({"items": [item()]}), encoding="utf-8")
        self.age(path, tick.TRIAGE_TIMEOUT_SEC + 60)
        line, _ = self.run_tick()
        self.assertIn("REQUEUED=1", line)
        self.assertIn("CLAIMED=1", line)

    def test_gives_up_after_max_attempts(self):
        path = self.home.claims / "pending-a.json"
        path.write_text(json.dumps({"items": [item()]}), encoding="utf-8")
        self.age(path, tick.TRIAGE_TIMEOUT_SEC + 60)
        tick.save_meta(self.home, {"pending-a.json": {"attempts": tick.MAX_ATTEMPTS}})
        line, _ = self.run_tick()
        self.assertIn("FAILED=1", line)
        self.assertIn("FAILEDFILES=pending-a.json", line)
        self.assertTrue((self.home.failed / "pending-a.json").exists())

    # --- PR の URL を含む件も、ふつうの仕分けとして扱う ---------------
    def pr_claim(self, seconds):
        path = self.home.claims / "pending-pr.json"
        path.write_text(json.dumps({"items": [item(pr=True)]}), encoding="utf-8")
        self.age(path, seconds)
        return path

    def test_stale_claim_with_a_pr_url_is_requeued_like_any_other(self):
        self.pr_claim(tick.TRIAGE_TIMEOUT_SEC + 60)
        line, _ = self.run_tick()
        self.assertIn("REQUEUED=1", line)
        self.assertIn("CLAIMED=1", line)  # 同じ回で取り直して、仕分けに回す
        self.assertNotIn("STALE", line)

    def test_fresh_claim_with_a_pr_url_is_left_alone(self):
        path = self.pr_claim(30 * 60)  # 仕分け中（45 分の手前）
        line, _ = self.run_tick()
        self.assertEqual(line, "OK NEW=0 CLAIMED=0")
        self.assertTrue(path.exists())

    def test_a_pr_url_does_not_change_the_kind_or_the_timeout(self):
        path = self.pr_claim(0)
        self.assertEqual(tick.payload_kind(path), "triage")
        self.assertEqual(tick.timeout_for(tick.payload_kind(path)), tick.TRIAGE_TIMEOUT_SEC)

    def test_a_kind_left_in_the_ledger_by_an_older_version_is_ignored(self):
        # 古い版は、帳簿（claims_meta.json）に kind=pr を残し、その件を自動では戻さなかった
        self.pr_claim(tick.TRIAGE_TIMEOUT_SEC + 60)
        tick.save_meta(self.home, {"pending-pr.json": {"attempts": 1, "kind": "pr"}})
        line, _ = self.run_tick()
        self.assertIn("REQUEUED=1", line)

    def test_claim_with_a_pr_url_gives_up_after_max_attempts(self):
        self.pr_claim(tick.TRIAGE_TIMEOUT_SEC + 60)
        tick.save_meta(self.home, {"pending-pr.json": {"attempts": tick.MAX_ATTEMPTS, "kind": "pr"}})
        line, _ = self.run_tick()
        self.assertIn("FAILEDFILES=pending-pr.json", line)
        self.assertTrue((self.home.failed / "pending-pr.json").exists())

    def test_unreadable_claim_waits_longer_then_goes_through_the_same_retry(self):
        path = self.home.claims / "pending-bad.json"
        path.write_text("{not json", encoding="utf-8")
        self.age(path, tick.TRIAGE_TIMEOUT_SEC + 60)
        self.assertEqual(self.run_tick()[0], "OK NEW=0 CLAIMED=0")  # 読めない件は、長めに待つ
        self.age(path, tick.UNKNOWN_TIMEOUT_SEC + 60)
        self.assertIn("REQUEUED=1", self.run_tick()[0])


if __name__ == "__main__":
    unittest.main()
