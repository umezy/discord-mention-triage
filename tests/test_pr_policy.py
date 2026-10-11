import unittest

from watcher import pr_policy
from watcher.config import PrPolicy

STAFF = 500


def policy(**kw):
    base = dict(allowed_orgs=frozenset({"exampleorg", "otherorg"}), requester_role_ids=frozenset({STAFF}))
    base.update(kw)
    return PrPolicy(**base)


def prs(*urls):
    return pr_policy.extract(" ".join(urls), PrPolicy())


class Extract(unittest.TestCase):
    def test_extracts_and_dedupes(self):
        got = pr_policy.extract(
            "see https://github.com/ExampleOrg/app/pull/12 and github.com/ExampleOrg/app/pull/12 and "
            "https://github.com/OtherOrg/web/pull/3/files",
            PrPolicy(),
        )
        self.assertEqual([p["url"] for p in got], [
            "https://github.com/ExampleOrg/app/pull/12",
            "https://github.com/OtherOrg/web/pull/3",
        ])
        self.assertEqual(got[0]["number"], 12)

    def test_no_urls(self):
        self.assertEqual(pr_policy.extract("no pr here", PrPolicy()), [])
        self.assertEqual(pr_policy.extract("", PrPolicy()), [])


class Split(unittest.TestCase):
    def test_allowed_org_and_role(self):
        ok, blocked = pr_policy.split(prs("https://github.com/ExampleOrg/app/pull/1"), policy(), frozenset({STAFF}), False)
        self.assertEqual(len(ok), 1)
        self.assertEqual(blocked, [])

    def test_org_match_is_case_insensitive(self):
        ok, _ = pr_policy.split(prs("https://github.com/exampleorg/app/pull/1"), policy(), frozenset({STAFF}), False)
        self.assertEqual(len(ok), 1)

    def test_other_org_is_blocked(self):
        ok, blocked = pr_policy.split(prs("https://github.com/evil/app/pull/1"), policy(), frozenset({STAFF}), False)
        self.assertEqual(ok, [])
        self.assertEqual(blocked[0]["reason"], "org_not_allowed")

    def test_requester_without_role_is_blocked(self):
        ok, blocked = pr_policy.split(prs("https://github.com/ExampleOrg/app/pull/1"), policy(), frozenset({1, 2}), False)
        self.assertEqual(ok, [])
        self.assertEqual(blocked[0]["reason"], "requester_not_allowed")

    def test_bot_requester_blocked_unless_allowed(self):
        p = prs("https://github.com/ExampleOrg/app/pull/1")
        _, blocked = pr_policy.split(p, policy(), frozenset({STAFF}), True)
        self.assertEqual(blocked[0]["reason"], "bot_requester")
        ok, _ = pr_policy.split(p, policy(allow_bot_requesters=True), frozenset({STAFF}), True)
        self.assertEqual(len(ok), 1)

    def test_unconfigured_policy_fails_closed(self):
        p = prs("https://github.com/ExampleOrg/app/pull/1")
        for pol in (PrPolicy(), policy(allowed_orgs=frozenset()), policy(requester_role_ids=frozenset())):
            ok, blocked = pr_policy.split(p, pol, frozenset({STAFF}), False)
            self.assertEqual(ok, [])
            self.assertEqual(blocked[0]["reason"], "policy_unconfigured")

    def test_mixed_list(self):
        p = prs("https://github.com/ExampleOrg/a/pull/1", "https://github.com/other/b/pull/2")
        ok, blocked = pr_policy.split(p, policy(), frozenset({STAFF}), False)
        self.assertEqual([x["number"] for x in ok], [1])
        self.assertEqual([x["reason"] for x in blocked], ["org_not_allowed"])


if __name__ == "__main__":
    unittest.main()
