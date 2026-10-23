import json
import os
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import dispatch  # noqa: E402
import run_claude  # noqa: E402
import tick  # noqa: E402
from tests.fake_discord import FakeDiscord  # noqa: E402
from watcher import config, search  # noqa: E402
from watcher.store import Home  # noqa: E402

FAKE_CLAUDE = [sys.executable, str(REPO / "tests" / "fake_claude.py")]
TOKEN = "TOKEN-MUST-NOT-LEAK"
MINUTE = 60


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg_path = self.root / "config.json"
        self.cfg_path.write_text(json.dumps({
            "state_root": str(self.root / "state"), "token_env_path": str(self.root / "state" / ".env"),
            "claude_timeout_sec": 30,
            "watchers": [{"name": "me", "user_id": "111", "channel_blocklist": ["999"]}],
        }), encoding="utf-8")
        self.cfg = config.load(str(self.cfg_path))
        self.home = Home(self.cfg.watchers[0].home).ensure()
        (self.cfg.state_root / ".env").write_text("DISCORD_BOT_TOKEN=%s\n" % TOKEN, encoding="utf-8")
        (self.home.root / ".env").write_text("DIGEST_WEBHOOK=http://example.invalid/h\nDIGEST_MENTION_USER_ID=42\n", encoding="utf-8")
        self.now = time.time()
        self.posts, self.runs, self.polls = [], [], []
        self.poll_result = search.PollResult()
        self._old_token = os.environ.pop("DISCORD_BOT_TOKEN", None)

    def tearDown(self):
        if self._old_token is not None:
            os.environ["DISCORD_BOT_TOKEN"] = self._old_token
        self._tmp.cleanup()

    def put(self, name="pending-a.json"):
        (self.home.inbox / name).write_text(json.dumps({"items": [{"message_id": "1", "pull_requests": []}]}), encoding="utf-8")

    def poster(self, url, content, mention):
        self.posts.append(content)
        return 204

    def runner(self, home, watcher, cfg, files, poster=None):
        self.runs.append([Path(f).name for f in files])
        return {"ok": True, "reason": "done"}

    def poller(self, cfg, watcher, home, dry_run, backfill_hours):
        """受付の代わり。結果だけ返す（読めなかった回は、本物と同じく、いつから読めていないかを記録する）。"""
        self.polls.append({"dry_run": dry_run, "backfill_hours": backfill_hours, "watcher": watcher.name})
        if not dry_run:
            state = home.read_poll() or {}
            state["fail_since"] = None if self.poll_result.ok else (state.get("fail_since") or self.now)
            home.write_poll(state)
        return self.poll_result

    def down(self, reason="http_500_search"):
        self.poll_result = search.PollResult()
        self.poll_result.fail(reason)

    def run_dispatch(self, dry=False, poster=None, backfill=0, **kw):
        args = Namespace(user="me", home=None, config=str(self.cfg_path), dry_run=dry, backfill_hours=backfill)
        kw.setdefault("runner", self.runner)
        kw.setdefault("poller", self.poller)
        return dispatch.run(args, poster=poster or self.poster, now=self.now, **kw)


class DispatchTest(Base):
    # --- 新着が無い回は、Claude を動かさない -----------------------------------------
    def test_idle_runs_nothing(self):
        self.assertEqual(self.run_dispatch(), ("OK POLL=ok FOUND=0 EMITTED=0 CLAIMED=0 RAN=0", 0))
        self.assertEqual((self.runs, self.posts), ([], []))
        self.assertEqual(len(self.polls), 1)  # 受付（検索）は、毎回動く

    def test_new_mentions_are_claimed_and_triaged_in_the_same_run(self):
        self.put("pending-a.json")
        self.poll_result = search.PollResult(found=1, emitted=1)
        line, code = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(line, "OK POLL=ok FOUND=1 EMITTED=1 CLAIMED=1 RAN=1 RESULT=done")
        self.assertEqual(self.runs, [["pending-a.json"]])
        self.assertTrue((self.home.claims / "pending-a.json").exists())

    def test_each_run_triages_only_the_oldest_mention(self):
        # 別々のメンションを、同じ回に届いたというだけで、同じセッションにしない（ADR-0041）。残りは次の回へ
        for name in ("pending-20261023-0701-b.json", "pending-20261023-0700-a.json", "pending-20261023-0702-c.json"):
            self.put(name)
        self.poll_result = search.PollResult(found=3, emitted=3)
        self.assertEqual(self.run_dispatch()[0], "OK POLL=ok FOUND=3 EMITTED=3 CLAIMED=1 RAN=1 RESULT=done")
        self.poll_result = search.PollResult()
        for _ in range(3):
            self.run_dispatch()
        self.assertEqual(self.runs, [["pending-20261023-0700-a.json"], ["pending-20261023-0701-b.json"],
                                     ["pending-20261023-0702-c.json"]])
        self.assertEqual(self.run_dispatch()[0], "OK POLL=ok FOUND=0 EMITTED=0 CLAIMED=0 RAN=0")  # 空になったら、Claude は動かない

    def test_retried_and_skipped_counts_are_reported(self):
        self.poll_result = search.PollResult(found=4, emitted=0, retrying=1, skips={"replied": 1, "reacted": 2})
        self.assertEqual(self.run_dispatch()[0],
                         "OK POLL=ok FOUND=4 EMITTED=0 RETRY=1 SKIP=reacted:2,replied:1 CLAIMED=0 RAN=0")

    def test_dry_run_changes_and_starts_nothing(self):
        self.put()
        line, _ = self.run_dispatch(dry=True)
        self.assertIn("dry=1", line)
        self.assertEqual(self.polls[0]["dry_run"], True)
        self.assertEqual(self.runs, [])
        self.assertTrue((self.home.inbox / "pending-a.json").exists())

    def test_backfill_hours_reaches_the_poller(self):
        self.run_dispatch(backfill=6)
        self.assertEqual(self.polls[0]["backfill_hours"], 6)

    def test_output_is_one_ascii_line(self):
        self.put()
        self.down("http_401_guilds")
        line, _ = self.run_dispatch()
        self.assertNotIn("\n", line)
        line.encode("ascii")

    def test_a_failed_triage_is_reported_in_the_line(self):
        self.put()
        line, code = self.run_dispatch(runner=lambda *a, **k: {"ok": False, "reason": "claude_exit_1"})
        self.assertEqual(code, 0)  # 仕分けの失敗は、run_claude が通知して、再試行に回す
        self.assertIn("RAN=1 RESULT=claude_exit_1", line)

    # --- Discord を読めないとき ---------------------------------------------------------
    def test_a_short_outage_is_not_reported(self):
        self.down()
        line, code = self.run_dispatch()
        self.assertEqual((line, code), ("OK POLL=down:http_500_search CLAIMED=0 RAN=0", 0))
        self.now += 10 * MINUTE
        self.run_dispatch()
        self.assertEqual(self.posts, [])

    def test_a_long_outage_is_reported_once_an_hour(self):
        self.down()
        self.run_dispatch()
        self.now += 16 * MINUTE
        self.run_dispatch()
        self.assertEqual(len(self.posts), 1)
        self.assertIn("Discord を読めていません", self.posts[0])
        self.assertIn("http_500_search", self.posts[0])
        self.assertIn("16 分前から", self.posts[0])
        self.now += 10 * MINUTE
        self.run_dispatch()
        self.assertEqual(len(self.posts), 1)  # 同じ停止では連投しない
        self.now += 60 * MINUTE
        self.run_dispatch()
        self.assertEqual(len(self.posts), 2)

    def test_a_rejected_token_is_reported_at_once(self):
        for reason in ("http_401_guilds", "no_token"):
            with self.subTest(reason):
                self.posts.clear()
                self.home.clear_alert("poll_down")
                self.down(reason)
                self.run_dispatch()
                self.assertEqual(len(self.posts), 1)
                self.assertNotIn(TOKEN, self.posts[0])

    def test_recovery_rearms_the_alert(self):
        self.down("http_401_guilds")
        self.run_dispatch()
        self.poll_result = search.PollResult()
        self.run_dispatch()
        self.down("http_401_guilds")
        self.run_dispatch()
        self.assertEqual(len(self.posts), 2)

    def test_work_already_in_the_inbox_is_still_processed_while_discord_is_unreadable(self):
        self.put()
        self.down()
        line, _ = self.run_dispatch()
        self.assertIn("POLL=down:http_500_search CLAIMED=1 RAN=1", line)
        self.assertEqual(self.runs, [["pending-a.json"]])

    def test_outage_with_no_webhook_is_reported_as_failure(self):
        (self.home.root / ".env").unlink()
        self.down("http_401_guilds")
        line, code = self.run_dispatch()
        self.assertEqual(code, 3)
        self.assertIn("ALERT_FAILED=1", line)

    def test_dry_run_never_alerts(self):
        self.down("http_401_guilds")
        self.run_dispatch(dry=True)
        self.assertEqual(self.posts, [])

    # --- Claude を呼べない間は、溜めて待つ ----------------------------------------------
    def test_auth_block_stops_claiming_until_it_expires(self):
        self.put()
        runs = self.home.root / "runs"
        runs.mkdir(exist_ok=True)
        (runs / "blocked.json").write_text(json.dumps({"reason": "claude_auth", "until": self.now + 300}), encoding="utf-8")
        line, _ = self.run_dispatch()
        self.assertIn("BLOCKED=claude_auth", line)  # 理由は止め札のファイルから読む
        self.assertIn("CLAIMED=0 RAN=0", line)
        self.assertTrue((self.home.inbox / "pending-a.json").exists())  # 溜めたまま
        self.assertEqual(len(self.polls), 1)  # 受付は止めない（届いた分は、拾って溜める）
        (runs / "blocked.json").write_text(json.dumps({"reason": "claude_auth", "until": self.now - 1}), encoding="utf-8")
        line, _ = self.run_dispatch()
        self.assertIn("CLAIMED=1 RAN=1", line)  # 期限が切れたら再開

    # --- 同時に動くのは 1 つだけ -----------------------------------------------------------
    def test_overlapping_run_does_nothing(self):
        self.put()
        with dispatch.ProcessLock(self.home.root / "dispatch.lock") as held:
            self.assertTrue(held.held)
            self.assertEqual(self.run_dispatch(), ("OK SKIPPED=locked", 0))
        self.assertEqual((self.polls, self.runs), ([], []))
        self.assertIn("RAN=1", self.run_dispatch()[0])  # 放せば、次の回は動く

    def test_the_lock_is_held_while_claude_works(self):
        self.put()
        seen = []

        def slow_runner(home, watcher, cfg, files, poster=None):
            seen.append(self.run_dispatch()[0])  # Claude が動いている最中に、次の回が起動された
            return {"ok": True, "reason": "done"}

        self.run_dispatch(runner=slow_runner)
        self.assertEqual(seen, ["OK SKIPPED=locked"])

    def test_a_lock_file_left_by_a_dead_run_does_not_block(self):
        # 古い版は、ロックのファイルを作って消していた。落ちると残り、120 秒待つ作りだった。いまは OS のロックなので、残っていても動く
        (self.home.root / "dispatch.lock").write_text("x")
        self.assertEqual(self.run_dispatch()[0], "OK POLL=ok FOUND=0 EMITTED=0 CLAIMED=0 RAN=0")

    # --- 失敗した件の再試行と、人手が要る状態の通知 ---------------------------------------
    def pr_claim(self, age):
        f = self.home.claims / "pending-pr.json"
        f.write_text(json.dumps({"items": [{"pull_requests": [{"url": "x"}]}]}), encoding="utf-8")
        t = self.now - age
        os.utime(f, (t, t))
        return f

    def test_failed_triage_of_a_claim_with_a_pr_url_is_retried_automatically(self):
        # 以前は自動では戻らず、「PRレビューが止まっています」と知らせるだけだった
        f = self.pr_claim(0)
        run_claude.age_claims([f], self.now)  # 仕分けが失敗したときの印（finish_failure と同じ）
        self.assertIn("CLAIMED=0 RAN=0", self.run_dispatch()[0])  # 待ち時間のうちは触らない
        self.now += run_claude.RETRY_AFTER_SEC + 60
        line, _ = self.run_dispatch()
        self.assertIn("CLAIMED=1 RAN=1", line)
        self.assertIn("REQUEUED=1", line)
        self.assertEqual(self.runs, [["pending-pr.json"]])
        self.assertEqual(self.posts, [])  # 人を呼ばない
        self.assertEqual(tick.load_meta(self.home)["pending-pr.json"]["attempts"], 1)

    def test_claim_that_keeps_failing_is_moved_to_failed_and_reported_once(self):
        self.pr_claim(3 * 3600)
        tick.save_meta(self.home, {"pending-pr.json": {"attempts": tick.MAX_ATTEMPTS}})
        line, _ = self.run_dispatch()
        self.assertIn("FAILED=1", line)
        self.assertTrue((self.home.failed / "pending-pr.json").exists())
        self.assertEqual(len(self.posts), 1)
        self.assertIn("pending-pr.json", self.posts[0])
        self.assertIn("failed/", self.posts[0])
        self.run_dispatch()
        self.assertEqual(len(self.posts), 1)

    # --- --home だけ（テスト・診断用） ---------------------------------------------------
    def test_home_only_mode_does_not_poll_and_cannot_start_claude(self):
        self.put()
        args = Namespace(user=None, home=str(self.home.root), config=None, dry_run=False, backfill_hours=0)
        line, code = dispatch.run(args, poster=self.poster, poller=self.poller, now=self.now)
        self.assertEqual((line, code), ("ERROR=need_user_to_run", 2))  # 誰の設定で動かすか分からない
        self.assertEqual(self.polls, [])


class TokenHandling(Base):
    def test_the_bot_token_goes_to_the_poller_only_and_never_into_the_environment(self):
        got = {}

        def fake_poll_once(cfg, w, home, token, **kw):
            got.update(token=token, kw=kw)
            return search.PollResult()

        with mock.patch.object(search, "poll_once", fake_poll_once):
            res = dispatch.poll_discord(self.cfg, self.cfg.watchers[0], self.home, False, 3)
        self.assertTrue(res.ok)
        self.assertEqual(got["token"], TOKEN)
        self.assertEqual(got["kw"], {"backfill_hours": 3, "dry_run": False})
        self.assertNotIn("DISCORD_BOT_TOKEN", os.environ)
        self.assertNotIn(TOKEN, json.dumps(dict(run_claude.child_env(self.home))))

    def test_missing_token_is_a_poll_failure_not_a_crash(self):
        (self.cfg.state_root / ".env").unlink()
        res = dispatch.poll_discord(self.cfg, self.cfg.watchers[0], self.home, False, 0)
        self.assertEqual((res.ok, res.error), (False, "no_token"))


class EndToEnd(Base):
    """検索のヒットから、専用チャンネルへの投稿まで。Discord も Claude も偽物（Discord へは GET だけ、Claude は tests/fake_claude.py）。"""

    def test_a_mention_travels_from_search_to_the_digest(self):
        from tests.fake_discord import ME, sf

        fake = FakeDiscord()
        self.home.write_poll({"since": self.now - 10 * MINUTE})
        mid = fake.add(sf(5 * MINUTE, self.now), "<@%d> SECRET-BODY お願いします" % ME, mentions=[ME])

        def runner(home, watcher, cfg, files, poster=None):
            return run_claude.triage(home, watcher, cfg, files, command=FAKE_CLAUDE, poster=poster)

        with mock.patch("watcher.rest.urllib.request.urlopen", fake):
            line, code = self.run_dispatch(runner=runner, poller=dispatch.poll_discord)
        self.assertEqual((line, code), ("OK POLL=ok FOUND=1 EMITTED=1 CLAIMED=1 RAN=1 RESULT=done", 0))
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.posts[0].startswith("<@42>\n**返信ドラフト**"))
        (done,) = list(self.home.processed.glob("pending-*.json"))
        self.assertEqual(json.loads(done.read_text(encoding="utf-8"))["items"][0]["message_id"], str(mid))
        self.assertEqual(list(self.home.inbox.glob("*")) + list(self.home.claims.glob("*")), [])
        self.assertTrue(all(r.get_method() == "GET" for r in fake.requests))

        with mock.patch("watcher.rest.urllib.request.urlopen", fake):
            self.now += 2 * MINUTE
            line, _ = self.run_dispatch(runner=runner, poller=dispatch.poll_discord)
        self.assertEqual(line, "OK POLL=ok FOUND=0 EMITTED=0 CLAIMED=0 RAN=0")  # 同じ件を、もう一度は出さない
        self.assertEqual(len(self.posts), 1)


class NoConsoleWindow(unittest.TestCase):
    """仕分けのたびに、ターミナルの窓が前面に出ない。

    秘書は、dispatch の中で、そのまま動く（別のプロセスに切り離さない）ので、「コンソールを持たない」プロセスは生まれない。
    claude そのものは、Windows では、窓を作らせない指定で起こす。"""

    DETACHED = 0x00000008  # 「コンソールを持たない」。この指定で起こした先の子に、Windows が見える窓を作る
    NO_WINDOW = 0x08000000

    def test_claude_itself_is_started_without_a_window(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(run_claude.os, "name", "nt"), mock.patch.object(run_claude.subprocess, "Popen") as popen:
                popen.return_value.communicate.return_value = (b"{}", b"")
                popen.return_value.returncode = 0
                run_claude.run_process(["claude"], "x", Path(d) / "run", 5)
        self.assertEqual(popen.call_args.kwargs["creationflags"], self.NO_WINDOW)

    def test_other_systems_pass_no_flags(self):
        with mock.patch.object(run_claude.os, "name", "posix"):
            self.assertEqual(run_claude.no_window_flags(), 0)

    def test_nothing_in_the_pipeline_starts_a_process_without_a_console(self):
        import ast
        bad = []
        for f in sorted((REPO / "pipeline").glob("*.py")) + [REPO / "triage.py"]:
            if not f.exists():
                continue
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr == "DETACHED_PROCESS":
                    bad.append("%s:%d" % (f.name, node.lineno))
                if isinstance(node, ast.Constant) and node.value == "DETACHED_PROCESS":
                    bad.append("%s:%d" % (f.name, node.lineno))
        self.assertEqual(bad, [], "コンソールを持たない指定が使われている（窓が出る原因）: " + ", ".join(bad))

    def test_the_triage_runs_inside_dispatch_not_in_a_detached_process(self):
        self.assertFalse(hasattr(dispatch, "spawn_runner"))
        self.assertIs(dispatch.run.__defaults__[0], run_claude.triage)


class WhileTheResidentWatcherStillRuns(Base):
    """常駐の受付（古い版）がまだ動いている PC で、新しい版のコードが動き始めたとき。

    フォルダだけ更新して、自動起動を登録し直していない状態。古い受付が拾い続けているので、検索は引かない
    （両方が拾うと、同じメンションが 2 通届く）。溜まった分の開封と仕分けは、進める。"""

    def beat(self, age):
        (self.home.root / "heartbeat.json").write_text(json.dumps({"ts": self.now - age, "ready": True}), encoding="utf-8")

    def test_no_search_while_its_heartbeat_is_fresh(self):
        self.beat(20)
        self.put()
        line, code = self.run_dispatch()
        self.assertEqual((line, code), ("OK POLL=resident_watcher CLAIMED=1 RAN=1 RESULT=done", 0))
        self.assertEqual(self.polls, [])
        self.assertEqual(self.runs, [["pending-a.json"]])

    def test_search_takes_over_once_the_old_watcher_has_stopped(self):
        self.beat(dispatch.RESIDENT_FRESH_SEC + 30)
        self.assertEqual(self.run_dispatch()[0], "OK POLL=ok FOUND=0 EMITTED=0 CLAIMED=0 RAN=0")
        self.assertEqual(len(self.polls), 1)

    def test_no_alert_about_discord_while_the_old_watcher_is_receiving(self):
        self.beat(20)
        self.down("http_401_guilds")
        self.run_dispatch()
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
