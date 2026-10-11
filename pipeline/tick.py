"""受信トレイの門番。「仕事があるか」を調べて、仕事があれば claim（先取り）する。AI は使わない。Discord にも通信しない。

dispatch.py が、受付（watcher/search.py）のあとに、この部品を使う。手元で状態を見たいときは、このスクリプトを単体で
動かすと、同じ判定を ASCII の1行で返す（診断用）。

診断出力（常に ASCII の1行。Windows のコンソールは日本語を壊すため）:

  OK NEW=0 CLAIMED=0
  OK NEW=2 CLAIMED=1 FILES=<abs>|<abs> HOME=<abs>
  OK NEW=0 CLAIMED=1 REQUEUED=1 FILES=<abs> HOME=<abs>
  OK NEW=0 CLAIMED=0 FAILED=1 FAILEDFILES=<name>
  ERROR=tick <ExceptionName>                                  （このスクリプト自身の故障。exit=4）

終了コード: 0 正常（仕事の有無は問わない）/ 4 tick 自身の故障。

復旧: claim は処理が済むまで claims/ に残る。途中で落ちた回の claim は、claim の前に掃除する:
  * claim が TRIAGE_TIMEOUT を過ぎたら inbox/ へ戻し、同じ回で再 claim する。仕分けが失敗した claim は、
    run_claude が更新時刻を古くしておくので、5 分後に戻る。
  * MAX_ATTEMPTS 回やって駄目なら failed/ へ退避して1回だけ知らせる（毒入りの入力を無限に試さない）。
  * PR の URL を含む件も、同じ扱い。PR の自動レビューは無効（ADR-0004）で、どの件も「仕分け」として動くため。
    レビュー用の別扱い（長いタイムアウト、自動では戻さない）は、有効にする決定と一緒に戻す（ADR-0013）。

使い方:
  python -X utf8 tick.py [--user NAME]          claim する
  python -X utf8 tick.py --dry-run              状態を進めず、claim も掃除もしない。何度でも試せる
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

from common import add_target_args, p, resolve  # noqa: E402

# claim の持ち主が死んだと見なすまでの時間。仕分けは Claude 1回で数分。
TRIAGE_TIMEOUT_SEC = 45 * 60
UNKNOWN_TIMEOUT_SEC = 2 * 60 * 60
MAX_ATTEMPTS = 3


def load_meta(home) -> dict:
    try:
        data = json.loads(home.meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_meta(home, meta: dict) -> None:
    try:
        home.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 帳簿だけの話。実行を落とさない


def read_items(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("items", [])
        return items if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def payload_kind(path: Path) -> str:
    """'triage' = 仕分ける件 / 'unknown' = 読めない。

    PR の URL を含む件（`pull_requests`）も 'triage'。PR の自動レビューは無効で（ADR-0004）、その件も、ふつうの
    仕分けとして動く。別の種類にすると、仕分けが失敗したときに自動で再試行されない（ADR-0013）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    return "triage" if isinstance(data, dict) else "unknown"


def timeout_for(kind: str) -> int:
    return TRIAGE_TIMEOUT_SEC if kind == "triage" else UNKNOWN_TIMEOUT_SEC


def sweep(home, now: float, meta: dict, dry_run: bool) -> tuple:
    """持ち主が死んだと見なす claim を片付ける。(requeued, failed) の名前リストを返す。"""
    requeued, failed = [], []
    if not home.claims.exists():
        return requeued, failed
    for path in sorted(home.claims.glob("pending-*.json")):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        # 種類は、毎回ファイルから決める。古い版が帳簿に残した kind（"pr" を含む）は使わない
        if age <= timeout_for(payload_kind(path)):
            continue  # まだ誰かの作業中。触らない
        attempts = meta.get(path.name, {}).get("attempts", 1)
        if attempts >= MAX_ATTEMPTS:
            if dry_run:
                failed.append(path.name)
                continue
            home.failed.mkdir(parents=True, exist_ok=True)
            try:
                path.rename(home.failed / path.name)
            except OSError:
                continue
            meta.pop(path.name, None)
            failed.append(path.name)
            continue
        if dry_run:
            requeued.append(path.name)
            continue
        home.inbox.mkdir(parents=True, exist_ok=True)
        try:
            path.rename(home.inbox / path.name)
        except OSError:
            continue
        requeued.append(path.name)
    return requeued, failed


def claim(home, meta: dict, dry_run: bool, limit: int | None = None) -> list:
    """inbox/pending-*.json を claims/ へ mv する。この mv がロック。

    作業の前に claim するのは、長い処理が次の起動で二重に走るのを防ぐため。rename が失敗したら
    別の実行が先に取ったということなので、黙って諦めて二度と触らない。limit は同時に抱える数の上限。
    """
    claimed = []
    if not home.inbox.exists():
        return claimed
    home.claims.mkdir(parents=True, exist_ok=True)
    for src in sorted(home.inbox.glob("pending-*.json")):
        if limit is not None and len(claimed) >= limit:
            break
        dst = home.claims / src.name
        if dry_run:
            claimed.append(dst)
            continue
        try:
            src.rename(dst)
        except OSError:
            continue
        try:
            dst.touch()  # mtime = claim した時刻。sweep がこれを読む
        except OSError:
            pass
        prev = meta.get(src.name, {})
        meta[src.name] = {"attempts": prev.get("attempts", 0) + 1, "claimed_at": int(time.time())}
        claimed.append(dst)
    return claimed


def prune_meta(home, meta: dict) -> None:
    for name in list(meta):
        if (home.claims / name).exists() or (home.inbox / name).exists():
            continue
        meta.pop(name, None)


def collect(home, now: float, dry_run: bool, limit: int | None = None) -> dict:
    """滞留の掃除 → claim。結果を辞書で返す。"""
    meta = load_meta(home)
    # claim の前に掃除する。ここで戻したものは、この回のうちに claim されて処理される。
    requeued, failed = sweep(home, now, meta, dry_run)
    claimed = claim(home, meta, dry_run, limit)
    if not dry_run:
        prune_meta(home, meta)
        save_meta(home, meta)
    return {
        "requeued": requeued,
        "failed": failed,
        "claimed": claimed,
    }


def run(args, now: float | None = None) -> tuple:
    """(出力1行, 終了コード)。テストから直接呼べるよう print しない。"""
    home, watcher = resolve(args)
    home.ensure()
    now = time.time() if now is None else now
    dry_run = args.dry_run

    got = collect(home, now, dry_run)
    claimed = got["claimed"]
    new = sum(len(read_items(c)) for c in claimed)

    parts = ["OK", "NEW=%d" % new, "CLAIMED=%d" % len(claimed)]
    if got["requeued"]:
        parts.append("REQUEUED=%d" % len(got["requeued"]))
    if got["failed"]:
        parts.append("FAILED=%d" % len(got["failed"]))
    if claimed:
        parts.append("FILES=" + "|".join(p(c) for c in claimed))
        parts.append("HOME=" + p(home.root))
    if got["failed"]:
        parts.append("FAILEDFILES=" + "|".join(got["failed"]))
    if dry_run:
        parts.append("dry=1")
    return " ".join(parts), 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    add_target_args(ap)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        line, code = run(args)
    except Exception as e:  # noqa: BLE001 - 常に1行で返す
        print("ERROR=tick %s" % type(e).__name__)
        return 4
    print(line)
    return code


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="ascii", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="ascii", errors="replace")
    sys.exit(main())
