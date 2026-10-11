# 困ったとき

いちばん早いのは、リポジトリを Claude Code で開いて、起きたことをそのまま伝えることです（[README の「困ったとき」](../README.md#困ったとき)）。
ここには、通知の意味と、自分で調べるときの見方を置きます。

## 「⚠」で始まる通知が届いた

異常があると、専用チャンネルに「⚠」で始まる通知が届きます。同じ通知が、続けて何度も届くことはありません。
どの通知でも、拾ったメンションは溜めてあり、直れば、溜めた分から処理されます。

| 通知 | 原因と、すること |
|---|---|
| Discord を読めていません | 通知の中の理由を見ます。`http_401` はボットのトークン違い（リセットして `.env` を書き換える）。`no_token` はトークンを読めない。`http_403_search` は、そのサーバーの「メッセージ履歴を読む」権限が無い。`network` はネットワーク。15 分以上読めないと届きます（トークン違いは、すぐ届く）。直れば、止まっていた間の分も拾います（最大 72 時間） |
| Claude にログインできていない | OAuth トークンが無い、または切れた（1 年で切れる）。`claude setup-token` で作り直し、`CLAUDE_CODE_OAUTH_TOKEN` を書き換えます（[Claude の合鍵](setup.md#claude-の合鍵)） |
| Claude の利用枠に達した | 30 分おきに確かめて、自動で再開します。頻繁なら、`config.json` の `triage_model` / `triage_effort` を軽くします |
| Claude が仕分けを始める前にエラーで止まった | 通知の末尾の「Claude Code のエラー」を読みます（`runs/` の記録の `cli_error` / `api_error` にも残る）。モデルは動いておらず、届いた分は `inbox/` に溜めてあります。ログイン、契約、Claude Code の更新を確かめます。直れば、自動で再開します（10 分おきに確かめる） |
| メンション仕分けの実行に失敗しました | 数分後に、自動で再実行します。続くときは、`runs/` の記録（理由、件数、拒否された操作の数）を見ます。`denials` が多いなら、許可ルールの調整が要ります |
| 仕分けに 3 回失敗したので failed/ に退避しました | その 1 件だけ、処理できなかった。`failed/` の中身を見て、やり直すなら `inbox/` へ戻します |

## 通知は無いが、結果が届かない

- メンションされてから、結果が届くまで、3〜6 分かかります（[時間の根拠](../README.md#しくみ)）。
- 自分がリアクション・返信したメンションは、仕様として届きません。
- 「対応不要」だけのときは、通知が出ません。結果は、専用チャンネルに残ります。
- PC がスリープしている間は、止まります。起きた最初の回が、続きから拾います。
- ボットが入っていないサーバー、ボットに見えないチャンネル、DM は、拾えません。

どれにも当たらないときは、Claude Code に「届かなくなった」と伝えるか、下の「自分で調べる」へ進みます。

## 管理者から返事が来ない

招待されるまでは、確認で「Discord を読めない」（`http_403`）と出ます。ほかの手順は、先に済ませておけます。

## 自分で調べる

リポジトリの中で、`python triage.py status` と `python triage.py check` を打ちます。`check` は、設定から Claude まで順に確かめて、悪い所と直し方を出します。

| `status` の表示 | 意味 |
|---|---|
| `自動起動: 登録されていない` | まだ自動で動いていない。`python triage.py install` |
| `Discord: 読めていない（…）` | いつから、なぜ読めていないか。直し方が、同じ行に出ます |
| `Claude: 止めている（…）` | Claude を呼べないので、待っている。届いた分は、溜めてあります |
| `件数: 未開封 … / 仕分け中 … / 失敗で退避 …` | どこに、何件あるか |
| `最近の記録` | 新着があった回と、異常があった回の記録 |

記録は `%USERPROFILE%\.discord-mention-triage\` の下にあります（[ファイルの置き場](how-it-works.md#ファイルの置き場)）。`dispatch.log` には、変化があった回と異常だけが 1 行ずつ残ります。

| 見えるもの | 意味と、すること |
|---|---|
| `dispatch.log` に `POLL=down:http_202_search` が続く | Discord が、そのサーバーの検索の索引を作っている最中。待てば直ります |
| `dispatch.log` に `SKIP=emit_failed` | 検索には出たが、5 回試しても本体を読めなかった件（ボットが開けないチャンネルなど） |
| `dispatch.log` に `ERROR=config ...` | 機械が読む 1 行は ASCII なので、項目名しか出ません。`python triage.py check` が、全文を日本語で出します |
| `SKIP=` が多い | すでにスタンプ・返信をした分。正常です |
| `ERROR=no_webhook` | `<名前>\.env` に `DIGEST_WEBHOOK` が無い |
| `inbox\` に `pending-*.json` が無い | 受付が拾っていない。下の `--dry-run` で `FOUND=` を見ます |
| `claims\` に残っている | 仕分けの最中か、途中で落ちた回。落ちた回は、最長 45 分で `inbox\` へ戻って再実行されます（3 回で `failed\`）。急ぐなら、手で `inbox\` へ戻します |

部品ごとに確かめるときは、次のコマンドを使います。

| コマンド | 確かめること |
|---|---|
| `python -X utf8 -m watcher --check` | 設定とトークンの有無（Discord には繋がない） |
| `python -X utf8 -m watcher --user <名前> --dry-run` | 検索を 1 回引くだけ。`FOUND=` が、拾えた件数 |
| `python -X utf8 pipeline\check_claude.py --user <名前>` | 本番と同じ引数で、小さいモデルを 1 回だけ動かす。`OK auth=token` が出ればよい |
| `python -X utf8 pipeline\check_claude.py --user <名前> --isolated` | `claude` のログインが無い状態でも動くか。`auth=cli_login` なら合鍵を読めていない、`reason=claude_auth` なら合鍵が違うか期限切れ |

`python triage.py run` は、`python -X utf8 pipeline\dispatch.py --user <名前>` と同じです。出力は ASCII の 1 行です（例: `OK POLL=ok FOUND=1 EMITTED=1 CLAIMED=1 RAN=1 RESULT=done`）。
