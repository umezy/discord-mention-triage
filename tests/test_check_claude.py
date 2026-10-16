import dataclasses
import json
import os
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pipeline"))

import check_claude  # noqa: E402
import run_claude  # noqa: E402
from watcher import config  # noqa: E402

from tests.test_run_claude import FAKE, Base  # noqa: E402


class CheckClaude(Base):
    """Claude を本番と同じ形で1回だけ動かす確認。`--isolated` は、`claude` のログインに頼らずに動くかを確かめる。"""

    def setUp(self):
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self._env.stop()

    def check(self, **kw):
        return check_claude.check(self.home, self.watcher, self.cfg, command=FAKE, **kw)

    def pin_token(self):
        (self.home.root / ".env").write_text(
            "DIGEST_WEBHOOK=http://example.invalid/hook\nCLAUDE_CODE_OAUTH_TOKEN=tok-claude\n", encoding="utf-8")

    def test_reports_which_credential_claude_ran_with(self):
        res = self.check()
        self.assertTrue(res["ok"], res)
        self.assertEqual(check_claude.format_line(res), "OK auth=cli_login isolated=0 model=haiku")
        self.pin_token()
        line = check_claude.format_line(self.check())
        self.assertEqual(line, "OK auth=token isolated=0 model=haiku")
        self.assertNotIn("tok-claude", line)

    def test_isolated_hides_the_stored_login_but_still_passes_the_pinned_token(self):
        self.pin_token()
        out = Path(self._tmp.name) / "env.json"
        os.environ["FAKE_CLAUDE_ARGV_OUT"] = str(out)
        self.assertTrue(self.check()["ok"])
        self.assertIsNone(json.loads(out.read_text(encoding="utf-8"))["config_dir"])  # ふだんは設定フォルダを変えない
        res = self.check(isolated=True)
        self.assertTrue(res["ok"], res)
        rec = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(rec["config_dir"])
        self.assertFalse(Path(rec["config_dir"]).exists())  # 空の一時フォルダで、終われば消える
        self.assertEqual(rec["oauth_token"], "tok-claude")
        self.assertEqual(check_claude.format_line(res), "OK auth=token isolated=1 model=haiku")

    def test_a_refusal_is_reported_with_its_reason_and_the_error_text(self):
        os.environ["FAKE_CLAUDE_MODE"] = "notstarted"
        res = self.check()
        self.assertFalse(res["ok"])
        line = check_claude.format_line(res)
        self.assertTrue(line.startswith(
            "FAILED auth=cli_login isolated=0 model=haiku reason=claude_not_started error=Your organization has disabled"), line)
        self.assertIn("403 oauth_not_allowed_for_organization", line)
        os.environ["FAKE_CLAUDE_MODE"] = "autherr"
        self.assertEqual(self.check()["reason"], "claude_auth")
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        self.assertEqual(self.check()["reason"], "claude_exit_1")

    def test_nothing_is_recorded_posted_or_queued(self):
        os.environ["FAKE_CLAUDE_MODE"] = "notstarted"
        self.check()
        self.assertEqual(self.posts, [])
        self.assertFalse((self.home.root / "runs").exists())
        self.assertEqual(run_claude.blocked_state(self.home), (0.0, ""))

    def test_the_command_is_the_production_one_with_a_small_model_and_no_transcript(self):
        keep = config.parse({"state_root": self.cfg.state_root.as_posix(), "keep_transcripts": True,
                             "watchers": [{"name": "me", "user_id": "1", "triage_model": "opus", "triage_effort": "xhigh"}]})
        prod = run_claude.build_command("CLAUDE", keep, keep.watchers[0], self.home)
        chk = check_claude.check_command("CLAUDE", keep, keep.watchers[0], self.home)
        self.assertEqual(chk[chk.index("--model") + 1], "haiku")
        self.assertNotIn("--effort", chk)
        self.assertIn("--no-session-persistence", chk)  # 確認の記録は残さない
        self.assertNotIn("--bare", chk)
        # 権限まわりは本番と同じ（確認で通ったのに本番で通らない、を作らない）
        for flag in ("--tools", "--permission-mode", "--permission-prompts", "--setting-sources", "--allowedTools"):
            self.assertEqual(chk[chk.index(flag) + 1], prod[prod.index(flag) + 1], flag)
        self.assertEqual(chk[:3], prod[:3])
        # 取り込む設定（自動モード。ADR-0033）でも、確認は本番と同じモードで動く
        imp = dataclasses.replace(keep, desktop_import=True)
        prod = run_claude.build_command("CLAUDE", imp, imp.watchers[0], self.home)
        chk = check_claude.check_command("CLAUDE", imp, imp.watchers[0], self.home)
        self.assertEqual(chk[chk.index("--permission-mode") + 1], "auto")
        self.assertEqual(chk[chk.index("--permission-mode") + 1], prod[prod.index("--permission-mode") + 1])
