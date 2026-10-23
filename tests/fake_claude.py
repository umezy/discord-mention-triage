"""テスト用の偽 claude。標準入力の指示文から控えの書き出し先を読み取り、ふりをして控えを書き、投稿本文を最後の返答で返す。

FAKE_CLAUDE_MODE:
  ok        控えを書いて、投稿本文と COUNTS 行を最後の返答にした JSON を返す（既定）
  fail      何も書かず、終了コード 1
  nodigest  控えは書くが、最後の返答は COUNTS 行だけ（投稿本文が無い）
  hang      60秒眠る（タイムアウトの検査用）
  iserror   控えと返答は作るが is_error: true を返す
  autherr   認証切れのエラーを返して終了コード 1
  limit     利用枠切れのエラーを返して終了コード 1
  notstarted  モデルが動く前のエラー（費用 0）を返して終了コード 1。文言は、認証切れにも利用枠切れにも当たらない
              （2026-10-05 に実機で出た形）。FAKE_CLAUDE_ERROR_TEXT があれば、エラー文をそれに差し替える
FAKE_CLAUDE_ARGV_OUT があれば、受け取った引数・作業フォルダ・標準入力を JSON で書き出す。
FAKE_CLAUDE_COUNTS があれば、COUNTS 行の中身をそれにする（例: "draft=0 stamp=0 skip=2 pr=0"。"none" なら COUNTS 行を出さない）。
FAKE_CLAUDE_DIGEST があれば、返答の投稿本文をそれにする。
"""

import json
import os
import re
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
prompt = sys.stdin.buffer.read().decode("utf-8")

out_path = os.environ.get("FAKE_CLAUDE_ARGV_OUT")
if out_path:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"argv": sys.argv[1:], "stdin_chars": len(prompt), "stdin_head": prompt[:40], "cwd": os.getcwd(),
                   "stdin": prompt,
                   "oauth_token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
                   "discord_token_present": "DISCORD_BOT_TOKEN" in os.environ,
                   "api_key_present": "ANTHROPIC_API_KEY" in os.environ,
                   "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
                   "inherited_claude_vars": sorted(k for k in os.environ if k.startswith(("CLAUDE_CODE_", "ANTHROPIC_")))}, f, ensure_ascii=False)

if mode == "hang":
    time.sleep(60)

if mode == "autherr":
    print(json.dumps({"type": "result", "is_error": True,
                      "result": "Failed to authenticate: OAuth session expired and could not be refreshed"}))
    sys.exit(1)

if mode == "limit":
    print(json.dumps({"type": "result", "is_error": True,
                      "result": "Claude usage limit reached. Your limit will reset at 3pm."}))
    sys.exit(1)

if mode == "notstarted":
    text = os.environ.get("FAKE_CLAUDE_ERROR_TEXT") or (
        "Your organization has disabled Claude subscription access for Claude Code "
        "· Use an Anthropic API key instead, or ask your admin to enable access")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": text, "total_cost_usd": 0,
                      "num_turns": 1, "modelUsage": {}, "terminal_reason": "api_error", "api_error_status": 403,
                      "api_error_code": "oauth_not_allowed_for_organization", "permission_denials": []}))
    sys.exit(1)

if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(1)

counts = os.environ.get("FAKE_CLAUDE_COUNTS") or "draft=1 stamp=0 skip=0 pr=0"
drafts = re.search(r"\(1\) ドラフトの控え: `([^`]+)`", prompt)
if drafts:
    with open(drafts.group(1), "w", encoding="utf-8") as f:
        f.write("# drafts (fake)\n")
reply = [] if mode == "nodigest" else [os.environ.get("FAKE_CLAUDE_DIGEST") or "**返信ドラフト**\nalice（fake）\n```\n確認します\n```"]
if counts != "none":
    reply.append("COUNTS " + counts)

print(json.dumps({
    "type": "result", "is_error": mode == "iserror", "session_id": os.environ.get("FAKE_CLAUDE_SESSION_ID") or "sess-fake",
    "result": "\n".join(reply),
    "total_cost_usd": 0.0123, "permission_denials": [{"tool_name": "Bash"}],
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 9045, "cache_read_input_tokens": 9918, "output_tokens": 5,
              "service_tier": "standard"},
}))
