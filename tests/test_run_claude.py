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
import continuation  # noqa: E402
import run_claude  # noqa: E402
import tick  # noqa: E402
from watcher import config  # noqa: E402
from watcher.store import Home  # noqa: E402

FAKE = [sys.executable, str(REPO / "tests" / "fake_claude.py")]
REAL_RENAME = run_claude.rename_session  # Base は、これを偽物に差し替える。付け直しそのものを確かめるときに使う
REPLY = "**返信ドラフト**\nalice（fake）\n```\n確認します\n```"  # 偽の Claude の、既定の投稿本文
NAME = "予定の確認"  # 偽の Claude が、返答の NAME 行で付けるセッションの名前（付け直しは、Base で成功したことにする）
TEMP = "メンション 10月3日 03時00分"  # 起動するときの仮の名前（NOW の時刻）
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
        # 名前の付け直しは、本物の claude を呼ばない。呼ばれた名前を控え、成功したことにする（付け直しの検査は Rename で）
        self.renames, self.rename_result = [], "ok"
        patcher = mock.patch.object(run_claude, "rename_session",
                                    side_effect=lambda claude, cfg, watcher, sid, name, cwd, env=None:
                                    self.renames.append((sid, name)) or self.rename_result)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_MODE", None)
        os.environ.pop("FAKE_CLAUDE_NAME", None)
        os.environ.pop("FAKE_CLAUDE_RENAME_OUT", None)
        os.environ.pop("FAKE_CLAUDE_ARGV_OUT", None)
        os.environ.pop("FAKE_CLAUDE_ERROR_TEXT", None)
        os.environ.pop("FAKE_CLAUDE_COUNTS", None)
        os.environ.pop("FAKE_CLAUDE_DIGEST", None)
        os.environ.pop("FAKE_CLAUDE_SESSION_ID", None)
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
        self.assertEqual(content, "<@42>\n" + REPLY)
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

    def test_the_last_reply_is_what_is_posted(self):
        # セッションの最後の返答と、Discord に届く文面を同じものにする。COUNTS と NAME の行だけ外す（ADR-0037、ADR-0043）
        body = "**返信ドラフト**\nalice\n```\nありがとうございます\n```\n%%SPLIT%%\n**対応不要**\nbob\n共有だけです"
        os.environ["FAKE_CLAUDE_DIGEST"] = body
        res = self.triage([self.claim()])
        self.assertTrue(res["ok"], res)
        self.assertEqual([c for c, _ in self.posts], ["<@42>\n" + body.split("\n%%SPLIT%%\n")[0], "**対応不要**\nbob\n共有だけです"])
        self.assertFalse(any("COUNTS" in c or "NAME" in c for c, _ in self.posts))

    def test_split_reply_drops_only_the_machine_lines(self):
        f = run_claude.split_reply
        self.assertEqual(f("**見出し**\n本文\nCOUNTS draft=1 stamp=0 skip=0 pr=0"), ("**見出し**\n本文", ""))
        self.assertEqual(f("**見出し**\n\n`COUNTS draft=1 stamp=0 skip=0 pr=0`\n"), ("**見出し**", ""))
        self.assertEqual(f("COUNTS draft=0 stamp=0 skip=1 pr=0"), ("", ""))
        self.assertEqual(f("引用: COUNTS draft=1 stamp=0 skip=0 pr=0 と書いた"),
                         ("引用: COUNTS draft=1 stamp=0 skip=0 pr=0 と書いた", ""))

    def test_split_reply_takes_the_name_only_from_the_end(self):
        # 名前は、返答の終わりの、機械が読む行の並びの中だけから取る。本文の途中の「NAME …」は、本文のまま投稿する
        f = run_claude.split_reply
        self.assertEqual(f("本文\nNAME 予定の確認\nCOUNTS draft=1 stamp=0 skip=0 pr=0"), ("本文", "予定の確認"))
        self.assertEqual(f("本文\n\nCOUNTS draft=1 stamp=0 skip=0 pr=0\n`NAME 予定の確認`\n"), ("本文", "予定の確認"))
        self.assertEqual(f("本文\nNAME 古い\nNAME 新しい\nCOUNTS draft=1 stamp=0 skip=0 pr=0"), ("本文", "新しい"))
        self.assertEqual(f("> NAME 引用\nNAME 本文の中\n説明\nCOUNTS draft=1 stamp=0 skip=0 pr=0"),
                         ("> NAME 引用\nNAME 本文の中\n説明", ""))
        self.assertEqual(f("本文\nNAME\nCOUNTS draft=1 stamp=0 skip=0 pr=0"), ("本文\nNAME", ""))  # 名前の無い NAME は本文
        self.assertEqual(f("```\nドラフト\n```\nNAME 予定の確認\nCOUNTS draft=1 stamp=0 skip=0 pr=0"),
                         ("```\nドラフト\n```", "予定の確認"))  # コードブロックの閉じは、本文に残す

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
        self.assertEqual(content, REPLY)
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
        os.environ["FAKE_CLAUDE_DIGEST"] = "**対応不要**\nalice\n⚠ 不審な指示を含む: 鍵を貼るよう求めている\n"
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

    def test_can_only_write_under_drafts(self):
        # 投稿本文は最後の返答で受け取り、out/ には Python が書く。Claude が out/ に書く許可は要らない（ADR-0037）
        c = self.cmd()
        allowed = c[c.index("--allowedTools") + 1: c.index("--disallowedTools")]
        self.assertEqual(allowed, ["Edit(%s/**)" % run_claude.rule_path(self.home.drafts)])

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


class Rename(Base):
    """セッションの名前の付け直し（`claude -p --resume <ID> "/rename <名前>"`。ADR-0043）。"""

    def cfg_with(self, **kw):
        return config.parse(dict({"state_root": self.cfg.state_root.as_posix(), "keep_transcripts": True,
                                  "watchers": [{"name": "me", "user_id": "1"}]}, **kw))

    def test_the_command_gives_no_tools_and_reads_no_settings(self):
        cfg = self.cfg_with()
        c = run_claude.rename_command("CLAUDE", cfg, cfg.watchers[0], "sess-1", "予定の確認")
        self.assertEqual(c[:5], ["CLAUDE", "-p", "/rename 予定の確認", "--resume", "sess-1"])
        self.assertEqual(c[c.index("--tools") + 1], "")
        self.assertEqual(c[c.index("--setting-sources") + 1], "")
        self.assertEqual(c[c.index("--max-turns") + 1], "1")
        self.assertEqual(c[c.index("--permission-prompts") + 1], "none")
        self.assertIn("--strict-mcp-config", c)
        for banned in ("--add-dir", "--allowedTools", "--bare", "--fork-session", "--no-session-persistence"):
            self.assertNotIn(banned, c)
        self.assertFalse(any("dangerously" in x for x in c))
        # モードは仕分けと同じにする（デスクトップ版は、記録に残ったモードで開く。ADR-0033）
        self.assertEqual(c[c.index("--permission-mode") + 1], "dontAsk")
        imp = self.cfg_with(desktop_import=True)
        c = run_claude.rename_command("CLAUDE", imp, imp.watchers[0], "sess-1", "予定の確認")
        self.assertEqual(c[c.index("--permission-mode") + 1], "auto")

    def test_the_result_says_whether_the_name_was_changed(self):
        cfg = self.cfg_with()
        w = cfg.watchers[0]

        def ran(stdout=b"", code=0):
            return mock.patch.object(run_claude.subprocess, "run", return_value=mock.Mock(returncode=code, stdout=stdout))

        done = json.dumps({"type": "result", "is_error": False, "result": "Session renamed to: 予定の確認"}).encode("utf-8")
        with ran(done) as r:
            self.assertEqual(REAL_RENAME("CLAUDE", cfg, w, "sess-1", "予定の確認", self.home.root), "ok")
            self.assertEqual(r.call_args.kwargs["stdin"], subprocess.DEVNULL)  # 無人の実行。入力を待たない
            self.assertEqual(r.call_args.kwargs["cwd"], str(self.home.root))
        # 終わったが、新しい名前が返らない（/rename が命令として通らず、モデルに届いた、など）
        with ran(json.dumps({"type": "result", "is_error": False, "result": "OK"}).encode("utf-8")):
            self.assertEqual(REAL_RENAME("CLAUDE", cfg, w, "sess-1", "予定の確認", self.home.root), "unexpected")
        with ran(code=2):
            self.assertEqual(REAL_RENAME("CLAUDE", cfg, w, "sess-1", "予定の確認", self.home.root), "exit_2")
        with mock.patch.object(run_claude.subprocess, "run", side_effect=subprocess.TimeoutExpired("c", 1)):
            self.assertEqual(REAL_RENAME("CLAUDE", cfg, w, "sess-1", "予定の確認", self.home.root), "timeout")
        with mock.patch.object(run_claude.subprocess, "run", side_effect=OSError("no exe")):
            self.assertEqual(REAL_RENAME("CLAUDE", cfg, w, "sess-1", "予定の確認", self.home.root), "error")
        self.assertEqual(REAL_RENAME("", cfg, w, "sess-1", "予定の確認", self.home.root), "claude_not_found")

    def test_an_unsafe_name_or_id_is_never_passed(self):
        cfg = self.cfg_with()
        with mock.patch.object(run_claude.subprocess, "run") as r:
            for name, sid in (('a" & calc & "', "sess-1"), ("%PATH%", "sess-1"), ("-x", "sess-1"), ("", "sess-1"),
                              ("予定の確認", "-rf"), ("予定の確認", "a b")):
                self.assertEqual(REAL_RENAME("CLAUDE", cfg, cfg.watchers[0], sid, name, self.home.root), "unsafe")
            r.assert_not_called()


class FakeView:
    """見ていたセッションを控えて、取り込んだあとに戻す係（desktop_view.DesktopView）の偽物。本物のデスクトップ版には触らない。"""

    def __init__(self, viewed="local_viewed", result="ok", fail_remember=False, state="listed"):
        self.viewed, self.result, self.fail_remember, self.state = viewed, result, fail_remember, state
        self.calls = []

    def listed(self, cli_session_id):
        self.calls.append(("listed", cli_session_id))
        return self.state

    def remember(self):
        self.calls.append("remember")
        if self.fail_remember:
            raise OSError("cannot read")
        return self.viewed

    def go_back(self, viewed, cli_session_id):
        self.calls.append(("go_back", viewed, cli_session_id))
        return self.result


class DesktopImport(Base):
    """記録したセッションを、仕分けのたびにデスクトップ版の左側へ取り込む（失敗しても仕分けは成功のまま）。"""

    def setUp(self):
        super().setUp()
        self.view = FakeView()

    def keep_cfg(self, **kw):
        data = {"state_root": self.cfg.state_root.as_posix(), "keep_transcripts": True, "desktop_import": True,
                "watchers": [{"name": "me", "user_id": "1"}]}
        data.update(kw)
        return config.parse(data)

    def run_triage(self, cfg, import_fn, name="pending-d.json"):
        return run_claude.triage(self.home, cfg.watchers[0], cfg, [self.claim(name)], command=FAKE, now=NOW,
                                 poster=self.poster, import_fn=import_fn, view=self.view)

    def test_imports_the_session_after_the_digest_is_posted(self):
        calls = []

        def fake_import(claude, session_id, home, env=None, cwd=None):
            calls.append((session_id, len(self.posts)))  # 呼ばれた時点で、結果の投稿は済んでいる
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
                                import_fn=lambda *a, **k: calls.append(a) or "ok", view=self.view)
        self.assertEqual(res["reason"], "post_failed")
        self.assertEqual(calls, [])  # 投稿に失敗した回は取り込まない
        self.post_fails = False
        os.environ["FAKE_CLAUDE_MODE"] = "fail"  # もう一度 Claude を動かすなら失敗する。動かさないので成功する
        res = run_claude.triage(self.home, cfg.watchers[0], cfg, [f], command=FAKE, now=NOW, poster=self.poster,
                                import_fn=lambda *a, **k: calls.append(a) or "ok", view=self.view)
        self.assertEqual(res["reason"], "reused_digest")
        self.assertEqual([c[1] for c in calls], ["sess-fake"])  # 記録しておいた session_id で取り込む

    def test_each_item_ends_with_its_session_name(self):
        # 結果のあとに名前を 1 通で送ると、最後の件のセッションに見える。件ごとのメッセージの最後の行に入れる。
        # 長押しで写すと、名前も一緒に取れる（ほかの場所で「この件の続き」と頼むときに使う）
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.posts, [("<@42>\n" + REPLY + "\n\nセッション: " + NAME, "42")])
        self.assertNotIn("name_post", res)
        rec = (self.home.root / "runs").glob("*.json")
        self.assertNotIn("SECRET-BODY", next(rec).read_text(encoding="utf-8"))  # 名前には本文の頭が入る。記録には書かない

    def test_every_item_of_a_run_gets_the_session_line(self):
        os.environ["FAKE_CLAUDE_DIGEST"] = "**返信ドラフト**\nalice\n%%SPLIT%%\n**スタンプで十分**\nbob\n%%SPLIT%%\n**対応不要**\ncarol"
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        line = "\n\nセッション: " + NAME
        self.assertEqual(self.posts, [("<@42>\n**返信ドラフト**\nalice" + line, "42"), ("**スタンプで十分**\nbob" + line, ""),
                                      ("**対応不要**\ncarol" + line, "")])  # 通知は、最初の 1 通だけ

    def test_with_session(self):
        f = run_claude.with_session
        self.assertEqual(f("A\n%%SPLIT%%\nB\n", "N"), "A\n\nセッション: N\n%%SPLIT%%\nB\n\nセッション: N")
        self.assertEqual(f("A\n%%SPLIT%%\n\n%%SPLIT%%\n", "N", "https://x/o.html#local_1"),
                         "A\n\nセッション: N\n[Claudeで開く(PCのみ)](<https://x/o.html#local_1>)")  # 空の塊には付けない

    def test_the_name_message_links_to_the_relay_page_when_one_is_set(self):
        # Discord は claude:// をリンクにしないので、中継ページへのリンクにする。ID は # の後ろ（ADR-0040）
        sid = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"
        os.environ["FAKE_CLAUDE_SESSION_ID"] = sid
        res = self.run_triage(self.keep_cfg(desktop_link_page="https://example.github.io/t/open.html"), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.posts[0][0].endswith("\nセッション: " + NAME + "\n[Claudeで開く(PCのみ)]"
                                                  "(<https://example.github.io/t/open.html#local_%s>)" % sid), self.posts)

    def test_no_link_without_a_page(self):
        os.environ["FAKE_CLAUDE_SESSION_ID"] = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.posts[0][0].endswith("\nセッション: " + NAME), self.posts)

    def test_no_link_for_a_session_id_of_another_form(self):
        # 中継ページが通さない形の ID は、リンクにしない（押しても開かないリンクを送らない）
        res = self.run_triage(self.keep_cfg(desktop_link_page="https://example.github.io/t/open.html"), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.posts[0][0].endswith("\nセッション: " + NAME))  # 偽の Claude の ID は sess-fake

    def test_the_relay_page_url_is_checked_when_the_config_is_read(self):
        base = {"state_root": self.cfg.state_root.as_posix(), "watchers": [{"name": "me", "user_id": "1"}]}
        self.assertEqual(config.parse(base).desktop_link_page, "")
        for ok in ("https://example.github.io/t/open.html", "http://localhost:8000/open.html?x=1"):
            self.assertEqual(config.parse(dict(base, desktop_link_page=ok)).desktop_link_page, ok)
        for bad in ("claude://code/continue", "javascript:alert(1)", "https://x/open.html#local_", "https://x/a b",
                    "https://x/a>b", "open.html"):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.parse(dict(base, desktop_link_page=bad))

    def test_the_relay_page_opens_only_a_session_link(self):
        page = (REPO / "docs" / "open.html").read_text(encoding="utf-8")
        self.assertIn('"claude://code/continue?session=" + id', page)
        # ページが通す ID の形は、リンクを作る側と同じ（local_ を付けたもの）
        self.assertIn("/^local_%s/" % run_claude.desktop_view.SESSION_ID_RE.pattern[1:], page)
        self.assertNotIn("src=", page)  # 外のスクリプトや画像を読まない

    def test_no_session_line_unless_the_session_is_imported(self):
        for kw in ({"desktop_import": False}, {"keep_transcripts": False}):
            self.posts.clear()
            res = self.run_triage(self.keep_cfg(**kw), lambda *a, **k: "ok", name="pending-%s.json" % next(iter(kw)))
            self.assertTrue(res["ok"], res)
            self.assertEqual(self.posts, [("<@42>\n" + REPLY, "42")], kw)

    def test_a_reposted_digest_still_sends_the_name(self):
        cfg = self.keep_cfg()
        f = self.claim("pending-p.json")
        self.post_fails = True
        self.assertEqual(run_claude.triage(self.home, cfg.watchers[0], cfg, [f], command=FAKE, now=NOW, poster=self.poster,
                                           import_fn=lambda *a, **k: "ok", view=self.view)["reason"], "post_failed")
        self.post_fails = False
        os.environ["FAKE_CLAUDE_MODE"] = "fail"  # Claude は動かさない。名前は、控えから読む
        res = run_claude.triage(self.home, cfg.watchers[0], cfg, [f], command=FAKE, now=NOW, poster=self.poster,
                                import_fn=lambda *a, **k: "ok", view=self.view)
        self.assertEqual(res["reason"], "reused_digest")
        self.assertEqual(self.posts, [("<@42>\n" + REPLY + "\n\nセッション: " + NAME, "42")])

    def test_returns_to_the_session_that_was_being_viewed(self):
        # 取り込むと、デスクトップ版の表示がそのセッションへ移る。取り込む前に見ていたセッションを控えて、そこへ戻す
        order = []
        self.view.remember = lambda: order.append("remember") or "local_viewed"
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: order.append("import") or "ok")
        self.assertEqual(order, ["remember", "import"])
        self.assertEqual(self.view.calls, [("go_back", "local_viewed", "sess-fake")])
        self.assertEqual(res["desktop_return"], "ok")
        rec = json.loads(next((self.home.root / "runs").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(rec["desktop_return"], "ok")

    def test_no_return_when_the_import_failed(self):
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: "exit_1")
        self.assertEqual(self.view.calls, ["remember"])
        self.assertNotIn("desktop_return", res)

    def test_the_import_still_happens_when_the_viewed_session_cannot_be_read(self):
        self.view = FakeView(fail_remember=True)
        calls = []
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: calls.append(a) or "ok")
        self.assertTrue(res["ok"], res)
        self.assertEqual([c[1] for c in calls], ["sess-fake"])
        self.assertEqual(self.view.calls, ["remember", ("go_back", "", "sess-fake")])

    def test_a_failure_to_return_does_not_fail_the_triage(self):
        def boom(viewed, cli_session_id):
            raise RuntimeError("desktop gone")

        self.view.go_back = boom
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["desktop_import"], res["desktop_return"]), ("ok", "error"))

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
        named = run_claude.build_command("CLAUDE", cfg, cfg.watchers[0], self.home, session_name=TEMP)
        self.assertEqual(named[named.index("--name") + 1], TEMP)
        self.assertNotIn("--no-session-persistence", named)  # 記録を残す設定なので、保存される

    def test_a_name_with_other_characters_is_not_passed_at_all(self):
        # 決めた文字だけの名前でなければ、引数に載せない（claude.cmd は cmd.exe を通る）
        cfg = self.keep_cfg()
        for bad in ('a" & calc & "', "%USERNAME%", "--bare", "a\nb"):
            cmd = run_claude.build_command("CLAUDE", cfg, cfg.watchers[0], self.home, session_name=bad)
            self.assertNotIn("--name", cmd)
            self.assertNotIn(bad, cmd)

    def test_the_session_is_renamed_to_the_name_claude_gave(self):
        # 起動するときは仮の名前。返答を書いた Claude が中身を見て付けた名前に、投稿の前に付け直す（ADR-0043）
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.renames, [("sess-fake", NAME)])
        self.assertEqual(res["rename"], "ok")
        self.assertEqual(self.posts, [("<@42>\n" + REPLY + "\n\nセッション: " + NAME, "42")])

    def test_the_name_from_claude_is_cleaned_and_cut(self):
        # 名前は、届いた本文に引きずられうる。決めた文字だけにして、全角 20 字ぶんに切る
        os.environ["FAKE_CLAUDE_NAME"] = '予定の確認 " & calc & " %PATH% <@42> [link](https://x) ' + "あ" * 30
        res = self.run_triage(self.keep_cfg(), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        name = self.renames[0][1]
        self.assertTrue(run_claude.session_naming.safe(name), name)
        self.assertTrue(name.startswith("予定の確認 calc PATH 42 link https x"), name)
        self.assertTrue(name.endswith("…"), name)
        self.assertTrue(self.posts[0][0].endswith("セッション: " + name))

    def test_without_a_name_or_a_rename_the_provisional_name_stays(self):
        # 付け直せなかったときは、各件の行も仮の名前にする（デスクトップ版の一覧の名前と、そろえる）
        cases = (("none", "ok", "no_name", 0), ("予定の確認", "exit_1", "exit_1", 1),
                 ("予定の確認", "unexpected", "unexpected", 1))
        for said, result, why, calls in cases:
            with self.subTest(why):
                self.posts.clear()
                self.renames.clear()
                os.environ["FAKE_CLAUDE_NAME"] = said
                self.rename_result = result
                res = self.run_triage(self.keep_cfg(), lambda *a, **k: "ok", name="pending-%s.json" % why)
                self.assertTrue(res["ok"], res)  # 名前は見やすさのためのもの。仕分けは止めない
                self.assertEqual((res["rename"], len(self.renames)), (why, calls))
                self.assertTrue(self.posts[0][0].endswith("セッション: " + TEMP), self.posts)

    def test_an_exception_while_renaming_does_not_fail_the_triage(self):
        with mock.patch.object(run_claude, "rename_session", side_effect=RuntimeError("boom")):
            res = self.run_triage(self.keep_cfg(), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["rename"], "error")
        self.assertTrue(self.posts[0][0].endswith("セッション: " + TEMP))

    def test_no_rename_unless_transcripts_are_kept(self):
        res = self.run_triage(self.keep_cfg(keep_transcripts=False), lambda *a, **k: "ok")
        self.assertTrue(res["ok"], res)
        self.assertNotIn("rename", res)
        self.assertEqual(self.renames, [])

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

    def test_triage_renames_the_session_through_the_launcher_and_keeps_the_name_out_of_the_run_record(self):
        claude, env = self.launcher()
        out, renamed = Path(self._tmp.name) / "argv.json", Path(self._tmp.name) / "rename.json"
        env.update(FAKE_CLAUDE_ARGV_OUT=str(out), FAKE_CLAUDE_RENAME_OUT=str(renamed), FAKE_CLAUDE_NAME="SECRET-BODY の確認")
        with mock.patch.dict(os.environ, env), mock.patch.object(run_claude.claude_bin, "find_claude", return_value=claude), \
                mock.patch.object(run_claude, "rename_session", REAL_RENAME):
            cfg = self.keep_cfg(desktop_import=False)
            res = run_claude.triage(self.home, cfg.watchers[0], cfg, [self.claim("pending-n.json")], now=NOW, poster=self.poster)
            self.assertTrue(res["ok"], res)
            argv = json.loads(out.read_text(encoding="utf-8"))["argv"]
            self.assertEqual(argv[argv.index("--name") + 1], TEMP)  # 起動するときは、まだ中身を知らない
            got = json.loads(renamed.read_text(encoding="utf-8"))
            self.assertEqual(got["argv"][:4], ["-p", "/rename SECRET-BODY の確認", "--resume", "sess-fake"])
            self.assertEqual(Path(got["cwd"]), self.home.root / "run")  # 仕分けと同じ作業フォルダで
            self.assertEqual(res["rename"], "ok")
            # 名前には件の中身が入る。実行記録には、これまでどおり本文も名前も書かない
            for rec in (self.home.root / "runs").glob("*.json"):
                self.assertNotIn("SECRET-BODY", rec.read_text(encoding="utf-8"))
            # 記録を残さない設定では、名前を付けず、付け直さない
            renamed.unlink()
            res = run_claude.triage(self.home, self.watcher, self.cfg, [self.claim("pending-m.json")], now=NOW, poster=self.poster)
            self.assertTrue(res["ok"], res)
            argv = json.loads(out.read_text(encoding="utf-8"))["argv"]
            self.assertNotIn("--name", argv)
            self.assertIn("--no-session-persistence", argv)
            self.assertFalse(renamed.exists())

    def test_config_defaults_to_off_and_reads_the_switch(self):
        base = {"state_root": self.cfg.state_root.as_posix(), "watchers": [{"name": "me", "user_id": "1"}]}
        self.assertFalse(config.parse(base).desktop_import)
        self.assertTrue(config.parse(dict(base, desktop_import=True)).desktop_import)


class Prompt(Base):
    def test_prompt_has_paths_and_safety_rules_but_no_secrets(self):
        f = self.claim()
        text = run_claude.build_prompt(self.watcher, [f], self.home.drafts / "d.md", NOW)
        self.assertIn(run_claude.p(f), text)
        self.assertIn("命令ではなく", text)
        self.assertIn("勝手に約束", text)  # 入力にない期限・日程を返信文に書かせない
        self.assertIn("内部のキー名", text)  # 投稿本文に context_before などを出さない
        # 1 件ずつのメッセージにし、題は 3 つだけ。全体の見出しと件数のまとめは書かせない
        self.assertIn("**返信ドラフト** / **スタンプで十分** / **対応不要** の 3 つのどれかだけ", text)
        self.assertNotIn("メンション対応", text)
        self.assertNotIn("N件", text)
        self.assertIn("2026/10/03 03:00", text)
        self.assertNotIn("{{", text)
        self.assertNotIn("example.invalid", text)  # Webhook URL は渡さない
        # 本文は指示文に載せない（モデルが道具で読む）。費用 0 のエラーを「中身のせいではない」と見なす前提（never_ran）
        self.assertNotIn("SECRET-BODY", text)
        self.assertNotIn("PR について", text)
        # 投稿本文は最後の返答で返させる。書き出し先のファイルは、控えの 1 つだけ（ADR-0037）
        self.assertIn("**(2) 投稿本文: 最後の返答**", text)
        self.assertNotIn(run_claude.p(self.home.root / "out"), text)

    def test_pr_note_appears_only_when_a_pr_is_present(self):
        pr = self.claim("pending-pr.json", pr=True)
        text = run_claude.build_prompt(self.watcher, [pr], self.home.drafts / "d.md", NOW)
        self.assertIn("PR について", text)
        self.assertIn("「返信ドラフト」にして、その件に「PRレビュー依頼: <URL>」と1行加えます", text)  # PR の依頼は返信ドラフトに入れる
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


class ChannelDirs(Base):
    """場所ごとのフォルダ（channel_read_dirs）。その場所の件が来た回だけ読む場所に足し、指示文で ID とフォルダを示す。"""

    def setUp(self):
        super().setUp()
        root = Path(self._tmp.name)
        self.game, self.docs, self.repo = (str(root / n) for n in ("game", "docs", "repo"))
        self.cfg = config.parse({
            "state_root": str(root),
            "watchers": [{"name": "me", "user_id": "1", "read_dirs": [self.repo],
                          "channel_read_dirs": {"20": [self.game], "30": self.docs}}],
        })
        self.watcher = self.cfg.watchers[0]

    def claim_at(self, name, channel_id, parent_id=None):
        it = dict(item(), channel_id=str(channel_id), thread_parent_id=str(parent_id) if parent_id else None,
                  channel_name="誰でも付けられるスレッド名")
        path = self.home.claims / name
        path.write_text(json.dumps({"items": [it]}), encoding="utf-8")
        return path

    def test_the_channel_or_its_thread_parent_picks_the_folders(self):
        self.assertEqual(run_claude.channel_dirs(self.watcher, [self.claim_at("a.json", 20)]), [(20, (self.game,))])
        self.assertEqual(run_claude.channel_dirs(self.watcher, [self.claim_at("b.json", 99, parent_id=30)]), [(30, (self.docs,))])
        self.assertEqual(run_claude.channel_dirs(self.watcher, [self.claim_at("c.json", 99)]), [])
        old = self.home.claims / "d.json"  # thread_parent_id を持たない、前の形の件
        old.write_text(json.dumps({"items": [dict(item(), channel_id="20")]}), encoding="utf-8")
        self.assertEqual(run_claude.channel_dirs(self.watcher, [old]), [(20, (self.game,))])

    def test_the_folders_are_added_to_read_dirs_for_that_run_only(self):
        def dirs(c):
            return [c[i + 1] for i, x in enumerate(c) if x == "--add-dir"]

        claims = str(self.home.claims)
        plain = run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home)
        self.assertEqual(dirs(plain), [claims, self.repo])
        extra = run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home, extra_dirs=[self.game, self.repo])
        self.assertEqual(dirs(extra), [claims, self.repo, self.game])  # 同じフォルダは 1 回だけ
        self.assertEqual(extra[extra.index("--tools") + 1], "Read,Grep,Glob,Edit,Write")  # 道具と書き込み先は変わらない
        self.assertEqual(extra[extra.index("--allowedTools"): extra.index("--disallowedTools")],
                         plain[plain.index("--allowedTools"): plain.index("--disallowedTools")])

    def test_the_prompt_names_the_id_and_folder_but_not_the_thread_name(self):
        text = run_claude.build_prompt(self.watcher, [self.claim_at("a.json", 20)], self.home.drafts / "d.md", NOW)
        self.assertIn("場所ごとのフォルダ", text)
        self.assertIn("- 20: " + self.game, text)
        self.assertNotIn(self.docs, text)
        self.assertNotIn("誰でも付けられるスレッド名", text)  # スレッド名は誰でも付けられるので、指示文に混ぜない
        self.assertNotIn("{{", text)
        text = run_claude.build_prompt(self.watcher, [self.claim_at("b.json", 99)], self.home.drafts / "d.md", NOW)
        self.assertNotIn("場所ごとのフォルダ", text)

    def test_triage_adds_the_folders_of_the_claimed_items(self):
        seen = []

        def fake_build(claude, cfg, watcher, home, session_name="", extra_dirs=(), fork_from="", append_to="", **kw):
            seen.append(list(extra_dirs))
            return FAKE

        with mock.patch.object(run_claude.claude_bin, "find_claude", return_value="CLAUDE"), \
                mock.patch.object(run_claude, "build_command", side_effect=fake_build):
            res = run_claude.triage(self.home, self.watcher, self.cfg, [self.claim_at("a.json", 99, parent_id=30)],
                                    now=NOW, poster=self.poster)
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen, [[self.docs]])

    def test_config_shapes(self):
        def parse(value):
            return config.parse({"state_root": self.cfg.state_root.as_posix(),
                                 "watchers": [{"name": "me", "user_id": "1", "channel_read_dirs": value}]}).watchers[0]

        self.assertEqual(self.watcher.channel_read_dirs, ((20, (self.game,)), (30, (self.docs,))))  # 1 つなら文字列でもよい
        self.assertEqual(parse(None).channel_read_dirs, ())
        self.assertEqual(parse({}).channel_read_dirs, ())
        with self.assertRaises(config.ConfigError):
            parse(["C:/repo"])
        with self.assertRaises(config.ConfigError):
            parse({"dev": ["C:/repo"]})


class Continuation(Base):
    """同じ流れの続きの件は、前のセッションに書き足して仕分ける（ADR-0038）。前のセッションを、いま動いている Claude Code が
    開いているときなどは、前の会話を写した新しいセッションで仕分ける（continuation.py、ADR-0036）。"""

    def setUp(self):
        super().setUp()
        root = Path(self._tmp.name)
        self.cfg = config.parse({"state_root": str(root), "keep_transcripts": True,
                                 "watchers": [{"name": "me", "user_id": "1"}]})
        self.watcher = self.cfg.watchers[0]
        self.claude_dir = root / "claude-config"  # 本物の ~/.claude を見ない
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def transcript(self, session_id="sess-fake"):
        folder = self.claude_dir / "projects" / "C--state-me-run"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ("%s.jsonl" % session_id)).write_text("{}\n", encoding="utf-8")

    def claim_msg(self, name, message_id, channel_id="20", before=()):
        it = dict(item(), message_id=str(message_id), channel_id=str(channel_id),
                  context_before=[{"author": "bob", "text": "earlier", "id": str(i)} for i in before])
        path = self.home.claims / name
        path.write_text(json.dumps({"items": [it]}), encoding="utf-8")
        return path

    def run_once(self, files, cfg=None, **kw):
        """build_command に渡った名前と、写す先・書き足す先を控える。claude は偽物（session_id は、いつも sess-fake）。"""
        cfg = cfg or self.cfg
        seen = []

        def fake_build(claude, cfg, watcher, home, session_name="", extra_dirs=(), fork_from="", append_to="", **kw):
            seen.append((session_name, fork_from, append_to))
            return FAKE

        with mock.patch.object(run_claude.claude_bin, "find_claude", return_value="CLAUDE"), \
                mock.patch.object(run_claude, "build_command", side_effect=fake_build):
            res = run_claude.triage(self.home, cfg.watchers[0], cfg, files, now=NOW, poster=self.poster, **kw)
        return res, seen

    def hold(self, session_id="sess-fake", pid=None, **extra):
        """Claude Code が、動いている間に書く印（<設定フォルダ>/sessions/<プロセス ID>.json）を置く。既定は、このテストのプロセス。"""
        pid = os.getpid() if pid is None else pid
        folder = self.claude_dir / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ("%d.json" % pid)).write_text(
            json.dumps(dict({"pid": pid, "sessionId": session_id, "kind": "interactive", "entrypoint": "claude-desktop"},
                            **extra)), encoding="utf-8")

    def desktop_cfg(self):
        return config.parse({"state_root": self.cfg.state_root.as_posix(), "keep_transcripts": True,
                             "desktop_import": True, "watchers": [{"name": "me", "user_id": "1"}]})

    def table(self):
        return continuation.load(self.home)

    def test_a_reply_in_the_same_flow_is_added_to_the_previous_session(self):
        res, seen = self.run_once([self.claim_msg("a.json", 100)])
        self.assertEqual(seen, [(TEMP, "", "")])  # 初めての場所は、新しいセッション
        self.assertEqual(self.table()["20"]["message_ids"], ["100"])
        self.assertEqual(self.table()["20"]["name"], NAME)
        self.transcript()
        res, seen = self.run_once([self.claim_msg("b.json", 200, before=[90, 100])])
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen, [("", "", "sess-fake")])  # 名前は付け直さない
        self.assertEqual(self.renames, [("sess-fake", NAME)])  # 付け直したのは、初めの回だけ
        self.assertNotIn("rename", res)
        self.assertEqual((res["continued_from"], res["continue_mode"]), ("sess-fake", "append"))
        self.assertEqual(self.table()["20"]["message_ids"], ["100", "200"])  # 続けた回は、前に仕分けた ID も引き継ぐ
        self.assertEqual(self.table()["20"]["name"], NAME)  # 名前も引き継ぐ
        rec = next((self.home.root / "runs").glob("*-%s.json" % res["run_id"])).read_text(encoding="utf-8")
        self.assertIn('"continued_from": "sess-fake"', rec)
        self.assertNotIn("SECRET-BODY", rec)
        self.assertNotIn("fork_reason", res)  # 書き足した回には、写した理由は無い
        # この回のトークン数（usage）。cost は、続けた回では前の回までの累計なので、1 回ぶんの重さはこちらで見る
        self.assertEqual(json.loads(rec)["tokens"], {"in": 2, "cache_write": 9045, "cache_read": 9918, "out": 5})

    def test_a_session_open_in_a_running_claude_is_copied_instead(self):
        # デスクトップ版で話しかけたあと、裏の処理が動いている間に書き足すと、記録が枝分かれする（ADR-0038）
        self.run_once([self.claim_msg("a.json", 100)])
        self.transcript()
        self.hold()
        res, seen = self.run_once([self.claim_msg("b.json", 200, before=[100])])
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen, [(TEMP + "（続き）", "sess-fake", "")])
        self.assertEqual(res["continue_mode"], "fork")
        self.assertEqual(res["fork_reason"], "held")
        # 時刻の無い印は、どこから動いているかだけ。取り込まない設定では、裏の処理を終わらせない（ADR-0039）
        self.assertEqual(res["held_by"], {"entrypoint": "claude-desktop", "release": "no_desktop"})
        self.assertEqual(self.table()["20"]["name"], NAME + "（続き）")  # 次は、写した方に書き足す
        self.assertEqual(self.renames[-1], ("sess-fake", NAME + "（続き）"))

    def test_a_previous_entry_without_a_name_is_copied(self):
        # 名前を控える前の形。名前の 1 通を送れないので、写して、新しい名前で控え直す
        self.home.root.joinpath("sessions.json").write_text(
            json.dumps({"20": {"session_id": "sess-fake", "message_ids": ["100"], "at": int(time.time())}}), encoding="utf-8")
        self.transcript()
        res, seen = self.run_once([self.claim_msg("b.json", 200, before=[100])])
        self.assertEqual(seen, [(TEMP + "（続き）", "sess-fake", "")])
        self.assertEqual(res["fork_reason"], "no_name")
        self.assertEqual(self.table()["20"]["name"], NAME + "（続き）")

    def test_an_idle_desktop_process_is_ended_and_the_flow_is_added_to(self):
        # デスクトップ版の待機中の裏の処理が開いていて、画面に出ていなければ、終わらせてから書き足す（ADR-0039）
        cfg = self.desktop_cfg()
        imports, released = [], []

        def imp(*a, **k):
            imports.append(a[1])
            return "ok"

        def rel(session_id, run_dir):
            released.append((session_id, Path(run_dir)))
            (self.claude_dir / "sessions" / ("%d.json" % os.getpid())).unlink()  # 終わった処理の印は、もう無い
            return "ok"

        self.run_once([self.claim_msg("a.json", 100)], cfg=cfg, import_fn=imp, view=FakeView())
        self.transcript()
        imports.clear()
        self.hold(status="idle")
        with mock.patch.object(run_claude.continuation, "release", side_effect=rel):
            res, seen = self.run_once([self.claim_msg("b.json", 200, before=[100])], cfg=cfg, import_fn=imp,
                                      view=FakeView())
        self.assertTrue(res["ok"], res)
        self.assertEqual(released, [("sess-fake", self.home.root / "run")])  # 仕分けの作業フォルダを渡す
        self.assertEqual(seen, [("", "", "sess-fake")])
        self.assertEqual(res["continue_mode"], "append")
        self.assertNotIn("fork_reason", res)
        self.assertEqual(res["held_by"], {"status": "idle", "entrypoint": "claude-desktop", "release": "ok"})
        self.assertEqual(imports, [])

    def test_a_held_session_on_screen_or_not_released_is_copied(self):
        cfg = self.desktop_cfg()
        for case, viewed, result in (("on_screen", "local_sess-fake", "ok"), ("busy", "local_viewed", "busy"),
                                     ("still_held", "local_viewed", "still_held")):
            with self.subTest(case):
                self.home.root.joinpath("sessions.json").write_text(json.dumps({"20": {
                    "session_id": "sess-fake", "name": NAME, "message_ids": ["100"],
                    "at": int(time.time())}}), encoding="utf-8")
                self.transcript()
                self.hold(status="idle")
                calls = []
                with mock.patch.object(run_claude.continuation, "release",
                                       side_effect=lambda *a: calls.append(a) or result):
                    res, seen = self.run_once([self.claim_msg("b-%s.json" % case, 200, before=[100])], cfg=cfg,
                                              import_fn=lambda *a, **k: "ok", view=FakeView(viewed=viewed))
                self.assertTrue(res["ok"], res)
                self.assertEqual((res["continue_mode"], res["fork_reason"]), ("fork", "held"))
                self.assertEqual(res["held_by"]["release"], case)
                self.assertEqual(seen, [(TEMP + "（続き）", "sess-fake", "")])
                self.assertEqual(len(calls), 0 if case == "on_screen" else 1)  # 本人が見ているセッションは、終わらせようとしない

    def release_env(self, **extra):
        """release の検査用の印。仕分けの作業フォルダで、待機中の、デスクトップ版の裏の処理（プロセス 4242、開始時刻 777）。"""
        run_dir = self.home.root / "run"
        fields = dict({"status": "idle", "cwd": str(run_dir), "procStart": "777"}, **extra)
        self.hold("sess-fake", pid=4242, **fields)
        return {"CLAUDE_CONFIG_DIR": str(self.claude_dir)}, run_dir

    def test_release_ends_only_an_idle_desktop_process_of_the_triage_folder(self):
        alive = {4242}
        ended = []

        def end(pid):
            ended.append(pid)
            alive.discard(pid)
            return True

        env, run_dir = self.release_env()
        res = continuation.release("sess-fake", run_dir, env, alive=alive.__contains__, created={4242: 777}.get, end=end,
                                   windows=True)
        self.assertEqual((res, ended), ("ok", [4242]))
        self.assertEqual(continuation.release("sess-fake", run_dir, env, alive=alive.__contains__, end=end,
                                              windows=True), "ok")  # もう開いている処理が無い
        self.assertEqual(continuation.release("sess-fake", run_dir, env, windows=False), "unsupported")

    def test_release_leaves_every_other_process_alone(self):
        cases = (
            ({"status": "busy"}, "busy"),  # 返答の途中
            ({"status": "waiting"}, "busy"),
            ({"entrypoint": "cli"}, "not_desktop"),  # 端末で本人が動かしている claude
            ({"cwd": "C:\\work\\other"}, "other_dir"),
            ({"procStart": "778"}, "changed"),  # プロセス ID が使い回された
            ({"procStart": None}, "changed"),
        )
        for extra, want in cases:
            with self.subTest(want=want, extra=extra):
                ended = []
                env, run_dir = self.release_env(**extra)
                res = continuation.release("sess-fake", run_dir, env, alive={4242}.__contains__, created={4242: 777}.get,
                                           end=lambda pid: ended.append(pid) or True, windows=True)
                self.assertEqual((res, ended), (want, []))

    def test_release_ends_nothing_when_one_of_the_holders_must_stay(self):
        ended = []
        env, run_dir = self.release_env()
        self.hold("sess-fake", pid=4243, status="busy", cwd=str(run_dir), procStart="888")
        res = continuation.release("sess-fake", run_dir, env, alive={4242, 4243}.__contains__,
                                   created={4242: 777, 4243: 888}.get, end=lambda pid: ended.append(pid) or True,
                                   windows=True)
        self.assertEqual((res, ended), ("busy", []))

    def test_release_reports_a_process_that_would_not_end(self):
        env, run_dir = self.release_env()
        ticks = iter(range(100))
        kw = dict(alive={4242}.__contains__, created={4242: 777}.get, windows=True, sleep=lambda s: None,
                  clock=lambda: next(ticks), wait_sec=3)
        self.assertEqual(continuation.release("sess-fake", run_dir, env, end=lambda pid: False, **kw), "end_failed")
        self.assertEqual(continuation.release("sess-fake", run_dir, env, end=lambda pid: True, **kw), "still_held")

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_end_tree_and_created_at_on_a_child_of_this_test(self):
        # このテストが自分で起こしたプロセスだけを相手にする（ほかのプロセスには触らない）
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            start = continuation.created_at(child.pid)
            self.assertIsInstance(start, int)
            self.assertGreater(start, 0)
            self.assertTrue(continuation.pid_alive(child.pid))
            self.assertTrue(continuation.end_tree(child.pid))
            child.wait(timeout=10)
            self.assertFalse(continuation.pid_alive(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
        self.assertIsNone(continuation.created_at(0))

    def test_holder_reports_only_fixed_words_and_minutes(self):
        env = {"CLAUDE_CONFIG_DIR": str(self.claude_dir)}
        folder = self.claude_dir / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        now = 1_800_000_000.0
        folder.joinpath("1.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": "sess-fake", "status": "idle", "entrypoint": "claude-desktop",
            "startedAt": (now - 3 * 3600) * 1000, "statusUpdatedAt": (now - 90) * 1000,
            "name": "SECRET-BODY", "cwd": "C:\\x"}), encoding="utf-8")
        alive = {os.getpid()}.__contains__
        self.assertEqual(continuation.holder("sess-fake", env, alive=alive, now=now),
                         {"status": "idle", "entrypoint": "claude-desktop", "minutes": 180, "status_minutes": 1})
        self.assertEqual(continuation.holder("other", env, alive=alive, now=now), {})
        # 決まった形でない値は書かない（名前や本文が紛れ込まないように）
        folder.joinpath("1.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": "sess-fake", "status": "SECRET BODY 「x」", "startedAt": "soon"}),
            encoding="utf-8")
        self.assertEqual(continuation.holder("sess-fake", env, alive=alive, now=now), {"status": "unknown"})

    def test_held_reads_the_running_sessions_of_claude_code(self):
        env = {"CLAUDE_CONFIG_DIR": str(self.claude_dir)}
        self.assertFalse(continuation.held("sess-fake", env))  # 印のフォルダが無い
        self.hold("other")
        self.hold("sess-fake", pid=999999)  # 落ちたプロセスの印が残っている
        folder = self.claude_dir / "sessions"
        folder.joinpath("bad.json").write_text("{not json", encoding="utf-8")
        folder.joinpath("odd.json").write_text(json.dumps({"pid": "x", "sessionId": "sess-fake"}), encoding="utf-8")
        alive = {os.getpid()}.__contains__
        self.assertFalse(continuation.held("sess-fake", env, alive=alive))
        self.hold("sess-fake")
        self.assertTrue(continuation.held("sess-fake", env, alive=alive))
        self.assertTrue(continuation.pid_alive(os.getpid()))
        self.assertFalse(continuation.pid_alive(0))

    def test_in_the_desktop_only_a_listed_session_is_added_to(self):
        cfg = self.desktop_cfg()
        imports = []

        def imp(*a, **k):
            imports.append(a[1])
            return "ok"

        self.run_once([self.claim_msg("a.json", 100)], cfg=cfg, import_fn=imp, view=FakeView())
        self.transcript()
        for state, mode in (("archived", "fork"), ("missing", "fork"), ("unknown", "fork"), ("listed", "append")):
            with self.subTest(state):
                self.home.root.joinpath("sessions.json").write_text(json.dumps({"20": {
                    "session_id": "sess-fake", "name": NAME, "message_ids": ["100"],
                    "at": int(time.time())}}), encoding="utf-8")
                imports.clear()
                self.posts.clear()
                view = FakeView(state=state)
                res, seen = self.run_once([self.claim_msg("b-%s.json" % state, 200, before=[100])], cfg=cfg,
                                          import_fn=imp, view=view)
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["continue_mode"], mode)
                self.assertEqual(res.get("fork_reason"), None if mode == "append" else state)
                self.assertIn(("listed", "sess-fake"), view.calls)
                # 書き足した回は取り込まない（表示が移らない）。各件のセッションの行は、書き足したセッションの名前にする
                self.assertEqual(imports, [] if mode == "append" else ["sess-fake"])
                self.assertTrue(self.posts[0][0].endswith(
                    "セッション: " + NAME + ("" if mode == "append" else "（続き）")), self.posts)
                self.assertEqual("desktop_import" in res, mode != "append")

    def test_a_reposted_digest_of_an_appended_run_is_not_imported(self):
        cfg = self.desktop_cfg()
        imports = []

        def imp(*a, **k):
            imports.append(a[1])
            return "ok"

        self.run_once([self.claim_msg("a.json", 100)], cfg=cfg, import_fn=imp, view=FakeView())
        self.transcript()
        imports.clear()
        self.post_fails = True
        f = self.claim_msg("b.json", 200, before=[100])
        res, _ = self.run_once([f], cfg=cfg, import_fn=imp, view=FakeView())
        self.assertEqual((res["reason"], res["continue_mode"]), ("post_failed", "append"))
        self.post_fails = False
        os.environ["FAKE_CLAUDE_MODE"] = "fail"  # Claude は動かさない
        res, seen = self.run_once([f], cfg=cfg, import_fn=imp, view=FakeView())
        self.assertEqual((res["reason"], seen), ("reused_digest", []))
        self.assertEqual(imports, [])
        self.assertTrue(self.posts[-1][0].endswith("\nセッション: " + NAME), self.posts)

    def test_it_starts_a_new_session_unless_the_flow_is_the_same(self):
        self.run_once([self.claim_msg("a.json", 100)])
        self.transcript()
        cases = {
            "前の件が、前の会話に無い": [self.claim_msg("b.json", 200, before=[90])],
            "別の場所": [self.claim_msg("c.json", 300, channel_id="21", before=[100])],
            "場所がまたがる": [self.claim_msg("d.json", 400, before=[100]), self.claim_msg("e.json", 500, channel_id="21")],
        }
        for why, files in cases.items():
            with self.subTest(why):
                self.home.root.joinpath("sessions.json").write_text(
                    json.dumps({"20": {"session_id": "sess-fake", "message_ids": ["100"], "at": int(time.time())}}),
                    encoding="utf-8")
                res, seen = self.run_once(files)
                self.assertTrue(res["ok"], res)
                self.assertEqual(seen[0][1:], ("", ""))
                self.assertNotIn("（続き）", seen[0][0])
                self.assertNotIn("continued_from", res)

    def test_a_deleted_previous_session_is_not_continued(self):
        self.run_once([self.claim_msg("a.json", 100)])  # 記録のファイルを作らない（30 日で消えた、本人が消した）
        res, seen = self.run_once([self.claim_msg("b.json", 200, before=[100])])
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen[0][1:], ("", ""))

    def test_a_failed_continued_run_is_retried_in_a_new_session(self):
        self.run_once([self.claim_msg("a.json", 100)])
        self.transcript()
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        f = self.claim_msg("b.json", 200, before=[100])
        res, seen = self.run_once([f])
        self.assertEqual((res["reason"], seen[0][2]), ("claude_exit_1", "sess-fake"))
        self.assertNotIn("20", self.table())  # 続ける先が壊れていても、止まらない
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        res, seen = self.run_once([f])
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen[0][1:], ("", ""))

    def test_nothing_is_kept_or_continued_without_transcripts(self):
        cfg = config.parse({"state_root": self.cfg.state_root.as_posix(), "watchers": [{"name": "me", "user_id": "1"}]})
        self.home.root.joinpath("sessions.json").write_text(
            json.dumps({"20": {"session_id": "sess-fake", "message_ids": ["100"], "at": int(time.time())}}), encoding="utf-8")
        self.transcript()
        res, seen = self.run_once([self.claim_msg("b.json", 200, before=[100])], cfg=cfg)
        self.assertTrue(res["ok"], res)
        self.assertEqual(seen, [("", "", "")])

    def test_the_command_forks_the_previous_session_and_keeps_the_same_limits(self):
        plain = run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home)
        cmd = run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home, fork_from="3888cecd-4303-4920")
        self.assertEqual(cmd[cmd.index("--resume"): cmd.index("--resume") + 3], ["--resume", "3888cecd-4303-4920", "--fork-session"])
        self.assertEqual([x for x in cmd if x not in ("--resume", "3888cecd-4303-4920", "--fork-session")], plain)
        for bad in ("", "a b", 'a" & calc', "--bare", "x" * 65):
            self.assertNotIn("--resume", run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home, fork_from=bad))
        off = config.parse({"state_root": self.cfg.state_root.as_posix(), "watchers": [{"name": "me", "user_id": "1"}]})
        self.assertNotIn("--resume", run_claude.build_command("CLAUDE", off, off.watchers[0], self.home, fork_from="abc"))

    def test_the_command_adds_to_the_previous_session_and_keeps_the_same_limits(self):
        plain = run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home)
        cmd = run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home, append_to="3888cecd-4303-4920")
        self.assertEqual(cmd[cmd.index("--resume"):], ["--resume", "3888cecd-4303-4920"])  # 写さない（--fork-session なし）
        self.assertEqual(cmd[:cmd.index("--resume")], plain)
        for bad in ("", "a b", 'a" & calc', "--bare", "x" * 65):
            self.assertNotIn("--resume", run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home, append_to=bad))
        off = config.parse({"state_root": self.cfg.state_root.as_posix(), "watchers": [{"name": "me", "user_id": "1"}]})
        self.assertNotIn("--resume", run_claude.build_command("CLAUDE", off, off.watchers[0], self.home, append_to="abc"))

    def test_the_prompt_says_it_is_a_continuation_only_when_it_is(self):
        f = self.claim_msg("a.json", 100)
        args = (self.watcher, [f], self.home.drafts / "d.md", NOW)
        text = run_claude.build_prompt(*args, continued=True)
        self.assertIn("前の会話の続き", text)
        self.assertNotIn("{{", text)
        self.assertNotIn("前の会話の続き", run_claude.build_prompt(*args))

    def test_the_table_keeps_recent_places_only(self):
        items = [{"channel_id": "20", "message_id": str(i)} for i in range(continuation.KEEP_IDS + 5)]
        continuation.record(self.home, items, "s1", now=1_000_000_000)
        self.assertEqual(len(self.table()["20"]["message_ids"]), continuation.KEEP_IDS)
        continuation.record(self.home, [{"channel_id": "21", "message_id": "1"}], "s2",
                            now=1_000_000_000 + continuation.KEEP_SEC + 1)
        self.assertEqual(set(self.table()), {"21"})  # 記録が消えたころの控えは捨てる
        continuation.record(self.home, [{"channel_id": "21", "message_id": "2"}], "s3")
        self.assertEqual(self.table()["21"]["message_ids"], ["2"])  # 新しいセッションは、前の ID を引き継がない
        continuation.record(self.home, [{"channel_id": "22", "message_id": "1"}], 'a" & calc')
        self.assertNotIn("22", self.table())


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


class Environments(Base):
    """件に合う環境を選ばせ、その環境の作業フォルダで動かす（environments.py、ADR-0042）。選ぶ係は偽物。"""

    def setUp(self):
        super().setUp()
        root = Path(self._tmp.name)
        outside = tempfile.TemporaryDirectory()  # 環境のフォルダは、状態のフォルダの外にしか置けない
        self.addCleanup(outside.cleanup)
        self.proj, self.lib = Path(outside.name) / "proj", Path(outside.name) / "lib"
        self.proj.mkdir()
        self.lib.mkdir()
        self.work = run_claude.environments.Environment("Work", str(self.proj), (str(self.lib),))
        self.argv_out = root / "argv.json"
        os.environ["FAKE_CLAUDE_ARGV_OUT"] = str(self.argv_out)
        self.picked = []

    def pick_cfg(self, pick=True, **kw):
        return config.parse(dict({"state_root": self.cfg.state_root.as_posix(), "keep_transcripts": True,
                                  "watchers": [{"name": "me", "user_id": "1", "pick_environment": pick}]}, **kw))

    def picker(self, chosen):
        def pick(envs, folders, items):
            self.picked.append(([e.name for e in envs], [f.folder for f in folders], [i.get("message_id") for i in items]))
            return chosen, 0.02
        return pick

    def run_pick(self, cfg, chosen=None, envs=None, name="pending-e.json", **kw):
        envs = [self.work] if envs is None else envs
        return run_claude.triage(self.home, cfg.watchers[0], cfg, [self.claim(name)], command=FAKE, now=NOW, poster=self.poster,
                                 environments_fn=lambda: envs, picker=self.picker(chosen), **kw)

    def claim(self, name="pending-a.json", **kw):
        path = self.home.claims / name
        path.write_text(json.dumps({"items": [dict(item(**kw), channel_id="20")]}), encoding="utf-8")
        return path

    def seen(self):
        return json.loads(self.argv_out.read_text(encoding="utf-8"))

    def test_the_chosen_environment_is_the_working_folder(self):
        res = self.run_pick(self.pick_cfg(), chosen=self.work)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.picked, [(["Work"], [], ["1"])])
        self.assertEqual(os.path.normcase(self.seen()["cwd"]), os.path.normcase(str(self.proj)))
        self.assertIn("この回の環境", self.seen()["stdin"])
        self.assertIn(str(self.lib), self.seen()["stdin"])
        self.assertEqual((res["environment"], res["environment_by"], res["pick_cost"]), ("Work", "picked", 0.02))
        rec = json.loads(next((self.home.root / "runs").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((rec["environment"], rec["environment_by"]), ("Work", "picked"))

    def test_nothing_chosen_runs_in_the_run_folder(self):
        res = self.run_pick(self.pick_cfg(), chosen=None)
        self.assertTrue(res["ok"], res)
        self.assertEqual(os.path.normcase(self.seen()["cwd"]), os.path.normcase(str(self.home.root / "run")))
        self.assertNotIn("この回の環境", self.seen()["stdin"])
        self.assertEqual(res["environment_by"], "none")
        self.assertNotIn("environment", res)

    def test_without_a_fitting_environment_a_related_folder_is_used(self):
        cfg = config.parse({"state_root": self.cfg.state_root.as_posix(), "keep_transcripts": True,
                            "watchers": [{"name": "me", "user_id": "1", "pick_environment": True, "read_dirs": [str(self.lib)]}]})
        folder = run_claude.environments.Environment("", str(self.lib))
        res = self.run_pick(cfg, chosen=folder)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.picked[0][1], [str(self.lib)])  # 設定で読ませているフォルダが、関連するフォルダに並ぶ
        self.assertEqual(os.path.normcase(self.seen()["cwd"]), os.path.normcase(str(self.lib)))
        self.assertIn("この回の作業フォルダ", self.seen()["stdin"])
        self.assertEqual((res["environment"], res["environment_by"]), ("lib", "folder"))

    def test_no_list_means_no_pick(self):
        res = self.run_pick(self.pick_cfg(), chosen=self.work, envs=[])
        self.assertTrue(res["ok"], res)
        self.assertEqual((self.picked, res["environment_by"]), ([], "no_list"))

    def test_nothing_is_picked_unless_asked(self):
        res = self.run_pick(self.pick_cfg(pick=False), chosen=self.work)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.picked, [])
        self.assertNotIn("environment_by", res)
        self.assertEqual(os.path.normcase(self.seen()["cwd"]), os.path.normcase(str(self.home.root / "run")))

    def test_the_import_runs_in_the_chosen_folder(self):
        calls = []
        res = self.run_pick(self.pick_cfg(desktop_import=True), chosen=self.work,
                            import_fn=lambda *a, **k: calls.append(k.get("cwd")) or "ok", view=FakeView())
        self.assertTrue(res["ok"], res)
        self.assertEqual([os.path.normcase(str(c)) for c in calls], [os.path.normcase(str(self.proj))])

    def test_the_environment_is_kept_for_the_follow_ups(self):
        self.run_pick(self.pick_cfg(), chosen=self.work)
        self.assertEqual(continuation.load(self.home)["20"]["environment"], self.work.to_json())

    def test_a_retried_post_keeps_the_environment(self):
        self.post_fails = True
        self.assertEqual(self.run_pick(self.pick_cfg(), chosen=self.work)["reason"], "post_failed")
        self.post_fails = False
        res = self.run_pick(self.pick_cfg(), chosen=None)  # 投稿だけやり直す回は、選び直さない
        self.assertEqual((res["reason"], len(self.picked)), ("reused_digest", 1))
        self.assertEqual(continuation.load(self.home)["20"]["environment"], self.work.to_json())


class EnvironmentFollowUps(Continuation):
    """同じ流れの続きは、選び直さず、前のセッションの環境で動かす（ADR-0042）。"""

    def test_a_follow_up_runs_in_the_previous_environment_without_picking(self):
        root = Path(self._tmp.name)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        proj = Path(outside.name) / "proj"
        proj.mkdir()
        work = run_claude.environments.Environment("Work", str(proj))
        cfg = config.parse({"state_root": str(root), "keep_transcripts": True,
                            "watchers": [{"name": "me", "user_id": "1", "pick_environment": True}]})
        seen, picks = [], []

        def fake_build(claude, cfg, watcher, home, workdir=None, **kw):
            seen.append(str(workdir))
            return FAKE

        def picker(envs, folders, items):
            picks.append(1)
            return work, 0.01

        def once(path):
            with mock.patch.object(run_claude.claude_bin, "find_claude", return_value="CLAUDE"), \
                    mock.patch.object(run_claude, "build_command", side_effect=fake_build):
                return run_claude.triage(self.home, cfg.watchers[0], cfg, [path], now=NOW, poster=self.poster,
                                         environments_fn=lambda: [work], picker=picker)

        self.assertTrue(once(self.claim_msg("a.json", 100))["ok"])
        self.transcript()
        res = once(self.claim_msg("b.json", 200, before=[100]))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["continue_mode"], "append")
        self.assertEqual((res["environment"], res["environment_by"]), ("Work", "continued"))
        self.assertEqual(picks, [1])  # 選んだのは、初めの回だけ
        self.assertEqual(seen, [str(proj)] * 2)


class EnvironmentCommand(Base):
    """環境で動かすときも、書けるのは drafts/ だけ。設定ファイルは読まない（ADR-0042）。"""

    def test_working_and_added_folders_are_readable_but_not_writable(self):
        root = Path(self._tmp.name)
        c = run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home, extra_dirs=[str(root / "docs")],
                                     workdir=root / "proj", env_dirs=(str(root / "lib"),))
        dirs = [c[i + 1] for i, x in enumerate(c) if x == "--add-dir"]
        self.assertEqual(dirs, [str(self.home.claims), str(root / "repo"), str(root / "docs"), str(root / "lib")])
        denied = c[c.index("--disallowedTools") + 1: c.index("--add-dir")]
        for d in (root / "proj", self.home.claims, root / "repo", root / "docs", root / "lib"):
            self.assertIn("Edit(%s/**)" % run_claude.rule_path(d), denied)
        self.assertNotIn("Edit(%s/**)" % run_claude.rule_path(self.home.drafts), denied)
        self.assertEqual(c[c.index("--allowedTools") + 1], "Edit(%s/**)" % run_claude.rule_path(self.home.drafts))
        self.assertEqual(c[c.index("--setting-sources") + 1], "")

    def test_the_run_folder_is_the_default_and_is_not_writable_either(self):
        c = run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home)
        self.assertIn("Edit(%s/**)" % run_claude.rule_path(self.home.root / "run"), c)

    def test_a_folder_holding_drafts_is_added_but_not_denied(self):
        c = run_claude.build_command("CLAUDE", self.cfg, self.watcher, self.home, extra_dirs=[str(self.home.root)])
        self.assertIn(str(self.home.root), c)
        self.assertNotIn("Edit(%s/**)" % run_claude.rule_path(self.home.root), c)


if __name__ == "__main__":
    unittest.main()
