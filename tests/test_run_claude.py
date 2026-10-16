import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import claude_bin  # noqa: E402
import run_claude  # noqa: E402
import tick  # noqa: E402
from watcher import config  # noqa: E402
from watcher.store import Home  # noqa: E402

FAKE = [sys.executable, str(REPO / "tests" / "fake_claude.py")]
NOW = datetime(2026, 10, 3, 3, 0, tzinfo=timezone(timedelta(hours=9)))


def item(pr=False, blocked=False):
    return {
        "message_id": "1", "kind": "direct", "channel_name": "dev", "jump_url": "https://discord.com/channels/1/2/1",
        "pull_requests": [{"url": "https://github.com/ExampleOrg/a/pull/1"}] if pr else [],
        "pull_requests_blocked": [{"url": "x", "reason": "org_not_allowed"}] if blocked else [],
        "mention": {"author": "alice", "text": "SECRET-BODY please"}, "context_before": [], "context_after": [],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.cfg = config.parse(
            {
                "state_root": str(root),
                "claude_timeout_sec": 30,
                "watchers": [{"name": "me", "user_id": "1", "read_dirs": [str(root / "repo")]}],
            }
        )
        self.watcher = self.cfg.watchers[0]
        self.home = Home(self.watcher.home).ensure()
        (self.home.root / ".env").write_text("DIGEST_WEBHOOK=http://example.invalid/hook\nDIGEST_MENTION_USER_ID=42\n", encoding="utf-8")
        self.posts = []
        self.post_fails = False
        os.environ.pop("FAKE_CLAUDE_MODE", None)

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_MODE", None)
        os.environ.pop("FAKE_CLAUDE_ARGV_OUT", None)
        os.environ.pop("FAKE_CLAUDE_ERROR_TEXT", None)
        os.environ.pop("FAKE_CLAUDE_COUNTS", None)
        os.environ.pop("FAKE_CLAUDE_DIGEST", None)
        self._tmp.cleanup()

    def poster(self, url, content, mention):
        if self.post_fails:
            raise OSError("down")
        self.posts.append((content, mention))
        return 204

    def claim(self, name="pending-a.json", **kw):
        path = self.home.claims / name
        path.write_text(json.dumps({"items": [item(**kw)]}), encoding="utf-8")
        return path

    def triage(self, files, **kw):
        return run_claude.triage(self.home, self.watcher, self.cfg, files, command=FAKE, now=NOW, poster=self.poster, **kw)


class Triage(Base):
    def test_success_posts_then_moves_claims_to_processed(self):
        f = self.claim()
        res = self.triage([f])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["counts"], {"draft": 1, "stamp": 0, "skip": 0, "pr": 0})
        self.assertEqual(res["denials"], 1)
        self.assertEqual(len(self.posts), 1)
        content, mention = self.posts[0]
        self.assertTrue(content.startswith("<@42>\n**Discord メンション対応"))
        self.assertEqual(mention, "42")
        self.assertTrue((self.home.processed / "pending-a.json").exists())
        self.assertFalse(f.exists())
        self.assertEqual(list((self.home.root / "out").glob("*")), [])  # 本文の一時ファイルは消える
        self.assertEqual(len(list(self.home.drafts.glob("*.md"))), 1)  # 控えは残る

    def test_run_record_contains_no_message_bodies(self):
        self.triage([self.claim()])
        recs = list((self.home.root / "runs").glob("*.json"))
        self.assertEqual(len(recs), 1)
        text = recs[0].read_text(encoding="utf-8")
        self.assertNotIn("SECRET-BODY", text)
        self.assertEqual(json.loads(text)["counts"]["draft"], 1)

    def test_claude_failure_ages_the_claim_and_alerts_once(self):
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        f = self.claim()
        res = self.triage([f])
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "claude_exit_1")
        self.assertTrue(f.exists())
        self.assertFalse((self.home.processed / "pending-a.json").exists())
        # 数分後に tick の掃除が inbox へ戻す程度に古くしてある（まだ45分は経っていないので戻さない）
        age = time.time() - f.stat().st_mtime
        self.assertGreater(age, tick.TRIAGE_TIMEOUT_SEC - run_claude.RETRY_AFTER_SEC - 5)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("claude_exit_1", self.posts[0][0])
        self.triage([f])  # 2回目の失敗では連投しない
        self.assertEqual(len(self.posts), 1)

    def test_session_id_is_recorded_only_when_transcripts_are_kept(self):
        self.triage([self.claim()])
        rec = json.loads(next((self.home.root / "runs").glob("*.json")).read_text(encoding="utf-8"))
        self.assertNotIn("session_id", rec)  # 既定は記録を残さないので、開ける先も無い
        keep = config.parse({"state_root": self.cfg.state_root.as_posix(), "keep_transcripts": True,
                             "watchers": [{"name": "me", "user_id": "1"}]})
        res = run_claude.triage(self.home, keep.watchers[0], keep, [self.claim("pending-k.json")],
                                command=FAKE, now=NOW, poster=self.poster)
        self.assertEqual(res["session_id"], "sess-fake")

    def test_failed_claim_is_requeued_by_tick_after_the_wait(self):
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        f = self.claim()
        meta = {"pending-a.json": {"attempts": 1}}
        tick.save_meta(self.home, meta)
        self.triage([f])
        later = time.time() + run_claude.RETRY_AFTER_SEC + 60
        requeued, failed = tick.sweep(self.home, later, meta, dry_run=False)
        self.assertEqual((requeued, failed), (["pending-a.json"], []))

    def test_failed_claim_with_a_pr_url_is_requeued_the_same_way(self):
        # PR の URL を含む件だけ、失敗しても自動では戻らなかった
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        f = self.claim(pr=True)
        meta = {"pending-a.json": {"attempts": 1}}
        self.triage([f])
        later = time.time() + run_claude.RETRY_AFTER_SEC + 60
        self.assertEqual(tick.sweep(self.home, later, meta, dry_run=False), (["pending-a.json"], []))

    def test_missing_digest_is_a_failure(self):
        os.environ["FAKE_CLAUDE_MODE"] = "nodigest"
        res = self.triage([self.claim()])
        self.assertEqual(res["reason"], "no_digest")
        self.assertEqual(self.posts[0][0].count("no_digest"), 1)

    def test_is_error_json_is_a_failure(self):
        os.environ["FAKE_CLAUDE_MODE"] = "iserror"
        self.assertEqual(self.triage([self.claim()])["reason"], "claude_exit_0")

    def test_timeout_kills_the_process(self):
        os.environ["FAKE_CLAUDE_MODE"] = "hang"
        cfg = config.parse({"state_root": self.cfg.state_root.as_posix(), "claude_timeout_sec": 2,
                            "watchers": [{"name": "me", "user_id": "1"}]})
        t0 = time.time()
        res = run_claude.triage(self.home, cfg.watchers[0], cfg, [self.claim()], command=FAKE, now=NOW, poster=self.poster)
        self.assertEqual(res["reason"], "timeout")
        self.assertLess(time.time() - t0, 30)

    def test_post_failure_keeps_digest_and_retry_skips_claude(self):
        f = self.claim()
        self.post_fails = True
        res = self.triage([f])
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "post_failed")
        self.assertTrue(f.exists())
        self.assertEqual(len(list((self.home.root / "out").glob("*.digest.txt"))), 1)
        # 再試行: Claude は動かさず（動かしたら失敗する設定）、投稿だけやり直して成功する
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        self.post_fails = False
        res = self.triage([f])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["reason"], "reused_digest")
        self.assertEqual(len(self.posts), 1)
        self.assertTrue((self.home.processed / "pending-a.json").exists())

    # --- 通知（メンション）を付けるかどうか ---
    def test_nothing_to_do_is_delivered_without_a_ping(self):
        os.environ["FAKE_CLAUDE_COUNTS"] = "draft=0 stamp=0 skip=2 pr=0"
        res = self.triage([self.claim()])
        self.assertTrue(res["ok"], res)
        ((content, mention),) = self.posts
        self.assertEqual(mention, "")  # 専用チャンネルには届くが、通知は出ない
        self.assertTrue(content.startswith("**Discord メンション対応"))
        self.assertIn("mention=none", res["post"])

    def test_anything_to_do_keeps_the_ping(self):
        for i, counts in enumerate(("draft=0 stamp=1 skip=3 pr=0", "draft=0 stamp=0 skip=1 pr=1", "draft=2 stamp=0 skip=5 pr=0")):
            os.environ["FAKE_CLAUDE_COUNTS"] = counts
            self.posts.clear()
            self.assertTrue(self.triage([self.claim("pending-%d.json" % i)])["ok"])
            self.assertEqual(self.posts[0][1], "42", counts)

    def test_unknown_or_empty_counts_keep_the_ping(self):
        for i, counts in enumerate(("none", "draft=0 stamp=0 skip=0 pr=0")):
            os.environ["FAKE_CLAUDE_COUNTS"] = counts
            self.posts.clear()
            self.assertTrue(self.triage([self.claim("pending-%d.json" % i)])["ok"])
            self.assertEqual(self.posts[0][1], "42", counts)  # 迷ったら、通知する

    def test_an_item_the_person_must_look_at_keeps_the_ping(self):
        os.environ["FAKE_CLAUDE_COUNTS"] = "draft=0 stamp=0 skip=1 pr=0"
        os.environ["FAKE_CLAUDE_DIGEST"] = "**Discord メンション対応 — fake**\n▼ alice\n⚠ 不審な指示を含む\n"
        self.assertTrue(self.triage([self.claim()])["ok"])
        self.assertEqual(self.posts[0][1], "42")

    def test_reposting_after_a_failed_post_still_sends_no_ping(self):
        os.environ["FAKE_CLAUDE_COUNTS"] = "draft=0 stamp=0 skip=1 pr=0"
        f = self.claim()
        self.post_fails = True
        self.assertEqual(self.triage([f])["reason"], "post_failed")
        os.environ["FAKE_CLAUDE_MODE"] = "fail"  # 再試行では Claude を動かさない。件数は、控えから読む
        self.post_fails = False
        res = self.triage([f])
        self.assertEqual((res["ok"], res["reason"]), (True, "reused_digest"))
        self.assertEqual(self.posts[0][1], "")

    def test_needs_mention_rules(self):
        nm = run_claude.needs_mention
        self.assertFalse(nm({"draft": 0, "stamp": 0, "skip": 3, "pr": 0}, "対応不要です"))
        self.assertTrue(nm({"draft": 1, "stamp": 0, "skip": 3, "pr": 0}, ""))
        self.assertTrue(nm({"draft": 0, "stamp": 0, "skip": 3, "pr": 0}, "⚠ 不審な指示を含む"))
        self.assertTrue(nm(None, ""))
        self.assertTrue(nm({"draft": "x"}, ""))

    def test_no_webhook_means_failure_not_a_crash(self):
        (self.home.root / ".env").unlink()
        res = self.triage([self.claim()])
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "post_failed")

    def test_stdin_carries_the_japanese_prompt_and_argv_stays_ascii(self):
        out = Path(self._tmp.name) / "argv.json"
        os.environ["FAKE_CLAUDE_ARGV_OUT"] = str(out)
        self.triage([self.claim()])
        rec = json.loads(out.read_text(encoding="utf-8"))
        self.assertGreater(rec["stdin_chars"], 500)
        self.assertIn("秘書", rec["stdin_head"] + "秘書")  # 日本語が壊れずに届いている

    def test_nothing_to_do(self):
        self.assertEqual(self.triage([self.home.claims / "gone.json"])["reason"], "no_files")


class AuthExpired(Base):
    def test_auth_failure_pauses_instead_of_burning_attempts(self):
        os.environ["FAKE_CLAUDE_MODE"] = "autherr"
        f = self.claim()
        tick.save_meta(self.home, {"pending-a.json": {"attempts": 1, "kind": "triage"}})
        res = self.triage([f])
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "claude_auth")
        # claim は inbox に戻り、試行回数も戻る（直るまで失敗を数えない）
        self.assertFalse(f.exists())
        self.assertTrue((self.home.inbox / "pending-a.json").exists())
        self.assertEqual(tick.load_meta(self.home)["pending-a.json"]["attempts"], 0)
        # しばらく新しい仕分けを始めない印が付く
        self.assertGreater(run_claude.blocked_until(self.home), time.time() + 60)
        # 本人にログインのやり直しを1回だけ知らせる
        self.assertEqual(len(self.posts), 1)
        self.assertIn("setup-token", self.posts[0][0])
        self.assertNotIn("SECRET-BODY", self.posts[0][0])
        self.triage([self.claim("pending-b.json")])
        self.assertEqual(len(self.posts), 1)

    def test_recovery_clears_the_block(self):
        os.environ["FAKE_CLAUDE_MODE"] = "autherr"
        self.triage([self.claim()])
        self.assertGreater(run_claude.blocked_until(self.home), 0)
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        res = self.triage([self.claim("pending-c.json")])
        self.assertTrue(res["ok"], res)
        self.assertEqual(run_claude.blocked_until(self.home), 0.0)

    def test_oauth_token_reaches_claude_but_the_discord_bot_token_does_not(self):
        (self.home.root / ".env").write_text(
            "DIGEST_WEBHOOK=http://example.invalid/hook\nDIGEST_MENTION_USER_ID=42\nCLAUDE_CODE_OAUTH_TOKEN=tok-claude\n",
            encoding="utf-8",
        )
        out = Path(self._tmp.name) / "env.json"
        os.environ["FAKE_CLAUDE_ARGV_OUT"] = str(out)
        os.environ["DISCORD_BOT_TOKEN"] = "must-not-leak"
        try:
            self.triage([self.claim()])
        finally:
            os.environ.pop("DISCORD_BOT_TOKEN", None)
        rec = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(rec["oauth_token"], "tok-claude")
        self.assertFalse(rec["discord_token_present"])

    def test_oauth_token_on_the_first_line_of_a_bom_file_still_reaches_claude(self):
        # メモ帳が BOM つきで保存した .env。先頭行のトークンが読めないと、黙って `claude` のログインで動いてしまう
        (self.home.root / ".env").write_bytes(
            b"\xef\xbb\xbfCLAUDE_CODE_OAUTH_TOKEN=tok-first-line\r\nDIGEST_WEBHOOK=http://example.invalid/hook\r\n")
        old = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        try:
            env = run_claude.child_env(self.home)
        finally:
            if old is not None:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = old
        self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN"), "tok-first-line")
        self.assertEqual(run_claude.auth_source(env), "token")

    def test_api_key_in_the_environment_never_reaches_claude(self):
        # -p では ANTHROPIC_API_KEY があると必ずそちらが使われ、サブスクではなく API 課金になってしまう
        out = Path(self._tmp.name) / "env2.json"
        os.environ["FAKE_CLAUDE_ARGV_OUT"] = str(out)
        os.environ["ANTHROPIC_API_KEY"] = "sk-should-not-pass"
        try:
            self.triage([self.claim()])
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertFalse(json.loads(out.read_text(encoding="utf-8"))["api_key_present"])

    def test_host_session_variables_are_not_inherited(self):
        # デスクトップアプリ配下の端末から手で動かしても、本番（タスクスケジューラ）と同じ環境で動くように
        out = Path(self._tmp.name) / "env3.json"
        os.environ["FAKE_CLAUDE_ARGV_OUT"] = str(out)
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = "abc"
        os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:1"
        try:
            self.triage([self.claim()])
        finally:
            os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["inherited_claude_vars"], [])

    def test_the_run_record_never_contains_a_token(self):
        (self.home.root / ".env").write_text(
            "DIGEST_WEBHOOK=http://example.invalid/hook\nCLAUDE_CODE_OAUTH_TOKEN=tok-claude\n", encoding="utf-8")
        self.triage([self.claim()])
        for f in (self.home.root / "runs").glob("*.json"):
            self.assertNotIn("tok-claude", f.read_text(encoding="utf-8"))


class UsageLimit(Base):
    def test_limit_pauses_longer_than_auth_and_does_not_burn_attempts(self):
        os.environ["FAKE_CLAUDE_MODE"] = "limit"
        f = self.claim()
        tick.save_meta(self.home, {"pending-a.json": {"attempts": 1, "kind": "triage"}})
        res = self.triage([f])
        self.assertEqual(res["reason"], "claude_limit")
        self.assertTrue((self.home.inbox / "pending-a.json").exists())
        self.assertEqual(tick.load_meta(self.home)["pending-a.json"]["attempts"], 0)
        until, reason = run_claude.blocked_state(self.home)
        self.assertEqual(reason, "claude_limit")
        self.assertGreater(until, time.time() + 25 * 60)  # 認証切れ（10分）より長く待つ
        self.assertEqual(len(self.posts), 1)
        self.assertIn("利用枠", self.posts[0][0])
        self.triage([self.claim("pending-b.json")])
        self.assertEqual(len(self.posts), 1)  # 通知は連投しない

    def test_limit_then_recovery_clears_the_pause(self):
        os.environ["FAKE_CLAUDE_MODE"] = "limit"
        self.triage([self.claim()])
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        self.assertTrue(self.triage([self.claim("pending-c.json")])["ok"])
        self.assertEqual(run_claude.blocked_state(self.home), (0.0, ""))


class NeverRan(Base):
    """モデルが一度も動かずにエラーで終わった回（費用 0）は、文言によらず、失敗として数えずに待つ。"""

    def record(self, res):
        path = sorted((self.home.root / "runs").glob("*-%s.json" % res["run_id"]))[-1]
        return json.loads(path.read_text(encoding="utf-8"))

    def test_an_unknown_zero_cost_error_pauses_instead_of_burning_attempts(self):
        os.environ["FAKE_CLAUDE_MODE"] = "notstarted"
        f = self.claim()
        tick.save_meta(self.home, {"pending-a.json": {"attempts": 1, "kind": "triage"}})
        res = self.triage([f])
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "claude_not_started")
        self.assertTrue((self.home.inbox / "pending-a.json").exists())
        self.assertEqual(tick.load_meta(self.home)["pending-a.json"]["attempts"], 0)
        until, reason = run_claude.blocked_state(self.home)
        self.assertEqual(reason, "claude_not_started")
        self.assertGreater(until, time.time() + 60)
        # 通知に、何が起きたか（Claude Code のエラー文と、API のエラーの種類）がそのまま出る
        self.assertEqual(len(self.posts), 1)
        self.assertIn("disabled Claude subscription access", self.posts[0][0])
        self.assertIn("403 oauth_not_allowed_for_organization", self.posts[0][0])
        self.assertNotIn("SECRET-BODY", self.posts[0][0])
        self.triage([self.claim("pending-b.json")])
        self.assertEqual(len(self.posts), 1)  # 通知は連投しない

    def test_it_never_reaches_failed_however_long_it_lasts(self):
        # 2026-10-04 の実例: 同じエラーが続き、3回で failed/ に退避された。直ったあとも、自動では処理されなかった
        os.environ["FAKE_CLAUDE_MODE"] = "notstarted"
        (self.home.inbox / "pending-a.json").write_text(json.dumps({"items": [item()]}), encoding="utf-8")
        for _ in range(tick.MAX_ATTEMPTS + 2):
            got = tick.collect(self.home, time.time(), dry_run=False)
            self.assertEqual([c.name for c in got["claimed"]], ["pending-a.json"])
            self.triage(got["claimed"])
        self.assertEqual(list(self.home.failed.glob("*")), [])
        self.assertTrue((self.home.inbox / "pending-a.json").exists())
        os.environ["FAKE_CLAUDE_MODE"] = "ok"  # 直れば、溜めてあった分がそのまま処理される
        res = self.triage(tick.collect(self.home, time.time(), dry_run=False)["claimed"])
        self.assertTrue(res["ok"], res)
        self.assertTrue((self.home.processed / "pending-a.json").exists())
        self.assertEqual(run_claude.blocked_state(self.home), (0.0, ""))

    def test_the_error_text_is_recorded_only_when_the_model_never_ran(self):
        os.environ["FAKE_CLAUDE_MODE"] = "notstarted"
        rec = self.record(self.triage([self.claim()]))
        self.assertTrue(rec["cli_error"].startswith("Your organization has disabled"))
        self.assertEqual(rec["api_error"], "403 oauth_not_allowed_for_organization")
        self.assertNotIn("SECRET-BODY", json.dumps(rec, ensure_ascii=False))
        # モデルが動いた回の結果には、メンションの本文が混ざりうる。記録にも通知にも載せず、これまでどおり失敗として数える
        os.environ["FAKE_CLAUDE_MODE"] = "iserror"
        res = self.triage([self.claim("pending-b.json")])
        self.assertEqual(res["reason"], "claude_exit_0")
        self.assertNotIn("cli_error", self.record(res))
        self.assertTrue((self.home.claims / "pending-b.json").exists())

    def test_the_wording_only_picks_the_notice(self):
        os.environ["FAKE_CLAUDE_MODE"] = "notstarted"
        os.environ["FAKE_CLAUDE_ERROR_TEXT"] = "Claude usage limit reached. Your limit will reset at 3pm."
        res = self.triage([self.claim()])
        self.assertEqual(res["reason"], "claude_limit")
        self.assertGreater(run_claude.blocked_state(self.home)[0], time.time() + 25 * 60)
        self.assertIn("利用枠", self.posts[0][0])
        self.assertIn("reset at 3pm", self.posts[0][0])

    def test_the_notice_suggests_a_pinned_token_only_when_none_is_set(self):
        # 専用トークンが無いと、PC に保存された `claude` のログインで動く。アカウントを切り替えると止まる（2026-10-04 の実例）
        os.environ["FAKE_CLAUDE_MODE"] = "notstarted"
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            res = self.triage([self.claim()])
            self.assertEqual(res["auth"], "cli_login")
            self.assertIn("setup-token", self.posts[0][0])
            (self.home.root / ".env").write_text(
                "DIGEST_WEBHOOK=http://example.invalid/hook\nCLAUDE_CODE_OAUTH_TOKEN=tok-claude\n", encoding="utf-8")
            self.home.clear_alert("claude_not_started")
            res = self.triage([self.claim("pending-b.json")])
            self.assertEqual(res["auth"], "token")
            self.assertNotIn("setup-token", self.posts[1][0])
            self.assertNotIn("tok-claude", self.posts[1][0])
            self.assertEqual(self.record(res)["auth"], "token")  # どの資格情報で動いたかは残す。値は残さない

    def test_never_ran_needs_an_error_and_a_reported_cost_of_zero(self):
        self.assertTrue(run_claude.never_ran({"is_error": True, "cost": 0}))
        self.assertFalse(run_claude.never_ran({"is_error": True, "cost": 0.0123}))  # モデルが動いた
        self.assertFalse(run_claude.never_ran({"is_error": True, "cost": None}))  # 費用が分からない回は、動いたかもしれない
        self.assertFalse(run_claude.never_ran({"is_error": False, "cost": 0}))
        self.assertFalse(run_claude.never_ran(run_claude.parse_result("not json")))  # JSON を返さずに落ちた回も同じ

    def test_the_error_text_is_one_short_line_without_keys(self):
        text = run_claude.clip_error("Invalid key sk-ant-abc123_DEF-456 in\n  `settings`  " + "x" * 500)
        self.assertNotIn("sk-ant-abc123", text)
        self.assertNotIn("\n", text)
        self.assertNotIn("`", text)  # 通知では ` で囲むので、途中で閉じさせない
        self.assertTrue(text.startswith("Invalid key <hidden> in 'settings' x"))
        self.assertEqual(len(text), run_claude.CLI_ERROR_MAX)


class Command(Base):
    def cmd(self, **cfg_kw):
        cfg = config.parse(dict({"state_root": self.cfg.state_root.as_posix(),
                                 "watchers": [{"name": "me", "user_id": "1", "read_dirs": [str(Path(self._tmp.name) / "repo")]}]}, **cfg_kw))
        return run_claude.build_command("CLAUDE", cfg, cfg.watchers[0], self.home)

    def test_permissions_are_locked_down(self):
        c = self.cmd()
        self.assertNotIn("--bare", c)  # --bare はサブスクのログインを使わない
        self.assertEqual(c[c.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(c[c.index("--permission-prompts") + 1], "none")
        # 取り込む設定のときだけ、自動モード（ADR-0033）。それ以外の絞り方は同じ
        imp = self.cmd(keep_transcripts=True, desktop_import=True)
        self.assertEqual(imp[imp.index("--permission-mode") + 1], "auto")
        self.assertEqual(self.cmd(keep_transcripts=True)[imp.index("--permission-mode") + 1], "dontAsk")
        for flag in ("--permission-prompts", "--tools", "--allowedTools", "--disallowedTools", "--setting-sources"):
            self.assertEqual(imp[imp.index(flag) + 1], c[c.index(flag) + 1], flag)
        self.assertEqual(imp[imp.index("--allowedTools"):imp.index("--add-dir")], c[c.index("--allowedTools"):c.index("--add-dir")])
        self.assertIn("--strict-mcp-config", imp)
        self.assertIn("--disable-slash-commands", imp)
        tools = c[c.index("--tools") + 1]
        self.assertEqual(set(tools.split(",")), {"Read", "Grep", "Glob", "Edit", "Write"})
        for banned in ("Bash", "PowerShell", "WebFetch", "WebSearch", "Agent", "Skill"):
            self.assertNotIn(banned, tools)
        self.assertIn("--strict-mcp-config", c)
        self.assertIn("--disable-slash-commands", c)

    def test_can_only_write_under_drafts_and_out(self):
        c = self.cmd()
        allowed = c[c.index("--allowedTools") + 1: c.index("--disallowedTools")]
        self.assertEqual(len(allowed), 2)
        self.assertTrue(all(a.startswith("Edit(") and a.endswith("/**)") for a in allowed))
        self.assertTrue(any("/drafts/" in a or a.endswith("/drafts/**)") for a in allowed))
        self.assertTrue(any(a.endswith("/out/**)") for a in allowed))

    def test_env_files_are_denied_and_read_dirs_are_added(self):
        c = self.cmd()
        denied = c[c.index("--disallowedTools") + 1: c.index("--add-dir")]
        self.assertIn("Read(//**/.env)", denied)
        dirs = [c[i + 1] for i, x in enumerate(c) if x == "--add-dir"]
        self.assertEqual(dirs[0], str(self.home.claims))
        self.assertTrue(dirs[1].endswith("repo"))

    def test_transcripts_are_not_kept_unless_asked(self):
        self.assertIn("--no-session-persistence", self.cmd())
        self.assertNotIn("--no-session-persistence", self.cmd(keep_transcripts=True))

    def test_the_instruction_is_ascii_and_the_task_goes_through_stdin(self):
        c = self.cmd()
        self.assertEqual(c[1:3], ["-p", "Follow the task written on stdin."])

    def test_rule_path_windows_form(self):
        if os.name != "nt":
            self.skipTest("Windows のパス表記の検査")
        self.assertEqual(run_claude.rule_path(Path("C:/Users/x/y")), "//c/Users/x/y")


class DesktopImport(Base):
    """記録したセッションを、仕分けのたびにデスクトップ版の左側へ取り込む（失敗しても仕分けは成功のまま）。"""

    def keep_cfg(self, **kw):
        data = {"state_root": self.cfg.state_root.as_posix(), "keep_transcripts": True, "desktop_import": True,
                "watchers": [{"name": "me", "user_id": "1"}]}
        data.update(kw)
        return config.parse(data)

    def run_triage(self, cfg, import_fn, name="pending-d.json"):
        return run_claude.triage(self.home, cfg.watchers[0], cfg, [self.claim(name)], command=FAKE, now=NOW,
                                 poster=self.poster, import_fn=import_fn)

    def test_imports_the_session_after_the_digest_is_posted(self):
        calls = []

        def fake_import(claude, session_id, home, env=None):
            calls.append((session_id, len(self.posts)))  # 呼ばれた時点で、投稿は済んでいる
            return "ok"

        res = self.run_triage(self.keep_cfg(), fake_import)
        self.assertTrue(res["ok"], res)
        self.assertEqual(calls, [("sess-fake", 1)])
        self.assertEqual(res["desktop_import"], "ok")
        rec = json.loads(next((self.home.root / "runs").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((rec["session_id"], rec["desktop_import"]), ("sess-fake", "ok"))

    def test_a_failed_import_does_not_fail_the_triage(self):
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: "exit_1")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["desktop_import"], "exit_1")
        self.assertTrue((self.home.processed / "pending-d.json").exists())  # claim は処理済みへ

    def test_an_exception_in_the_import_does_not_fail_the_triage(self):
        def boom(*a, **k):
            raise RuntimeError("desktop gone")

        res = self.run_triage(self.keep_cfg(), boom)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["desktop_import"], "error")
        self.assertTrue((self.home.processed / "pending-d.json").exists())

    def test_no_import_unless_enabled_and_a_session_was_kept(self):
        calls = []

        def fake_import(*a, **k):
            calls.append(a)
            return "ok"

        for kw in ({"desktop_import": False}, {"keep_transcripts": False}):
            res = self.run_triage(self.keep_cfg(**kw), fake_import, name="pending-%s.json" % next(iter(kw)))
            self.assertTrue(res["ok"], res)
            self.assertNotIn("desktop_import", res)
        self.assertEqual(calls, [])

    def test_a_failed_post_is_retried_without_rerunning_claude_and_still_imports(self):
        calls = []
        cfg = self.keep_cfg()
        self.post_fails = True
        f = self.claim("pending-r.json")
        res = run_claude.triage(self.home, cfg.watchers[0], cfg, [f], command=FAKE, now=NOW, poster=self.poster,
                                import_fn=lambda *a, **k: calls.append(a) or "ok")
        self.assertEqual(res["reason"], "post_failed")
        self.assertEqual(calls, [])  # 投稿に失敗した回は取り込まない
        self.post_fails = False
        os.environ["FAKE_CLAUDE_MODE"] = "fail"  # もう一度 Claude を動かすなら失敗する。動かさないので成功する
        res = run_claude.triage(self.home, cfg.watchers[0], cfg, [f], command=FAKE, now=NOW, poster=self.poster,
                                import_fn=lambda *a, **k: calls.append(a) or "ok")
        self.assertEqual(res["reason"], "reused_digest")
        self.assertEqual([c[1] for c in calls], ["sess-fake"])  # 記録しておいた session_id で取り込む

    def test_import_command_and_results(self):
        self.assertEqual(run_claude.desktop_import_command("CLAUDE", "abc"), ["CLAUDE", "--desktop", "--resume", "abc"])
        ok = mock.Mock(returncode=0)
        with mock.patch.object(run_claude.subprocess, "run", return_value=ok) as run:
            self.assertEqual(run_claude.import_to_desktop("CLAUDE", "abc", self.home, env={"X": "1"}), "ok")
        self.assertEqual(run.call_args.args[0], ["CLAUDE", "--desktop", "--resume", "abc"])
        kw = run.call_args.kwargs
        self.assertEqual(kw["env"], {"X": "1"})
        # --desktop は出力がリダイレクトされていると動かない。入出力は渡さず（パイプ・NUL・ファイルにしない）、
        # Windows では非表示の本物のコンソールを新しく作って動かす
        for name in ("stdin", "stdout", "stderr", "capture_output"):
            self.assertNotIn(name, kw)
        if os.name == "nt":
            self.assertTrue(kw["creationflags"] & subprocess.CREATE_NEW_CONSOLE)
            self.assertEqual(kw["startupinfo"].wShowWindow, 0)  # SW_HIDE
            self.assertTrue(kw["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        with mock.patch.object(run_claude.subprocess, "run", return_value=mock.Mock(returncode=3)):
            self.assertEqual(run_claude.import_to_desktop("CLAUDE", "abc", self.home), "exit_3")
        with mock.patch.object(run_claude.subprocess, "run", side_effect=subprocess.TimeoutExpired("c", 1)):
            self.assertEqual(run_claude.import_to_desktop("CLAUDE", "abc", self.home), "timeout")
        with mock.patch.object(run_claude.subprocess, "run", side_effect=OSError("no exe")):
            self.assertEqual(run_claude.import_to_desktop("CLAUDE", "abc", self.home), "error")
        self.assertEqual(run_claude.import_to_desktop("", "abc", self.home), "claude_not_found")

    def test_session_name_is_added_only_when_given(self):
        cfg = self.keep_cfg()
        plain = run_claude.build_command("CLAUDE", cfg, cfg.watchers[0], self.home)
        self.assertNotIn("--name", plain)
        named = run_claude.build_command("CLAUDE", cfg, cfg.watchers[0], self.home, session_name="alice「予定の件」#dev")
        self.assertEqual(named[named.index("--name") + 1], "alice「予定の件」#dev")
        self.assertNotIn("--no-session-persistence", named)  # 記録を残す設定なので、保存される

    def test_a_name_with_other_characters_is_not_passed_at_all(self):
        # 名前には、誰でも書ける文字列が入る。決めた文字だけでなければ、引数に載せない（claude.cmd は cmd.exe を通る）
        cfg = self.keep_cfg()
        for bad in ('a" & calc & "', "%USERNAME%", "--bare", "a\nb"):
            cmd = run_claude.build_command("CLAUDE", cfg, cfg.watchers[0], self.home, session_name=bad)
            self.assertNotIn("--name", cmd)
            self.assertNotIn(bad, cmd)

    def test_the_name_is_made_from_the_claimed_items(self):
        f = self.claim()
        self.assertEqual(run_claude.session_name_for([f]), "alice「SECRET-BODY please」#dev")
        self.assertEqual(run_claude.session_name_for([f, self.claim("pending-b.json")]), "alice「SECRET-BODY please」#dev ほか1件")
        broken = self.home.claims / "pending-x.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(run_claude.session_name_for([broken]), "メンション")  # 読めなくても、仕分けは止めない
        with mock.patch.object(run_claude.session_naming, "build", side_effect=RuntimeError("boom")):
            self.assertEqual(run_claude.session_name_for([f]), "メンション")

    def launcher(self):
        """本物と同じ形で起動される偽の claude。Windows では、npm 版と同じ .cmd（引数が cmd.exe を通る）。"""
        root = Path(self._tmp.name)
        fake = REPO / "tests" / "fake_claude.py"
        if os.name == "nt":
            path = root / "claude.cmd"
            path.write_bytes(b'@ECHO off\r\nSETLOCAL\r\n"%FAKE_PY%" "%FAKE_SCRIPT%" %*\r\n')
        else:
            path = root / "claude"
            path.write_text('#!/bin/sh\nexec "$FAKE_PY" "$FAKE_SCRIPT" "$@"\n', encoding="utf-8")
            path.chmod(0o755)
        return str(path), {"FAKE_PY": sys.executable, "FAKE_SCRIPT": str(fake)}

    def test_triage_names_the_session_after_the_mention_and_keeps_it_out_of_the_run_record(self):
        claude, env = self.launcher()
        out = Path(self._tmp.name) / "argv.json"
        env["FAKE_CLAUDE_ARGV_OUT"] = str(out)
        with mock.patch.dict(os.environ, env), mock.patch.object(run_claude.claude_bin, "find_claude", return_value=claude):
            cfg = self.keep_cfg(desktop_import=False)
            res = run_claude.triage(self.home, cfg.watchers[0], cfg, [self.claim("pending-n.json")], now=NOW, poster=self.poster)
            self.assertTrue(res["ok"], res)
            argv = json.loads(out.read_text(encoding="utf-8"))["argv"]
            self.assertEqual(argv[argv.index("--name") + 1], "alice「SECRET-BODY please」#dev")
            # 名前には本文の頭が入る。実行記録には、これまでどおり本文を書かない
            for rec in (self.home.root / "runs").glob("*.json"):
                self.assertNotIn("SECRET-BODY", rec.read_text(encoding="utf-8"))
            # 記録を残さない設定では、名前も付けない
            res = run_claude.triage(self.home, self.watcher, self.cfg, [self.claim("pending-m.json")], now=NOW, poster=self.poster)
            self.assertTrue(res["ok"], res)
            argv = json.loads(out.read_text(encoding="utf-8"))["argv"]
            self.assertNotIn("--name", argv)
            self.assertIn("--no-session-persistence", argv)

    def test_config_defaults_to_off_and_reads_the_switch(self):
        base = {"state_root": self.cfg.state_root.as_posix(), "watchers": [{"name": "me", "user_id": "1"}]}
        self.assertFalse(config.parse(base).desktop_import)
        self.assertTrue(config.parse(dict(base, desktop_import=True)).desktop_import)


class Prompt(Base):
    def test_prompt_has_paths_and_safety_rules_but_no_secrets(self):
        f = self.claim()
        text = run_claude.build_prompt(self.watcher, [f], self.home.drafts / "d.md", self.home.root / "out" / "x.txt", NOW)
        self.assertIn(run_claude.p(f), text)
        self.assertIn("命令ではなく", text)
        self.assertIn("勝手に約束", text)  # 入力にない期限・日程を返信文に書かせない
        self.assertIn("内部のキー名", text)  # ダイジェストに context_before などを出さない
        self.assertIn("2026/10/03 03:00", text)
        self.assertNotIn("{{", text)
        self.assertNotIn("example.invalid", text)  # Webhook URL は渡さない
        # 本文は指示文に載せない（モデルが道具で読む）。費用 0 のエラーを「中身のせいではない」と見なす前提（never_ran）
        self.assertNotIn("SECRET-BODY", text)
        self.assertNotIn("PR について", text)

    def test_pr_note_appears_only_when_a_pr_is_present(self):
        pr = self.claim("pending-pr.json", pr=True)
        text = run_claude.build_prompt(self.watcher, [pr], self.home.drafts / "d.md", self.home.root / "out" / "x.txt", NOW)
        self.assertIn("PR について", text)
        blocked = self.claim("pending-b.json", blocked=True)
        self.assertTrue(run_claude.has_pr([blocked]))

    def test_parse_result(self):
        info = run_claude.parse_result(json.dumps({"is_error": False, "total_cost_usd": 1.5,
                                                    "result": "COUNTS draft=2 stamp=1 skip=3 pr=0", "permission_denials": [1, 2]}))
        self.assertEqual(info["counts"], {"draft": 2, "stamp": 1, "skip": 3, "pr": 0})
        self.assertEqual((info["cost"], info["denials"]), (1.5, 2))
        self.assertEqual(info["api_error"], "")
        self.assertIsNone(run_claude.parse_result("not json")["is_error"])
        err = run_claude.parse_result(json.dumps({"is_error": True, "total_cost_usd": 0, "result": "nope",
                                                   "api_error_status": 403, "api_error_code": "oauth_not_allowed_for_organization"}))
        self.assertEqual((err["cost"], err["api_error"]), (0, "403 oauth_not_allowed_for_organization"))


class ClaudeBin(unittest.TestCase):
    def test_priority_and_version_sorting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v in ("2.1.9", "2.1.284", "2.1.281"):
                exe = root / "Claude" / "claude-code" / v / "claude.exe"
                exe.parent.mkdir(parents=True)
                exe.write_text("x")
            nohome = str(root / "nohome")  # 実機の ~/.local/bin を拾わないよう、空のホームを指す
            found = claude_bin.find_claude("", appdata=str(root), env={"PATH": ""}, home=nohome)
            self.assertTrue(found.replace("\\", "/").endswith("2.1.284/claude.exe"))  # 文字列順ではなく版の数値順
            explicit = root / "mine.exe"
            explicit.write_text("x")
            self.assertEqual(claude_bin.find_claude(str(explicit), appdata=str(root), env={"PATH": ""}, home=nohome), str(explicit))
            self.assertEqual(claude_bin.find_claude("", appdata=str(root), env={"PATH": "", "CLAUDE_BIN": str(explicit)}, home=nohome), str(explicit))

    def test_native_install_location_beats_the_desktop_bundle(self):
        # 公式インストーラーは PATH を更新しないことがある。置き場を直接見て、同梱版より優先する
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            native = root / "home" / ".local" / "bin" / "claude.exe"
            native.parent.mkdir(parents=True)
            native.write_text("x")
            bundled = root / "Claude" / "claude-code" / "2.1.284" / "claude.exe"
            bundled.parent.mkdir(parents=True)
            bundled.write_text("x")
            found = claude_bin.find_claude("", appdata=str(root), env={"PATH": ""}, home=str(root / "home"))
            self.assertEqual(found, str(native))
            # PATH にあれば、それがさらに優先される
            self.assertNotEqual(claude_bin.find_claude("", appdata=str(root), env={"PATH": str(native.parent)}, home=str(root / "home")), None)

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(claude_bin.find_claude("", appdata=d, env={"PATH": ""}, home=d))


if __name__ == "__main__":
    unittest.main()
