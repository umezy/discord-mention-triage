import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import post_digest  # noqa: E402
from watcher import config  # noqa: E402
from watcher.store import Home  # noqa: E402

VALID = {
    "state_root": str(Path(tempfile.gettempdir()) / "mention-triage-test-state"),
    "watchers": [
        {
            "name": "me",
            "user_id": "123456789012345678",
            "channel_blocklist": ["9"],
            "pr_policy": {"allowed_orgs": ["ExampleOrg"], "requester_role_ids": ["5"]},
        }
    ],
}


class Config(unittest.TestCase):
    def test_parse_valid(self):
        cfg = config.parse(VALID)
        w = cfg.watchers[0]
        self.assertEqual(w.user_id, 123456789012345678)
        self.assertEqual(w.channel_blocklist, frozenset({9}))
        self.assertEqual(w.pr_policy.allowed_orgs, frozenset({"exampleorg"}))
        self.assertEqual(w.pr_policy.requester_role_ids, frozenset({5}))
        self.assertEqual(w.home, cfg.state_root / "me")

    def test_rejects_bad_name_missing_id_and_empty(self):
        for bad in (
            {"watchers": [{"name": "Bad Name", "user_id": "1"}]},
            {"watchers": [{"name": "ok", "user_id": "REPLACE_ME"}]},
            {"watchers": [{"name": "ok"}]},
            {"watchers": []},
            {"watchers": [{"name": "a", "user_id": "1"}, {"name": "a", "user_id": "2"}]},
        ):
            with self.assertRaises(config.ConfigError):
                config.parse(bad)

    def test_state_must_not_live_inside_the_repo(self):
        # 状態にはメッセージ本文・ドラフト・トークンが入る。リポジトリ内だとコミットしてしまう。
        for bad in (str(REPO / "state"), "state", "."):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.parse(dict(VALID, state_root=bad))
        with self.assertRaises(config.ConfigError):
            config.parse(dict(VALID, token_env_path=str(REPO / ".env")))

    def test_empty_state_root_falls_back_to_the_default_under_home(self):
        cfg = config.parse(dict(VALID, state_root=""))
        self.assertEqual(cfg.state_root, Path.home() / ".discord-mention-triage")

    def test_pr_policy_defaults_to_closed(self):
        w = config.parse({"watchers": [{"name": "a", "user_id": "1"}]}).watchers[0]
        self.assertFalse(w.pr_policy.allowed_orgs)
        self.assertFalse(w.pr_policy.requester_role_ids)

    def test_pr_auto_review_cannot_be_switched_on(self):
        # 有効にする実装は無い（ADR-0004、ADR-0013）。true を黙って読み飛ばさない
        def with_flag(value):
            return {"watchers": [dict(VALID["watchers"][0], pr_auto_review=value)]}

        with self.assertRaises(config.ConfigError) as cm:
            config.parse(with_flag(True))
        self.assertIn("pr_auto_review", str(cm.exception))
        config.parse(with_flag(False))  # 古い設定ファイルに残っていても、false なら読める

    def test_settings_of_the_removed_pr_review_path_are_ignored(self):
        w = config.parse({"watchers": [dict(VALID["watchers"][0], pr_review_workdir="C:/work")]}).watchers[0]
        self.assertFalse(hasattr(w, "pr_review_workdir"))
        self.assertFalse(hasattr(w, "pr_auto_review"))

    def test_settings_of_the_resident_watcher_are_ignored_and_the_overlap_is_read(self):
        old = dict(VALID, heartbeat_seconds=30, sweep_seconds=10, catchup_max_messages_per_channel=2000,
                   max_parallel_runs=2, search_overlap_minutes=45, catchup_max_age_hours=24)
        cfg = config.parse(old)
        self.assertEqual((cfg.search_overlap_minutes, cfg.catchup_max_age_hours), (45, 24))
        for gone in ("heartbeat_seconds", "sweep_seconds", "catchup_max_messages_per_channel", "max_parallel_runs"):
            self.assertFalse(hasattr(cfg, gone), gone)
        self.assertEqual(config.parse(VALID).search_overlap_minutes, 30)

    def test_token_from_env_then_file_and_never_in_errors(self):
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            cfg = config.parse(dict(VALID, token_env_path=str(env_file)))
            old = os.environ.pop(config.TOKEN_KEY, None)
            try:
                with self.assertRaises(config.ConfigError) as cm:
                    config.load_token(cfg)
                self.assertNotIn("SECRET", str(cm.exception))
                env_file.write_text("# c\nDISCORD_BOT_TOKEN=\"SECRET-FILE\"\n", encoding="utf-8")
                self.assertEqual(config.load_token(cfg), "SECRET-FILE")
                os.environ[config.TOKEN_KEY] = "SECRET-ENV"
                self.assertEqual(config.load_token(cfg), "SECRET-ENV")
            finally:
                os.environ.pop(config.TOKEN_KEY, None)
                if old is not None:
                    os.environ[config.TOKEN_KEY] = old

    def test_files_saved_with_a_bom_are_read(self):
        # Windows のメモ帳は、UTF-8 を BOM つきで保存することがある。先頭の 1 行（＝いちばん大事な鍵）が読めなくなっていた
        bom = b"\xef\xbb\xbf"
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            env_file.write_bytes(bom + b"DISCORD_BOT_TOKEN=SECRET-FILE\r\n")
            cfg_file = Path(d) / "config.json"
            cfg_file.write_bytes(bom + json.dumps(dict(VALID, token_env_path=str(env_file))).encode("utf-8"))
            old = os.environ.pop(config.TOKEN_KEY, None)
            try:
                cfg = config.load(str(cfg_file))
                self.assertEqual(config.load_token(cfg), "SECRET-FILE")
            finally:
                if old is not None:
                    os.environ[config.TOKEN_KEY] = old

    def test_example_config_parses_once_placeholders_are_filled(self):
        text = (REPO / "config" / "config.example.json").read_text(encoding="utf-8")
        data = json.loads(text)
        data["watchers"][0]["user_id"] = "1"
        data["watchers"][0]["channel_blocklist"] = ["2"]
        data["watchers"][0]["pr_policy"]["requester_role_ids"] = ["3"]
        config.parse(data)


class Store(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Home(Path(self._tmp.name) / "me").ensure()

    def tearDown(self):
        self._tmp.cleanup()

    def test_seen_dedupes(self):
        self.assertFalse(self.home.is_known(10))
        self.home.mark_seen(10)
        self.assertTrue(self.home.is_known(10))

    def test_poll_state_is_none_until_the_first_run(self):
        self.assertIsNone(self.home.read_poll())
        self.home.write_poll({"since": 123.5})
        self.assertEqual(self.home.read_poll(), {"since": 123.5})
        self.home.poll_path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(self.home.read_poll())  # 壊れていたら、初回として扱う

    def test_heartbeat_left_by_the_resident_watcher_is_readable(self):
        self.assertIsNone(self.home.read_legacy_heartbeat())
        (self.home.root / "heartbeat.json").write_text(json.dumps({"ts": 1000.0, "ready": True}), encoding="utf-8")
        self.assertEqual(self.home.read_legacy_heartbeat(), 1000.0)
        (self.home.root / "heartbeat.json").write_text("[]", encoding="utf-8")
        self.assertIsNone(self.home.read_legacy_heartbeat())

    def test_seen_is_capped(self):
        for i in range(2105):
            self.home.mark_seen(i)
        self.assertFalse(self.home.is_known(0))
        self.assertTrue(self.home.is_known(2104))

    def test_write_inbox_is_atomic_and_globbable(self):
        now = datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc)
        path = self.home.write_inbox([{"message_id": "123456789"}], now)
        self.assertTrue(path.name.startswith("pending-20261003-030000-"))
        self.assertEqual([p.name for p in self.home.inbox.glob("pending-*.json")], [path.name])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["items"][0]["message_id"], "123456789")
        self.assertEqual([p.name for p in self.home.inbox.iterdir() if p.name.endswith(".tmp")], [])

    def test_alert_cooldown(self):
        self.assertTrue(self.home.should_alert("k", 100, now=1000))
        self.assertFalse(self.home.should_alert("k", 100, now=1050))
        self.assertTrue(self.home.should_alert("k", 100, now=1101))
        self.home.clear_alert("k")
        self.assertTrue(self.home.should_alert("k", 100, now=1102))


class EnvFile(unittest.TestCase):
    def test_first_key_survives_a_bom(self):
        with tempfile.TemporaryDirectory() as d:
            home = Home(Path(d) / "me").ensure()
            (home.root / ".env").write_bytes(
                b"\xef\xbb\xbfDIGEST_WEBHOOK=http://example.invalid/h\r\nDIGEST_MENTION_USER_ID=42\r\n")
            self.assertEqual(post_digest.read_env_file(home.root / ".env"),
                             {"DIGEST_WEBHOOK": "http://example.invalid/h", "DIGEST_MENTION_USER_ID": "42"})
            url, mention, _ = post_digest.read_target(home)
            self.assertEqual((url, mention), ("http://example.invalid/h", "42"))

    def test_without_a_bom_nothing_changes(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text("# c\nA=1\nB=\"2\"\n", encoding="utf-8")
            self.assertEqual(post_digest.read_env_file(f), {"A": "1", "B": "2"})


class Digest(unittest.TestCase):
    def test_split_mark_always_splits(self):
        self.assertEqual(post_digest.split_body("a\n%%SPLIT%%\nb"), ["a", "b"])

    def test_long_body_is_chunked_under_the_limit(self):
        body = "\n".join("line %04d " % i + "x" * 80 for i in range(100))
        chunks = post_digest.split_body(body)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= post_digest.DISCORD_LIMIT for c in chunks))
        self.assertEqual("\n".join(chunks).count("line "), 100)

    def test_code_fence_is_closed_and_reopened_across_chunks(self):
        body = "head\n```\n" + "\n".join("code %04d " % i + "y" * 80 for i in range(60)) + "\n```\ntail"
        chunks = post_digest.split_body(body)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.count("```") % 2, 0, "コードブロックが開いたまま切れている")

    def test_empty_parts_are_dropped(self):
        self.assertEqual(post_digest.split_body("%%SPLIT%%\n\n%%SPLIT%%"), [])


if __name__ == "__main__":
    unittest.main()
