"""受付（watcher/search.py）の流れを、偽の Discord（tests/fake_discord.py）で確かめる結合テスト。

Discord には一切つながない。偽物は GET にだけ答え、GET 以外が来たら、その場で落とす。
"""

import json
import tempfile
import unittest
from pathlib import Path

from tests.fake_discord import (ALICE, BOB, BOT, DEV, DIGEST, FORUM, FORUM_POST, ME, MY_ROLE, NOW, OTHER_ROLE,
                                STAFF_ROLE, FakeDiscord, sf)
from watcher import config, search
from watcher.rest import ReadOnlyDiscord
from watcher.store import Home

TOKEN = "TOKEN-MUST-NOT-LEAK"
MINUTE, HOUR = 60, 3600


class Base(unittest.TestCase):
    overrides: dict = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = self.make_cfg()
        self.w = self.cfg.watchers[0]
        self.home = Home(self.w.home).ensure()
        self.fake = FakeDiscord()
        self.clock = NOW
        self.sleeps = []

    def tearDown(self):
        self._tmp.cleanup()

    def make_cfg(self, **watcher_kw):
        w = {
            "name": "me", "user_id": str(ME), "channel_blocklist": [str(DIGEST)], "settle_seconds": 60,  # 外した項目。残っていても読み飛ばす
            "pr_policy": {"allowed_orgs": ["ExampleOrg"], "requester_role_ids": [str(STAFF_ROLE)]},
        }
        w.update(watcher_kw)
        return config.parse({"state_root": str(self.root), "watchers": [w]})

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds

    def poll(self, w=None, **kw):
        api = ReadOnlyDiscord(TOKEN, opener=self.fake, sleep=self.sleep)
        return search.Poller(api, self.cfg, w or self.w, self.home, now=lambda: self.clock).run(**kw)

    def started(self, seconds_ago=10 * MINUTE):
        """前に 1 回動いたことにする（初回は「いまから」になり、過去の分を拾わないので）。"""
        self.home.write_poll({"since": self.clock - seconds_ago, "last_ok": self.clock - seconds_ago})

    def mention_now(self, seconds_ago, **kw):
        """進めたあとの時計から見て seconds_ago 秒前の、自分宛てのメンション（fake.mention は、時計を進める前が基準）。"""
        return self.fake.add(sf(seconds_ago, self.clock), "<@%d> お願いします" % ME, mentions=[ME], **kw)

    def inbox_items(self):
        out = []
        for p in sorted(self.home.inbox.glob("pending-*.json")):
            out += json.loads(p.read_text(encoding="utf-8"))["items"]
        return out


class BasicFlow(Base):
    def test_direct_mention_is_emitted_with_context_without_waiting(self):
        self.started()
        self.fake.add(sf(300), "先に話していたこと", author=BOB)
        self.fake.add(sf(200), "その続き", author=ALICE)
        mid = self.fake.mention(30, "これ確認お願いします", attachments=["spec.png"])
        self.fake.add(sf(10), "補足です", author=ALICE)

        res = self.poll()  # 投稿から 30 秒でも、見つけた回に出す（待つ時間は置かない）
        self.assertEqual((res.ok, res.found, res.emitted), (True, 1, 1))
        self.assertTrue(self.home.is_known(mid))
        (item,) = self.inbox_items()
        self.assertEqual(item["message_id"], str(mid))
        self.assertEqual((item["kind"], item["direct"], item["watcher"]), ("direct", True, "me"))
        self.assertEqual((item["guild_name"], item["channel_name"], item["thread_parent"]), ("Example", "dev", None))
        self.assertEqual(item["jump_url"], "https://discord.com/channels/1/%d/%d" % (DEV, mid))
        self.assertEqual(item["mention"]["text"], "@たろう これ確認お願いします")  # <@111> は表示名に置き換わる
        self.assertEqual(item["mention"]["author"], "アリス（開発）")  # サーバー内の名前
        self.assertEqual(item["mention"]["attachments"], ["spec.png"])
        self.assertEqual(item["mention"]["time"], "2026/10/09 11:59")
        self.assertEqual([m["text"] for m in item["context_before"]], ["先に話していたこと", "その続き"])  # 古い順
        self.assertEqual([m["text"] for m in item["context_after"]], ["補足です"])
        self.assertEqual(item["context_before"][0]["author"], "bob")  # 表示名が無ければアカウント名

    def test_the_same_mention_is_not_emitted_twice(self):
        self.started()
        self.fake.mention(120)
        self.assertEqual(self.poll().emitted, 1)
        for _ in range(3):
            self.clock += 120
            res = self.poll()
            self.assertEqual((res.found, res.emitted), (0, 0))  # 重ねて引くので検索には出るが、既出なので捨てる
        self.assertEqual(len(self.inbox_items()), 1)

    def test_what_is_not_a_mention_is_never_fetched(self):
        self.started()
        self.fake.add(sf(120), "ふつうの発言")
        self.fake.add(sf(110), "<@%d> 別の人宛て" % BOB, mentions=[BOB])
        res = self.poll()
        self.assertEqual((res.found, res.emitted), (0, 0))
        self.assertEqual(self.fake.count("/channels/"), 0)  # 検索に出ないものは、本体を取りにも行かない

    def test_own_message_is_ignored(self):
        self.started()
        self.fake.add(sf(120), "<@%d> 自分にメモ" % ME, author=ME, mentions=[ME])
        self.assertEqual(self.poll().found, 0)

    def test_reply_with_ping_is_a_direct_mention(self):
        self.started()
        mid = self.fake.add(sf(120), "返信です", mentions=[ME], reply_to=ME)
        self.assertEqual(self.poll().emitted, 1)
        self.assertEqual(self.inbox_items()[0]["message_id"], str(mid))

    def test_join_notices_and_other_system_messages_are_ignored(self):
        self.started()
        self.fake.add(sf(120), "", mentions=[ME], mtype=7)
        self.assertEqual(self.poll().found, 0)


class Roles(Base):
    def test_role_mention_uses_the_roles_the_user_has(self):
        self.started()
        mine = self.fake.add(sf(200), "<@&%d> 集合" % MY_ROLE, roles=[MY_ROLE])
        self.fake.add(sf(190), "<@&%d> 集合" % OTHER_ROLE, roles=[OTHER_ROLE])
        self.assertEqual(self.poll().emitted, 1)
        (item,) = self.inbox_items()
        self.assertEqual((item["message_id"], item["kind"], item["direct"]), (str(mine), "role", False))
        self.assertEqual(item["mention"]["text"], "@Nebula 集合")

    def test_everyone_is_off_unless_configured(self):
        self.started()
        self.fake.add(sf(120), "@everyone お知らせ", everyone=True)
        self.assertEqual(self.poll().found, 0)
        w = self.make_cfg(include_everyone=True).watchers[0]
        self.assertEqual(self.poll(w).emitted, 1)
        self.assertEqual(self.inbox_items()[0]["kind"], "everyone")

    def test_a_guild_the_user_is_not_in_is_not_searched(self):
        self.started()
        self.fake.members.pop(ME)
        self.fake.mention(120)
        res = self.poll()
        self.assertEqual((res.ok, res.found), (True, 0))
        self.assertEqual(self.fake.count("/messages/search"), 0)

    def test_guild_allowlist_limits_the_search(self):
        self.started()
        self.fake.mention(120)
        w = self.make_cfg(guild_allowlist=["42"]).watchers[0]
        self.assertEqual(self.poll(w).found, 0)
        self.assertEqual(self.fake.count("/messages/search"), 0)


class AlreadyHandled(Base):
    def test_already_replied_is_skipped(self):
        self.started()
        mid = self.fake.mention(300)
        self.fake.add(sf(200), "対応しました", author=ME)
        res = self.poll()
        self.assertEqual((res.emitted, res.skips), (0, {"replied": 1}))
        self.assertTrue(self.home.is_known(mid))

    def test_already_stamped_is_skipped(self):
        self.started()
        mid = self.fake.mention(300)
        self.fake.reactions[mid] = {"👍": [BOB, ME]}
        self.assertEqual(self.poll().skips, {"reacted": 1})
        self.assertEqual(self.inbox_items(), [])

    def test_someone_elses_stamp_does_not_count(self):
        self.started()
        mid = self.fake.mention(300)
        self.fake.reactions[mid] = {"👍": [BOB], "🎉": [ALICE]}
        self.assertEqual(self.poll().emitted, 1)

    def test_own_stamp_is_found_beyond_the_first_hundred_users(self):
        self.started()
        mid = self.fake.mention(300)
        self.fake.reactions[mid] = {"👍": list(range(1, 101)) + [ME]}  # 1 回に返るのは 100 人まで。本人は 101 人目
        self.assertEqual(self.poll().skips, {"reacted": 1})
        self.assertEqual(self.fake.count("/reactions/"), 2)

    def test_deleted_message_is_skipped(self):
        self.started()
        mid = self.fake.mention(300)
        self.fake.ghosts.add(mid)  # 検索には出たが、取りに行くと、もう無い
        self.assertEqual(self.poll().skips, {"deleted": 1})
        self.assertTrue(self.home.is_known(mid))

    def test_blocked_channel_is_counted_once_and_never_fetched(self):
        self.started()
        self.fake.mention(120, channel=DIGEST)  # ダイジェスト自身（本人へのメンションつき）を拾い直さない
        res = self.poll()
        self.assertEqual((res.found, res.skips), (0, {"blocked": 1}))
        self.assertEqual(self.fake.count("/channels/"), 0)
        self.clock += 120
        self.assertEqual(self.poll().skips, {})  # 重ねて引いた分は、数え直さない


class Threads(Base):
    def test_forum_post_carries_the_parent_name(self):
        self.started()
        self.fake.mention(120, channel=FORUM_POST)
        self.assertEqual(self.poll().emitted, 1)
        (item,) = self.inbox_items()
        self.assertEqual((item["channel_name"], item["thread_parent"]), ("困りごと", "相談フォーラム"))

    def test_blocking_the_parent_blocks_its_threads(self):
        self.started()
        self.fake.mention(120, channel=FORUM_POST)
        w = self.make_cfg(channel_blocklist=[str(FORUM)]).watchers[0]
        self.assertEqual(self.poll(w).skips, {"blocked": 1})


class PullRequests(Base):
    URL = "https://github.com/ExampleOrg/app/pull/12"

    def item_for(self, **kw):
        self.started()
        self.fake.mention(120, "レビューお願いします " + kw.pop("url", self.URL), **kw)
        self.assertEqual(self.poll().emitted, 1)
        return self.inbox_items()[0]

    def test_pr_from_staff_in_allowed_org_meets_the_policy(self):
        item = self.item_for()
        self.assertEqual([p["url"] for p in item["pull_requests"]], [self.URL])
        self.assertEqual(item["pull_requests_blocked"], [])

    def test_pr_from_non_staff_is_blocked(self):
        item = self.item_for(author=BOB)
        self.assertEqual(item["pull_requests"], [])
        self.assertEqual(item["pull_requests_blocked"][0]["reason"], "requester_not_allowed")

    def test_pr_in_other_org_is_blocked(self):
        item = self.item_for(url="https://github.com/evil/app/pull/1")
        self.assertEqual(item["pull_requests_blocked"][0]["reason"], "org_not_allowed")

    def test_pr_from_a_bot_or_webhook_is_blocked(self):
        item = self.item_for(author=BOT)
        self.assertEqual(item["pull_requests_blocked"][0]["reason"], "bot_requester")

    def test_when_the_requesters_roles_cannot_be_read_the_pr_is_blocked(self):
        # 判定できないときは、許可しない側に倒れる
        self.started()
        self.fake.mention(120, "レビューお願いします " + self.URL)
        self.home.write_poll(dict(self.home.read_poll(), directory={
            "at": self.clock, "guilds": self.fake.guilds, "roles": {"1": [MY_ROLE]}}))
        self.fake.fail["member"] = 500
        self.assertEqual(self.poll().emitted, 1)
        item = self.inbox_items()[0]
        self.assertEqual(item["pull_requests"], [])
        self.assertEqual(item["pull_requests_blocked"][0]["reason"], "requester_not_allowed")
        self.assertEqual(item["mention"]["author"], "Alice")  # 名前は、アカウントの表示名に倒れる


class WhereToStart(Base):
    def test_first_run_starts_from_now_without_backlog(self):
        self.fake.mention(2 * HOUR)
        res = self.poll()
        self.assertEqual((res.ok, res.first_run, res.found), (True, True, 0))
        self.assertEqual(self.home.read_poll()["since"], NOW)
        self.clock += 240
        new = self.mention_now(90)  # 動かし始めたあとに来た分は拾う
        self.assertEqual(self.poll().emitted, 1)
        self.assertEqual(self.inbox_items()[0]["message_id"], str(new))

    def test_later_runs_do_not_reach_back_past_the_first_run(self):
        # 2 回目以降は、少し手前から重ねて引く。それでも、動かし始める前の分までは戻らない
        before_start = self.fake.mention(10 * MINUTE)
        self.poll()
        self.clock += 5 * MINUTE
        after_start = self.mention_now(2 * MINUTE)
        res = self.poll()
        self.assertEqual(res.emitted, 1)
        self.assertEqual([i["message_id"] for i in self.inbox_items()], [str(after_start)])
        self.assertFalse(self.home.is_known(before_start))

    def test_backfill_on_a_later_run_reaches_back(self):
        old = self.fake.mention(3 * HOUR)
        self.poll()
        self.clock += 5 * MINUTE
        self.assertEqual(self.poll().emitted, 0)
        self.assertEqual(self.poll(backfill_hours=4).emitted, 1)
        self.assertEqual(self.inbox_items()[0]["message_id"], str(old))

    def test_backfill_hours_picks_up_older_mentions(self):
        self.fake.mention(2 * HOUR)
        self.fake.mention(8 * HOUR)
        self.assertEqual(self.poll(backfill_hours=6).emitted, 1)

    def test_switching_from_the_resident_watcher_continues_where_it_stopped(self):
        # 常駐版が最後に生きていた時刻（heartbeat.json）から引く。止めてから入れ替えるまでの分を、取りこぼさない
        (self.home.root / "heartbeat.json").write_text(json.dumps({"ts": NOW - 3 * HOUR, "ready": True}), encoding="utf-8")
        during = self.fake.mention(1 * HOUR)
        self.fake.mention(5 * HOUR)  # 常駐版が生きていた頃の分（向こうが処理している）
        res = self.poll()
        self.assertEqual((res.first_run, res.emitted), (True, 1))
        self.assertEqual(self.inbox_items()[0]["message_id"], str(during))

    def test_picks_up_what_arrived_while_the_pc_slept(self):
        self.started(seconds_ago=20 * HOUR)
        a, b = self.fake.mention(15 * HOUR), self.fake.mention(2 * HOUR)
        self.assertEqual(self.poll().emitted, 2)
        self.assertEqual({i["message_id"] for i in self.inbox_items()}, {str(a), str(b)})

    def test_ignores_what_is_older_than_the_catch_up_window(self):
        self.started(seconds_ago=200 * HOUR)
        self.fake.mention(100 * HOUR)  # 72 時間より前
        recent = self.fake.mention(10 * HOUR)
        self.assertEqual(self.poll().emitted, 1)
        self.assertEqual(self.inbox_items()[0]["message_id"], str(recent))

    def test_a_mention_indexed_late_is_still_picked_up(self):
        # 検索の索引は、投稿より遅れることがある。前回の続きより手前から、重ねて引いているので拾える
        self.started(seconds_ago=2 * MINUTE)
        late = self.fake.mention(10 * MINUTE)
        self.fake.unindexed.add(late)
        self.assertEqual(self.poll().found, 0)
        self.clock += 2 * MINUTE
        self.fake.unindexed.clear()
        self.assertEqual(self.poll().emitted, 1)

    def test_walks_past_one_page_of_hits(self):
        self.started(seconds_ago=5 * HOUR)
        for i in range(30):
            self.fake.mention(4 * HOUR - i * 60)
        res = self.poll()
        self.assertEqual((res.found, res.emitted), (30, 30))


class WhenDiscordCannotBeRead(Base):
    def test_a_failed_search_does_not_advance_the_position(self):
        self.started(seconds_ago=5 * MINUTE)
        before = self.home.read_poll()["since"]
        mid = self.fake.mention(3 * MINUTE)
        self.fake.fail["search"] = 500
        res = self.poll()
        self.assertEqual((res.ok, res.error, res.emitted), (False, "http_500_search", 0))
        state = self.home.read_poll()
        self.assertEqual((state["since"], state["fail_since"], state["last_error"]), (before, NOW, "http_500_search"))

        self.clock += 10 * MINUTE
        self.fake.fail.clear()
        res = self.poll()  # 直れば、止まっていた間の分も拾う
        self.assertEqual((res.ok, res.emitted), (True, 1))
        self.assertEqual(self.inbox_items()[0]["message_id"], str(mid))
        self.assertIsNone(self.home.read_poll()["fail_since"])

    def test_a_rejected_token_is_reported_without_echoing_it(self):
        self.fake.fail["guilds"] = 401
        res = self.poll()
        self.assertEqual((res.ok, res.error), (False, "http_401_guilds"))
        self.assertNotIn(TOKEN, json.dumps(self.home.read_poll()))

    def test_no_network_is_reported(self):
        def down(req, timeout=None):
            raise OSError("down")

        api = ReadOnlyDiscord(TOKEN, opener=down, sleep=self.sleep)
        res = search.Poller(api, self.cfg, self.w, self.home, now=lambda: self.clock).run()
        self.assertEqual((res.ok, res.error), (False, "network_guilds"))

    def test_index_not_ready_waits_then_gives_up_for_this_round(self):
        self.started()
        self.fake.mention(120)
        self.fake.queue = [(202, {"code": 110000, "retry_after": 2})] * 10
        res = self.poll()
        self.assertEqual((res.ok, res.error), (False, "http_202_search"))
        self.assertEqual(self.sleeps[:3], [2.0, 2.0, 2.0])

    def test_rate_limit_is_waited_out(self):
        self.started()
        self.fake.mention(120)
        self.fake.queue = [(429, {"retry_after": 0.8, "global": False})]
        res = self.poll()
        self.assertEqual((res.ok, res.emitted), (True, 1))
        self.assertEqual(self.sleeps, [0.8])

    def test_waits_before_the_limit_is_hit_instead_of_running_into_it(self):
        # 残り回数が 0 と言われたら、戻るまで待ってから次へ進む（実サーバーでは、前後の会話の取得が 5 秒に 5 回まで）
        self.started()
        self.fake.mention(5 * MINUTE)
        self.fake.headers["history"] = {"X-RateLimit-Limit": "5", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "4.2"}
        self.fake.headers["search"] = {"X-RateLimit-Limit": "6", "X-RateLimit-Remaining": "3", "X-RateLimit-Reset-After": "2.3"}
        res = self.poll()
        self.assertEqual((res.ok, res.emitted), (True, 1))
        self.assertEqual(self.sleeps, [4.2, 4.2])  # 前後の会話を 2 回取った。検索は、まだ残りがあるので待たない

    def test_an_absurd_wait_is_not_obeyed(self):
        self.started()
        self.fake.headers["search"] = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "3600"}
        self.assertTrue(self.poll().ok)
        self.assertEqual(self.sleeps, [])

    def test_one_unreadable_mention_is_retried_then_given_up(self):
        self.started()
        mid = self.fake.mention(5 * MINUTE)
        self.fake.fail["history"] = 500
        for attempt in range(1, search.MAX_EMIT_ATTEMPTS):
            res = self.poll()
            self.assertEqual((res.ok, res.emitted, res.retrying, res.skips), (True, 0, 1, {}), attempt)
            self.assertFalse(self.home.is_known(mid))
            self.assertEqual(self.home.read_poll()["retries"], {str(mid): attempt})
            self.clock += 2 * MINUTE
        res = self.poll()
        self.assertEqual(res.skips, {"emit_failed": 1})
        self.assertTrue(self.home.is_known(mid))  # 毒入りの 1 件を、いつまでも試さない
        self.assertEqual(self.home.read_poll()["retries"], {})

    def test_rate_limit_while_fetching_is_not_counted_against_the_mention(self):
        # レート制限は、その件のせいではない。試した回数に数えず、残りの件も、次の回へ回す
        self.started()
        first, second = self.fake.mention(5 * MINUTE), self.fake.mention(4 * MINUTE)
        self.fake.fail["message"] = 429
        res = self.poll()
        self.assertEqual((res.ok, res.emitted, res.retrying, res.skips), (True, 0, 2, {}))
        self.assertEqual(self.home.read_poll()["retries"], {})
        self.assertEqual(self.fake.count("/messages/%d" % second), 0)  # 2 件目は、取りに行っていない
        self.assertLessEqual(self.home.read_poll()["since"], NOW - 5 * MINUTE)
        self.fake.fail.clear()
        self.clock += 2 * MINUTE
        self.assertEqual(self.poll().emitted, 2)
        self.assertTrue(self.home.is_known(first))

    def test_an_unreadable_mention_does_not_block_the_others(self):
        self.started()
        self.fake.mention(5 * MINUTE, channel=404)  # ボットが開けないチャンネル
        ok = self.fake.mention(4 * MINUTE)
        res = self.poll()
        self.assertEqual(res.emitted, 1)
        self.assertEqual(self.inbox_items()[0]["message_id"], str(ok))


class Quiet(Base):
    def test_guilds_and_roles_are_looked_up_once_in_ten_minutes(self):
        self.started()
        for _ in range(4):
            self.poll()
            self.clock += 2 * MINUTE
        self.assertEqual(self.fake.count("/users/@me/guilds"), 1)
        self.assertEqual(self.fake.count("/members/%d" % ME), 1)
        self.clock += 10 * MINUTE
        self.poll()
        self.assertEqual(self.fake.count("/users/@me/guilds"), 2)

    def test_an_idle_round_is_two_searches_and_nothing_else(self):
        self.started()
        self.poll()
        before = len(self.fake.requests)
        self.clock += 2 * MINUTE
        self.poll()
        idle = self.fake.paths[before:]
        self.assertEqual(len(idle), 2)  # 直接のメンションと、ロール宛て
        self.assertTrue(all(p.endswith("/messages/search") for p in idle))

    def test_dry_run_reads_but_writes_nothing(self):
        self.started()
        state = self.home.poll_path.read_text(encoding="utf-8")
        self.fake.mention(5 * MINUTE)
        res = self.poll(dry_run=True)
        self.assertEqual((res.found, res.emitted), (1, 0))
        self.assertEqual(self.inbox_items(), [])
        self.assertEqual(self.home.poll_path.read_text(encoding="utf-8"), state)
        self.assertFalse(self.home.state_path.exists())


class ReadOnly(Base):
    def test_every_request_is_a_get_to_discord_with_the_bot_token(self):
        self.started()
        mid = self.fake.mention(5 * MINUTE, "見てください https://github.com/ExampleOrg/app/pull/1", channel=FORUM_POST)
        self.fake.add(sf(6 * MINUTE), "<@&%d> 前の話" % OTHER_ROLE, channel=FORUM_POST, roles=[OTHER_ROLE])
        self.fake.reactions[mid] = {"👍": [BOB]}
        self.assertEqual(self.poll().emitted, 1)
        self.assertGreater(len(self.fake.requests), 8)
        for req in self.fake.requests:  # GET 以外は、偽物が受けた時点で落ちる
            self.assertEqual(req.get_method(), "GET")
            self.assertIsNone(req.data)
            self.assertTrue(req.full_url.startswith("https://discord.com/api/v10/"), req.full_url)
            self.assertEqual(req.get_header("Authorization"), "Bot " + TOKEN)

    def test_state_files_hold_no_message_text_and_no_token(self):
        self.started()
        self.fake.mention(5 * MINUTE, "SECRET-BODY")
        self.poll()
        for f in (self.home.poll_path, self.home.state_path):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("SECRET-BODY", text)
            self.assertNotIn(TOKEN, text)
        self.assertIn("SECRET-BODY", self.home.inbox.glob("pending-*.json").__next__().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
