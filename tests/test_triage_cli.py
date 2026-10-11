"""ただ 1 つのコマンド（triage.py）のテスト。Discord にも Claude にも繋がない（外との接点は、偽物に差し替える）。"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "pipeline"))

import common  # noqa: E402
import triage  # noqa: E402
from watcher import config, search  # noqa: E402
from watcher.store import Home  # noqa: E402

USER_ID, CHANNEL_ID = "123456789012345678", "987654321098765432"
BOT_TOKEN = "BOT-TOKEN-MUST-NOT-LEAK-0123456789abcdef"
WEBHOOK = "https://discord.com/api/webhooks/1234567890/WEBHOOK-MUST-NOT-LEAK_abc"
CLAUDE_TOKEN = "CLAUDE-TOKEN-MUST-NOT-LEAK-0123456789abcdef"
SECRETS = (BOT_TOKEN, WEBHOOK, CLAUDE_TOKEN)


HOME_VARS = ("USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # ホームフォルダを、一時フォルダに差し替える。init が書く設定は、置き場を書かない（既定の ~/.discord-mention-triage）ので、
        # 差し替えないと、テストが、この PC の本物の置き場（鍵がある）を読み書きしてしまう
        self._home_env = {k: os.environ.get(k) for k in HOME_VARS}
        for k in HOME_VARS:
            os.environ.pop(k, None)
        os.environ["USERPROFILE"] = os.environ["HOME"] = self._tmp.name
        self.root = Path(self._tmp.name) / ".discord-mention-triage"
        self.lines, self.asked, self.asked_secret = [], [], []
        self.answers, self.secret_answers = [], []
        self.poll_result = search.PollResult()
        self.claude_result = {"ok": True, "auth": "token", "isolated": False, "reason": "done", "error": ""}
        self.tasks = {}
        self.calls = {"poll": [], "claude": 0, "post": 0, "powershell": []}
        self.now = time.time()
        self._old_token = os.environ.pop("DISCORD_BOT_TOKEN", None)
        self._old_cfg = os.environ.pop("MENTION_TRIAGE_CONFIG", None)

    def tearDown(self):
        for key, old in (("DISCORD_BOT_TOKEN", self._old_token), ("MENTION_TRIAGE_CONFIG", self._old_cfg)):
            if old is not None:
                os.environ[key] = old
        for k, old in self._home_env.items():
            os.environ.pop(k, None)
            if old is not None:
                os.environ[k] = old
        self._tmp.cleanup()

    def test_the_home_folder_is_the_temporary_one(self):
        self.assertEqual(Path.home(), Path(self._tmp.name))
        self.assertEqual(triage.Deps().state_root, self.root)
        self.assertEqual(config.parse({"watchers": [{"name": "a", "user_id": "1"}]}).state_root, self.root)

    def deps(self):
        def ask(prompt):
            self.asked.append(prompt)
            return self.answers.pop(0) if self.answers else ""

        def ask_secret(prompt):
            self.asked_secret.append(prompt)
            return self.secret_answers.pop(0) if self.secret_answers else ""

        def poll(cfg, w, home, token):
            self.calls["poll"].append(token)
            return self.poll_result

        def claude_check(home, w, cfg):
            self.calls["claude"] += 1
            return self.claude_result

        def post_test(home):
            self.calls["post"] += 1
            return True, "OK chunks=1 mention=42"

        def powershell(args):
            self.calls["powershell"].append(args)
            return 0

        return triage.Deps(ask=ask, ask_secret=ask_secret, out=self.lines.append, state_root=self.root, legacy_configs=(),
                           poll=poll, claude_check=claude_check, post_test=post_test, task_info=self.tasks.get,
                           powershell=powershell, now=lambda: self.now)

    def run_cli(self, *argv):
        self.lines.clear()
        return triage.main(list(argv), deps=self.deps())

    @property
    def text(self):
        return "\n".join(self.lines)

    def init(self, answers=("me", USER_ID, CHANNEL_ID, ""), secrets=SECRETS, *argv):
        self.answers, self.secret_answers = list(answers), list(secrets)
        return self.run_cli("init", *argv)


class Init(Base):
    def test_answers_become_one_folder_with_config_and_two_key_files(self):
        self.assertEqual(self.init(), 0)
        cfg = config.load(str(self.root / "config.json"))
        w = cfg.watchers[0]
        self.assertEqual((w.name, w.user_id, w.channel_blocklist), ("me", int(USER_ID), frozenset({int(CHANNEL_ID)})))
        self.assertEqual(cfg.state_root, self.root)  # 置き場は書かない（既定の ~/.discord-mention-triage のまま）
        self.assertNotIn("state_root", json.loads((self.root / "config.json").read_text(encoding="utf-8")))
        self.assertFalse(cfg.desktop_import)  # デスクトップ版が無い PC でも、そのまま動く
        self.assertTrue(cfg.keep_transcripts)
        self.assertFalse(w.pr_policy.allowed_orgs)  # PR の条件は、空（何も満たさない）で作る
        bot = (self.root / ".env").read_text(encoding="utf-8")
        mine = (self.root / "me" / ".env").read_text(encoding="utf-8")
        self.assertIn("DISCORD_BOT_TOKEN=%s\n" % BOT_TOKEN, bot)
        self.assertIn("DIGEST_WEBHOOK=%s\n" % WEBHOOK, mine)
        self.assertIn("DIGEST_MENTION_USER_ID=%s\n" % USER_ID, mine)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=%s\n" % CLAUDE_TOKEN, mine)
        for text in (bot, mine):
            self.assertTrue(text.startswith("# "))
        self.assertFalse((self.root / ".env").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_keys_are_asked_without_echo_and_never_printed(self):
        self.init()
        self.assertEqual(len(self.asked_secret), 3)
        self.assertEqual(len(self.asked), 4)
        for secret in SECRETS:
            self.assertNotIn(secret, self.text)
            self.assertNotIn(secret, (self.root / "config.json").read_text(encoding="utf-8"))

    def test_the_written_files_work_with_the_rest_of_the_system(self):
        self.init()
        cfg = config.load(str(self.root / "config.json"))
        home = Home(cfg.watchers[0].home)
        self.assertEqual(config.load_token(cfg), BOT_TOKEN)
        url, mention, _ = triage.notify.read_target(home)
        self.assertEqual((url, mention), (WEBHOOK, USER_ID))
        self.assertEqual(triage.run_claude.child_env(home)["CLAUDE_CODE_OAUTH_TOKEN"], CLAUDE_TOKEN)

    def test_default_name_and_optional_read_dir(self):
        with mock.patch("getpass.getuser", return_value="Taro"):
            self.assertEqual(self.init(answers=("", USER_ID, CHANNEL_ID, str(self.root.parent))), 0)
        data = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["watchers"][0]["name"], "taro")
        self.assertEqual(data["watchers"][0]["read_dirs"], [str(self.root.parent)])
        self.assertTrue((self.root / "taro" / ".env").exists())

    def test_keys_can_be_skipped_and_the_gaps_are_named(self):
        self.assertEqual(self.init(secrets=("", "", "")), 0)
        mine = (self.root / "me" / ".env").read_text(encoding="utf-8")
        self.assertIn("\nDIGEST_WEBHOOK=\n", mine)
        self.assertIn("まだ書いていない鍵: ボットのトークン、Webhook の URL、Claude の合鍵", self.text)
        self.assertEqual(triage.notify.read_target(Home(self.root / "me"))[0], None)  # 空の行は「無い」と読まれる

    def test_a_bad_answer_is_asked_again_and_three_bad_answers_write_nothing(self):
        self.assertEqual(self.init(answers=("me", "not-a-number", USER_ID, CHANNEL_ID, "")), 0)
        self.assertEqual(len(self.asked), 5)

        self.root.joinpath("config.json").unlink()
        for f in (self.root / ".env", self.root / "me" / ".env"):
            f.unlink()
        self.assertEqual(self.init(answers=("me", "x", "y", "z")), 2)
        self.assertFalse((self.root / "config.json").exists())
        self.assertFalse((self.root / ".env").exists())

    def test_ids_cannot_be_skipped(self):
        self.assertEqual(self.init(answers=("me", "", CHANNEL_ID, "")), 2)
        self.assertFalse((self.root / "config.json").exists())

    def test_a_webhook_that_is_not_a_discord_webhook_is_rejected(self):
        code = self.init(secrets=(BOT_TOKEN, "https://evil.example/api/webhooks/1/x", "http://discord.com/api/webhooks/1/x",
                                  "nope"))
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "config.json").exists())

    def test_existing_setup_is_not_overwritten(self):
        self.init()
        before = (self.root / "config.json").read_text(encoding="utf-8")
        self.assertEqual(self.init(answers=("other", USER_ID, CHANNEL_ID, "")), 1)
        self.assertEqual((self.root / "config.json").read_text(encoding="utf-8"), before)
        self.assertIn("--force", self.text)

    def test_force_keeps_the_old_config_and_never_touches_or_asks_for_existing_keys(self):
        self.init()
        keys_before = (self.root / ".env").read_bytes(), (self.root / "me" / ".env").read_bytes()
        self.asked_secret.clear()
        code = self.init(("me", USER_ID, "111111111111111111", ""), ("NEW-TOKEN-THAT-MUST-NOT-BE-WRITTEN-123456",), "--force")
        self.assertEqual(code, 0)
        self.assertEqual(self.asked_secret, [])  # 鍵のファイルがあるので、聞かない
        self.assertEqual(((self.root / ".env").read_bytes(), (self.root / "me" / ".env").read_bytes()), keys_before)
        self.assertEqual(len(list(self.root.glob("config.json.bak-*"))), 1)
        self.assertEqual(json.loads((self.root / "config.json").read_text(encoding="utf-8"))["watchers"][0]["channel_blocklist"],
                         ["111111111111111111"])
        self.assertEqual(self.text.count("すでにあったので、触っていません"), 2)

    def test_no_input_asks_nothing_and_leaves_blanks_to_fill_in_notepad(self):
        # セットアップのスキル（.claude/skills/triage-setup）が使う形。ID は引数で渡し、鍵は、人がメモ帳で書く
        code = self.init(("ignored",), ("MUST-NOT-BE-ASKED-0123456789abcdef0123",), "--name", "me", "--user-id", USER_ID,
                         "--channel-id", CHANNEL_ID, "--no-input")
        self.assertEqual(code, 0)
        self.assertEqual((self.asked, self.asked_secret), ([], []))
        self.assertIn("\nDISCORD_BOT_TOKEN=\n", (self.root / ".env").read_text(encoding="utf-8"))
        mine = (self.root / "me" / ".env").read_text(encoding="utf-8")
        self.assertIn("\nDIGEST_WEBHOOK=\n", mine)
        self.assertIn("\nCLAUDE_CODE_OAUTH_TOKEN=\n", mine)
        self.assertIn("DIGEST_MENTION_USER_ID=%s\n" % USER_ID, mine)
        self.assertIn("まだ書いていない鍵: ボットのトークン、Webhook の URL、Claude の合鍵", self.text)
        self.assertIn('notepad "%s"' % (self.root / ".env"), self.text)
        self.assertIn('notepad "%s"' % (self.root / "me" / ".env"), self.text)
        self.assertEqual(self.text.count("notepad "), 2)  # 同じファイルは、1 回だけ出す
        self.assertEqual(json.loads((self.root / "config.json").read_text(encoding="utf-8"))["watchers"][0]["read_dirs"], [])

    def test_no_input_uses_the_default_name_and_needs_the_two_ids(self):
        with mock.patch("getpass.getuser", return_value="Taro"):
            self.assertEqual(self.init((USER_ID, CHANNEL_ID), (), "--no-input"), 2)  # 答えがあっても、聞かない
            self.assertIn("--user-id", self.text)
            self.assertFalse((self.root / "config.json").exists())
            self.assertEqual(self.init((), (), "--user-id", USER_ID, "--channel-id", CHANNEL_ID, "--read-dir",
                                       str(self.root.parent), "--no-input"), 0)
        data = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual((data["watchers"][0]["name"], data["watchers"][0]["read_dirs"]), ("taro", [str(self.root.parent)]))
        self.assertEqual((self.asked, self.asked_secret), ([], []))

    def test_no_input_never_touches_key_files_that_already_exist(self):
        self.init()
        keys_before = (self.root / ".env").read_bytes(), (self.root / "me" / ".env").read_bytes()
        code = self.init((), (), "--force", "--name", "me", "--user-id", USER_ID, "--channel-id", CHANNEL_ID, "--no-input")
        self.assertEqual(code, 0)
        self.assertEqual(((self.root / ".env").read_bytes(), (self.root / "me" / ".env").read_bytes()), keys_before)
        self.assertNotIn("notepad", self.text)
        self.assertNotIn("まだ書いていない鍵", self.text)

    def test_closed_input_is_an_empty_answer_not_a_crash(self):
        # 答えを打つ人がいない所（Claude Code の中など）から呼ばれても、エラーで止まらず、足りないものを言う
        deps = self.deps()

        def closed(prompt):
            raise EOFError

        deps.ask = deps.ask_secret = closed
        self.assertEqual(triage.main(["init"], deps=deps), 2)
        self.assertIn("省けません", self.text)
        self.assertFalse((self.root / "config.json").exists())

        self.assertEqual(triage.main(["init", "--name", "me", "--user-id", USER_ID, "--channel-id", CHANNEL_ID], deps=deps), 0)
        self.assertIn("まだ書いていない鍵", self.text)
        self.assertTrue((self.root / "me" / ".env").exists())

    def test_the_folder_cannot_be_inside_the_repository(self):
        deps = self.deps()
        deps.state_root = REPO / "state"
        self.assertEqual(triage.main(["init"], deps=deps), 2)
        self.assertFalse((REPO / "state").exists())


class Check(Base):
    def setUp(self):
        super().setUp()
        self.init()

    def test_everything_ok(self):
        self.assertEqual(self.run_cli("check"), 0)
        self.assertEqual([ln[:4] for ln in self.lines[:6]], ["[OK]"] * 5 + ["[--]"])
        self.assertIn("すべて OK です。", self.text)
        self.assertEqual(self.calls["poll"], [BOT_TOKEN])
        self.assertEqual((self.calls["claude"], self.calls["post"]), (1, 0))
        for secret in SECRETS:
            self.assertNotIn(secret, self.text)

    def test_no_setup_yet_points_to_init(self):
        (self.root / "config.json").unlink()
        self.assertEqual(self.run_cli("check"), 1)
        self.assertIn("[NG] 設定:", self.text)
        self.assertIn("python triage.py init", self.text)
        self.assertEqual(self.calls["poll"], [])

    def test_unfilled_placeholder_is_explained_in_words(self):
        data = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        data["watchers"][0]["user_id"] = "REPLACE_WITH_YOUR_DISCORD_USER_ID"
        (self.root / "config.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_cli("check"), 1)
        self.assertIn("user_id が数値でない", self.text)

    def test_missing_bot_token_skips_the_discord_check(self):
        (self.root / ".env").write_text("# empty\nDISCORD_BOT_TOKEN=\n", encoding="utf-8")
        self.assertEqual(self.run_cli("check"), 1)
        self.assertIn("[NG] ボットのトークン: 無い", self.text)
        self.assertEqual(self.calls["poll"], [])

    def test_unreadable_discord_gets_a_hint_for_its_reason(self):
        for reason, hint in (("http_401_guilds", "トークンが違う"), ("network_guilds", "ネットワーク"),
                             ("http_403_search", "権限が無い"), ("http_500_search", "止まったとき")):
            with self.subTest(reason):
                self.poll_result = search.PollResult()
                self.poll_result.fail(reason)
                self.assertEqual(self.run_cli("check"), 1)
                self.assertIn("[NG] Discord を読めない: %s" % reason, self.text)
                self.assertIn(hint, self.text)

    def test_missing_webhook_and_old_key_name(self):
        env = self.root / "me" / ".env"
        env.write_text("# x\nCLAUDE_CODE_OAUTH_TOKEN=%s\n" % CLAUDE_TOKEN, encoding="utf-8")
        self.assertEqual(self.run_cli("check"), 1)
        self.assertIn("[NG] Webhook: 無い", self.text)
        env.write_text("# x\nDISCORD_WATCH_WEBHOOK=%s\n" % WEBHOOK, encoding="utf-8")
        self.assertEqual(self.run_cli("check"), 0)  # 古い名前でも、いまは読める
        self.assertIn("古い名前（DISCORD_WATCH_WEBHOOK）", self.text)

    def test_claude_on_the_stored_login_passes_with_advice(self):
        self.claude_result = dict(self.claude_result, auth="cli_login")
        self.assertEqual(self.run_cli("check"), 0)
        self.assertIn("専用の合鍵が無い", self.text)

    def test_claude_failure_is_ng_with_what_to_do(self):
        self.claude_result = {"ok": False, "auth": "cli_login", "isolated": False, "reason": "claude_auth",
                              "error": "Failed to authenticate: OAuth session expired"}
        self.assertEqual(self.run_cli("check"), 1)
        self.assertIn("[NG] Claude: 動かなかった（claude_auth）", self.text)
        self.assertIn("claude setup-token", self.text)
        self.assertIn("NG が 1 件", self.text)

    def test_no_claude_flag_spends_nothing(self):
        self.assertEqual(self.run_cli("check", "--no-claude"), 0)
        self.assertEqual(self.calls["claude"], 0)

    def test_post_test_posts_only_when_asked(self):
        self.assertEqual(self.run_cli("check", "--post-test"), 0)
        self.assertEqual(self.calls["post"], 1)
        self.assertIn("確認用の投稿", self.text)

    def test_registered_task_is_reported(self):
        self.tasks["mention-triage"] = {"State": "Ready"}
        self.run_cli("check")
        self.assertIn("[OK] 自動起動: 登録済み（Ready）", self.text)

    def test_the_default_discord_check_is_a_dry_run(self):
        seen = {}

        def fake_poll_once(cfg, w, home, token, **kw):
            seen.update(kw)
            return search.PollResult()

        with mock.patch.object(search, "poll_once", fake_poll_once):
            cfg = config.load(str(self.root / "config.json"))
            triage.Deps().poll(cfg, cfg.watchers[0], Home(self.root / "me"), "t")
        self.assertEqual(seen, {"dry_run": True})


class Status(Base):
    def setUp(self):
        super().setUp()
        self.init()
        self.home = Home(self.root / "me").ensure()

    def run_status(self, *argv):
        return self.run_cli("status", *argv)

    def test_fresh_setup(self):
        self.assertEqual(self.run_status(), 0)
        self.assertIn("自動起動: 登録されていない", self.text)
        self.assertIn("Discord: まだ 1 回も読んでいない", self.text)
        self.assertIn("件数: 未開封 0 / 仕分け中 0 / 失敗で退避 0 / 処理済み 0", self.text)

    def test_running_state(self):
        self.tasks["mention-triage"] = {"State": "Ready", "LastRunTime": "10/10/2026 00:10:00", "NextRunTime": "10/10/2026 00:12:00"}
        self.tasks["mention-triage-watcher"] = {"State": "Running"}
        self.home.write_poll({"since": self.now - 90, "last_ok": self.now - 90, "fail_since": None})
        (self.home.inbox / "pending-a.json").write_text("{}", encoding="utf-8")
        (self.home.failed / "pending-b.json").write_text("{}", encoding="utf-8")
        (self.root / "dispatch.log").write_text(
            "\n".join("2026-10-10 00:0%d:00 exit=0 OK POLL=ok FOUND=1 EMITTED=1 CLAIMED=1 RAN=1 RESULT=done" % i for i in range(8)),
            encoding="ascii")
        self.assertEqual(self.run_status("--lines", "2"), 0)
        self.assertIn("自動起動: 登録済み（状態 Ready", self.text)
        self.assertIn("古いタスクが残っている: mention-triage-watcher", self.text)
        self.assertIn("Discord: 読めている（最後に読めたのは 90 秒前）", self.text)
        self.assertIn("件数: 未開封 1 / 仕分け中 0 / 失敗で退避 1 / 処理済み 0", self.text)
        self.assertEqual(sum(1 for ln in self.lines if "RESULT=done" in ln), 2)

    def test_a_resident_watcher_that_is_still_running_is_pointed_out(self):
        (self.home.root / "heartbeat.json").write_text(json.dumps({"ts": self.now - 20}), encoding="utf-8")
        self.run_status()
        self.assertIn("常駐の受付（古い版）が、まだ動いている", self.text)
        self.assertEqual(self.run_cli("check"), 0)
        self.assertIn("[--] 古い版（常駐の受付）が、まだ動いている", self.text)
        (self.home.root / "heartbeat.json").write_text(json.dumps({"ts": self.now - 3600}), encoding="utf-8")
        self.run_status()
        self.assertNotIn("常駐の受付", self.text)

    def test_unreadable_discord_and_paused_claude(self):
        self.home.write_poll({"since": 1, "fail_since": self.now - 1800, "last_error": "http_401_guilds"})
        (self.home.root / "runs").mkdir()
        (self.home.root / "runs" / "blocked.json").write_text(
            json.dumps({"reason": "claude_auth", "until": self.now + 300}), encoding="utf-8")
        self.run_status()
        self.assertIn("Discord: 読めていない（30 分前 から。理由 http_401_guilds）", self.text)
        self.assertIn("トークンが違う", self.text)
        self.assertIn("Claude: 止めている（claude_auth", self.text)


class RunAndInstall(Base):
    def setUp(self):
        super().setUp()
        self.init()

    def test_run_is_dispatch_with_the_found_config(self):
        with mock.patch.object(triage.dispatch, "main", return_value=0) as m:
            self.assertEqual(self.run_cli("run", "--dry-run", "--backfill-hours", "6"), 0)
        self.assertEqual(m.call_args[0][0], ["--user", "me", "--config", str(self.root / "config.json"), "--dry-run",
                                             "--backfill-hours", "6.0"])

    def test_install_calls_the_one_installer_with_execution_policy_handled_by_the_caller(self):
        self.assertEqual(self.run_cli("install", "--interval-minutes", "5"), 0)
        (args,) = self.calls["powershell"]
        self.assertEqual(args[:2], ["-File", str(REPO / "scripts" / "install-task.ps1")])
        self.assertEqual(args[2:], ["-User", "me", "-IntervalMinutes", "5", "-Config", str(self.root / "config.json")])
        self.assertIn("登録しました", self.text)

    def test_a_failed_registration_is_said_out_loud(self):
        deps = self.deps()
        deps.powershell = lambda args: 1
        self.lines.clear()
        self.assertEqual(triage.main(["install"], deps=deps), 1)
        self.assertIn("登録できませんでした", self.text)
        self.assertNotIn("登録しました", self.text)

    def test_uninstall(self):
        self.assertEqual(self.run_cli("uninstall"), 0)
        self.assertEqual(self.calls["powershell"], [["-File", str(REPO / "scripts" / "install-task.ps1"), "-Uninstall"]])

    def test_powershell_is_started_with_bypass_so_a_new_pc_does_not_refuse_the_script(self):
        with mock.patch.object(triage.os, "name", "nt"), mock.patch.object(triage.subprocess, "run") as run:
            run.return_value.returncode = 0
            triage.run_powershell(["-File", "x.ps1"])
        self.assertEqual(run.call_args[0][0], ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "x.ps1"])


class ConfigLocation(unittest.TestCase):
    def test_the_home_folder_wins_over_the_old_place_in_the_repository(self):
        with tempfile.TemporaryDirectory() as d:
            home, repo = Path(d) / "home", Path(d) / "repo"
            (home / ".discord-mention-triage").mkdir(parents=True)
            (repo / "config").mkdir(parents=True)
            old = os.environ.pop("MENTION_TRIAGE_CONFIG", None)
            try:
                with mock.patch.object(config.Path, "home", return_value=home), mock.patch.object(config, "REPO_ROOT", repo):
                    with self.assertRaises(config.ConfigError):
                        config.find_config_path()
                    (repo / "config" / "config.json").write_text("{}", encoding="utf-8")
                    self.assertEqual(config.find_config_path(), repo / "config" / "config.json")  # 古い置き場も、まだ読める
                    (home / ".discord-mention-triage" / "config.json").write_text("{}", encoding="utf-8")
                    self.assertEqual(config.find_config_path(), home / ".discord-mention-triage" / "config.json")
            finally:
                if old is not None:
                    os.environ["MENTION_TRIAGE_CONFIG"] = old

    def test_the_ascii_line_keeps_the_setting_name(self):
        hint = common.ascii_hint(config.ConfigError("watchers[taro].user_id が数値でない"))
        self.assertEqual(hint, "watchers[taro].user_id hint=run:python_triage.py_check")
        hint.encode("ascii")
        self.assertTrue(common.ascii_hint("日本語だけ").startswith("unreadable"))


if __name__ == "__main__":
    unittest.main()
