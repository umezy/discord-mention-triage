import unittest
from pathlib import Path

from watcher.config import WatchedUser
from watcher.detect import MessageView, classify

ME = 111


def watcher(**kw):
    base = dict(name="me", user_id=ME, home=Path("."))
    base.update(kw)
    return WatchedUser(**base)


def view(**kw):
    base = dict(id=1000, channel_id=10, guild_id=1, author_id=222, author_is_bot=False, content="hi")
    base.update(kw)
    return MessageView(**base)


class Classify(unittest.TestCase):
    def test_direct_mention(self):
        d = classify(view(mention_user_ids=(ME,)), watcher())
        self.assertEqual((d.kind, d.skip), ("direct", None))

    def test_no_mention_is_not_a_skip(self):
        d = classify(view(), watcher())
        self.assertEqual((d.kind, d.skip), (None, None))

    def test_mention_of_someone_else(self):
        d = classify(view(mention_user_ids=(333,)), watcher())
        self.assertIsNone(d.kind)

    def test_role_mention_needs_the_role(self):
        v = view(mention_role_ids=(900,))
        self.assertIsNone(classify(v, watcher(), frozenset()).kind)
        self.assertIsNone(classify(v, watcher(), frozenset({901})).kind)
        self.assertEqual(classify(v, watcher(), frozenset({900, 901})).kind, "role")

    def test_direct_wins_over_role(self):
        v = view(mention_user_ids=(ME,), mention_role_ids=(900,))
        self.assertEqual(classify(v, watcher(), frozenset({900})).kind, "direct")

    def test_everyone_is_off_by_default(self):
        v = view(mention_everyone=True)
        self.assertIsNone(classify(v, watcher()).kind)
        self.assertEqual(classify(v, watcher(include_everyone=True)).kind, "everyone")

    def test_own_message_is_ignored(self):
        d = classify(view(author_id=ME, mention_user_ids=(ME,)), watcher())
        self.assertEqual((d.kind, d.skip), (None, "own"))

    def test_dm_is_ignored(self):
        d = classify(view(guild_id=None, mention_user_ids=(ME,)), watcher())
        self.assertEqual((d.kind, d.skip), (None, "dm"))

    def test_blocked_channel_and_thread_parent(self):
        w = watcher(channel_blocklist=frozenset({10, 77}))
        v = view(mention_user_ids=(ME,))
        self.assertEqual(classify(v, w).skip, "blocked")
        v2 = view(channel_id=55, parent_channel_id=77, mention_user_ids=(ME,))
        self.assertEqual(classify(v2, w).skip, "blocked")

    def test_blocklist_does_not_hide_non_mentions(self):
        w = watcher(channel_blocklist=frozenset({10}))
        d = classify(view(), w)
        self.assertEqual((d.kind, d.skip), (None, None))  # 宛先でないものは数えない

    def test_guild_allowlist(self):
        w = watcher(guild_allowlist=frozenset({2}))
        v = view(mention_user_ids=(ME,))
        self.assertEqual(classify(v, w).skip, "guild")
        self.assertEqual(classify(view(guild_id=2, mention_user_ids=(ME,)), w).kind, "direct")


if __name__ == "__main__":
    unittest.main()
