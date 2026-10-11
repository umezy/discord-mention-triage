"""PR レビューを自動で起動してよいかの判定。

メンション本文は信用できない入力で、起動されるレビューは worktree 作成・push・フォローアップ PR
まで進む。だから既定は「何も起動しない」で、組織と依頼者のロールを両方満たしたときだけ許可する。
許可されなかった PR は pull_requests_blocked に理由つきで残し、通常の返信ドラフトの文脈に回す。
"""

from __future__ import annotations

import re

from .config import PrPolicy


def extract(content: str, policy: PrPolicy) -> list:
    """本文中の PR URL を重複なしで取り出す。"""
    pat = re.compile(policy.url_pattern)
    out, seen = [], set()
    for owner, repo, number in pat.findall(content or ""):
        url = f"https://github.com/{owner}/{repo}/pull/{number}"
        if url in seen:
            continue
        seen.add(url)
        out.append({"owner": owner, "repo": repo, "number": int(number), "url": url})
    return out


def split(prs: list, policy: PrPolicy, author_role_ids: frozenset, author_is_bot: bool) -> tuple:
    """(自動レビューしてよい PR, 許可しなかった PR と理由) を返す。"""
    eligible, blocked = [], []
    for pr in prs:
        reason = _reason(pr, policy, author_role_ids, author_is_bot)
        if reason is None:
            eligible.append(pr)
        else:
            blocked.append({"url": pr["url"], "reason": reason})
    return eligible, blocked


def _reason(pr: dict, policy: PrPolicy, author_role_ids: frozenset, author_is_bot: bool):
    if not policy.allowed_orgs or not policy.requester_role_ids:
        return "policy_unconfigured"
    if author_is_bot and not policy.allow_bot_requesters:
        return "bot_requester"
    if pr["owner"].lower() not in policy.allowed_orgs:
        return "org_not_allowed"
    if not (author_role_ids & policy.requester_role_ids):
        return "requester_not_allowed"
    return None
