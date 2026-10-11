"""設定の読み込み。標準ライブラリのみ（受付・pipeline・テストが使う）。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_PR_PATTERN = r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)"
TOKEN_KEY = "DISCORD_BOT_TOKEN"


class ConfigError(Exception):
    """設定の不備。メッセージにトークンなどの秘密を含めない。"""


@dataclass(frozen=True)
class PrPolicy:
    """PR レビューを自動で起動してよい条件。空の設定は「何も起動しない」（fail closed）。"""

    allowed_orgs: frozenset = field(default_factory=frozenset)
    requester_role_ids: frozenset = field(default_factory=frozenset)
    allow_bot_requesters: bool = False
    url_pattern: str = DEFAULT_PR_PATTERN


@dataclass(frozen=True)
class WatchedUser:
    name: str
    user_id: int
    home: Path
    guild_allowlist: frozenset = field(default_factory=frozenset)  # 空 = ボットと本人の両方がいる全サーバー
    channel_blocklist: frozenset = field(default_factory=frozenset)
    context_before: int = 15
    context_after: int = 10
    include_everyone: bool = False
    pr_policy: PrPolicy = field(default_factory=PrPolicy)
    # --- 仕分けを動かす Claude（pipeline/run_claude.py が使う） ---
    read_dirs: tuple = ()  # 事実確認のために Claude が読んでよい手元のリポジトリ（読み取りのみ）
    triage_model: str = "opus"
    triage_effort: str = "xhigh"


@dataclass(frozen=True)
class Config:
    state_root: Path
    token_env_path: Path
    watchers: tuple
    catchup_max_age_hours: int = 72  # 止まっていた間の分を、どこまでさかのぼって拾うか
    search_overlap_minutes: int = 30  # 検索を、前回の続きより手前から重ねて引く幅（索引の遅れ・順番の前後を吸収する）
    claude_bin: str = ""  # 空なら自動で探す（PATH → デスクトップアプリ同梱の最新版）
    claude_timeout_sec: int = 1800
    keep_transcripts: bool = False  # false = Claude のセッション記録（本文を含む）をディスクに残さない
    desktop_import: bool = False  # true = 仕分けのたびに、そのセッションをデスクトップ版の左側の一覧へ取り込む（keep_transcripts が前提）
    path: Path | None = None


def _ids(values, what: str) -> frozenset:
    out = set()
    for v in values or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            raise ConfigError(f"{what} に数値でない ID がある: {v!r}") from None
    return frozenset(out)


MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,63}$")


def _choice(value, what: str) -> str:
    """モデル名・effort はコマンドラインに渡すので、記号を含むものは拒否する。"""
    v = str(value)
    if not MODEL_RE.match(v):
        raise ConfigError(f"{what} に使えない文字がある: {v!r}")
    return v


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def find_config_path(explicit: str | None = None) -> Path:
    """明示指定 → 環境変数 → ~/.discord-mention-triage/config.json → リポジトリの config/config.json（古い置き場）。

    設定と鍵を 1 つのフォルダに寄せる（ADR-0015）。両方にあるときは、~/.discord-mention-triage/ のほうを読む。"""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    elif os.environ.get("MENTION_TRIAGE_CONFIG"):
        candidates.append(Path(os.environ["MENTION_TRIAGE_CONFIG"]))
    else:
        candidates.append(Path.home() / ".discord-mention-triage" / "config.json")
        candidates.append(REPO_ROOT / "config" / "config.json")
    for c in candidates:
        if c.is_file():
            return c
    raise ConfigError("config.json が見つからない。探した場所: " + " / ".join(str(c) for c in candidates))


def _inside_repo(p: Path) -> bool:
    try:
        p.resolve().relative_to(REPO_ROOT)
        return True
    except ValueError:
        return False


def parse(data: dict, path: Path | None = None) -> Config:
    state_root = _expand(data.get("state_root") or "~/.discord-mention-triage")
    # 状態にはメッセージ本文・ドラフト・トークンが入る。リポジトリの中に置くとコミットしてしまうので拒否する。
    if _inside_repo(state_root):
        raise ConfigError(f"state_root をリポジトリの中に置かない（本文をコミットしてしまう）: {state_root}")
    token_env = _expand(data.get("token_env_path") or str(state_root / ".env"))
    if _inside_repo(token_env):
        raise ConfigError(f"token_env_path をリポジトリの中に置かない: {token_env}")
    raw_watchers = data.get("watchers")
    if not raw_watchers:
        raise ConfigError("watchers が空。最低1人は設定する")

    watchers, seen = [], set()
    for w in raw_watchers:
        name = str(w.get("name", ""))
        if not NAME_RE.match(name):
            raise ConfigError(f"watchers[].name は英小文字・数字・-_ の32文字以内: {name!r}")
        if name in seen:
            raise ConfigError(f"watchers[].name が重複: {name}")
        seen.add(name)
        try:
            user_id = int(w["user_id"])
        except (KeyError, TypeError, ValueError):
            raise ConfigError(f"watchers[{name}].user_id が数値でない") from None
        if w.get("pr_auto_review"):
            # 有効にする実装は無い（ADR-0004）。true を黙って読み飛ばすと「有効にしたつもり」になるので、止める。
            raise ConfigError(f"watchers[{name}].pr_auto_review は true にできない（PR の自動レビューは無効。ADR-0004）")
        p = w.get("pr_policy") or {}
        policy = PrPolicy(
            allowed_orgs=frozenset(str(o).lower() for o in p.get("allowed_orgs", [])),
            requester_role_ids=_ids(p.get("requester_role_ids"), "pr_policy.requester_role_ids"),
            allow_bot_requesters=bool(p.get("allow_bot_requesters", False)),
            url_pattern=p.get("url_pattern", DEFAULT_PR_PATTERN),
        )
        watchers.append(
            WatchedUser(
                name=name,
                user_id=user_id,
                home=state_root / name,
                guild_allowlist=_ids(w.get("guild_allowlist"), "guild_allowlist"),
                channel_blocklist=_ids(w.get("channel_blocklist"), "channel_blocklist"),
                context_before=int(w.get("context_before", 15)),
                context_after=int(w.get("context_after", 10)),
                # settle_seconds（投稿から待つ秒数）は、外した（検索に出るまでの約 100 秒が、様子見を兼ねる）。古い設定ファイルに残っていても、読み飛ばす
                include_everyone=bool(w.get("include_everyone", False)),
                pr_policy=policy,
                read_dirs=tuple(str(d) for d in w.get("read_dirs", [])),
                triage_model=_choice(w.get("triage_model", "opus"), "triage_model"),
                triage_effort=_choice(w.get("triage_effort", "xhigh"), "triage_effort"),
            )
        )
    return Config(
        state_root=state_root,
        token_env_path=token_env,
        watchers=tuple(watchers),
        # 常駐版の項目（heartbeat_seconds / sweep_seconds / catchup_max_messages_per_channel / max_parallel_runs）は、
        # 古い設定ファイルに残っていても読み飛ばす
        catchup_max_age_hours=max(1, int(data.get("catchup_max_age_hours", 72))),
        search_overlap_minutes=max(0, int(data.get("search_overlap_minutes", 30))),
        claude_bin=str(data.get("claude_bin", "")),
        claude_timeout_sec=int(data.get("claude_timeout_sec", 1800)),
        keep_transcripts=bool(data.get("keep_transcripts", False)),
        desktop_import=bool(data.get("desktop_import", False)),
        path=path,
    )


def load(explicit: str | None = None) -> Config:
    path = find_config_path(explicit)
    try:
        # utf-8-sig: Windows のメモ帳が付ける BOM を読み飛ばす（付いたままだと、JSON として読めない）
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"config.json を読めない: {path} ({type(e).__name__})") from None
    return parse(data, path)


def load_token(cfg: Config) -> str:
    """環境変数 DISCORD_BOT_TOKEN、無ければ token_env_path の .env から読む。値は返すだけで表示しない。"""
    tok = os.environ.get(TOKEN_KEY, "").strip()
    if tok:
        return tok
    p = cfg.token_env_path
    if p.is_file():
        for line in p.read_text(encoding="utf-8-sig").splitlines():  # BOM つきでも、先頭の 1 行を読める
            line = line.strip()
            if line.startswith(TOKEN_KEY + "="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    raise ConfigError(f"{TOKEN_KEY} が無い。環境変数か {p} に書く（値はチャットやコミットに出さない）")
