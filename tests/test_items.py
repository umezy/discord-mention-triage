import unittest
from datetime import datetime, timezone

from watcher import items

T = datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc)  # JST 12:00

# これまでの pending-*.json が持っていたキー。互換を保つ（仕分けの指示文がこの形を読む）。
LEGACY_KEYS = {
    "message_id", "channel_id", "channel_name", "guild_id", "jump_url", "direct",
    "pull_requests", "mention", "context_before", "context_after",
}
LEGACY_SLIM_KEYS = {"author", "author_id", "time", "text", "attachments"}


def snap(i, text, name="alice", uid=222):
    return items.Snap(id=i, author_id=uid, author_name=name, created_at=T, content=text, attachments=("a.png",))


class Render(unittest.TestCase):
    def test_user_and_role_mentions_become_names(self):
        out = items.render_text("<@111> and <@!222> see <@&900>", {111: "taro", 222: "alice"}, {900: "Nebula"})
        self.assertEqual(out, "@taro and @alice see @Nebula")

    def test_unknown_ids_are_kept_readable(self):
        self.assertEqual(items.render_text("<@999>", {}, {}), "@999")

    def test_empty(self):
        self.assertEqual(items.render_text("", {}, {}), "")


class Time(unittest.TestCase):
    def test_snowflake_roundtrip(self):
        sf = items.time_to_snowflake(T)
        self.assertEqual(items.snowflake_time(sf), T)

    def test_format_is_jst(self):
        self.assertEqual(items.fmt_time(T), "2026/10/03 12:00")


class BuildItem(unittest.TestCase):
    def build(self, **kw):
        base = dict(
            watcher_name="me", kind="direct", guild_id=1, guild_name="Example", channel_id=10, channel_name="dev",
            parent_name=None, message=snap(1000, "hi <@111>"), before=[snap(990, "before")], after=[snap(1010, "after")],
            user_names={111: "taro"}, role_names={}, pull_requests=[], pull_requests_blocked=[],
        )
        base.update(kw)
        return items.build_item(**base)

    def test_keeps_legacy_shape(self):
        item = self.build()
        self.assertTrue(LEGACY_KEYS <= set(item))
        self.assertEqual(set(item["mention"]), LEGACY_SLIM_KEYS)
        self.assertEqual(item["jump_url"], "https://discord.com/channels/1/10/1000")
        self.assertTrue(item["direct"])
        self.assertEqual(item["mention"]["text"], "hi @taro")
        self.assertEqual(item["mention"]["attachments"], ["a.png"])

    def test_names_put_into_the_text_are_listed(self):
        # 表示名には空白が入りうる。あとから「@名前」を取り除けるよう、置き換えた名前を残す（セッションの名前に使う）
        item = self.build(message=snap(1000, "<@111> <@&900> <@111> <@999> hi"),
                          user_names={111: "taro / たろう"}, role_names={900: "Nebula"})
        self.assertEqual(item["mention"]["text"], "@taro / たろう @Nebula @taro / たろう @999 hi")
        self.assertEqual(item["mention_names"], ["taro / たろう", "999", "Nebula"])
        self.assertEqual(self.build(message=snap(1000, "hi"))["mention_names"], [])

    def test_ids_are_strings(self):
        item = self.build()
        for k in ("message_id", "channel_id", "guild_id"):
            self.assertIsInstance(item[k], str)

    def test_the_thread_parent_id_is_a_string_or_none(self):
        self.assertIsNone(self.build()["thread_parent_id"])
        self.assertEqual(self.build(parent_name="forum", parent_id=5)["thread_parent_id"], "5")

    def test_role_kind_is_not_direct(self):
        self.assertFalse(self.build(kind="role")["direct"])

    def test_context_order_is_preserved(self):
        item = self.build(before=[snap(1, "a"), snap(2, "b")], after=[snap(3, "c")])
        self.assertEqual([m["text"] for m in item["context_before"]], ["a", "b"])
        self.assertEqual([m["text"] for m in item["context_after"]], ["c"])


if __name__ == "__main__":
    unittest.main()
