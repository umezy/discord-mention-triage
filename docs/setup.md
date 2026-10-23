# コマンドで進める

[README のセットアップ](../README.md#セットアップ) を、Claude Code のスキルを使わずに、コマンドで進める手順と、コマンドと設定の説明。
スキルに頼むときも、中で同じコマンドが動く（手順書は [`.claude/skills/triage-setup/SKILL.md`](../.claude/skills/triage-setup/SKILL.md)）。
打つのは `python triage.py ...` の 1 種類（[ADR-0015](adr/0015-one-command-one-folder.md)）。どのコマンドも、鍵を画面に出さない。

## 手順

1. **用意する。** [必要なもの](../README.md#必要なもの) をそろえ、リポジトリを置く（`git clone https://github.com/umezy/discord-mention-triage.git`）。
   `claude --version` で `claude` が入っているかを見て、無ければ下の「`claude` を入れる」。
   ボット、専用チャンネル、ID を用意し、管理者にボットの招待を頼む（[Discord 側と鍵を用意する](get-the-keys.md)）。
2. **設定を作る。** リポジトリの中で `python triage.py init`。名前、ID 2 つ、鍵 3 つを、順に聞かれる。
   鍵は、貼っても画面に出ない。まだ手元に無い鍵は Enter で飛ばし、あとで `.env` に書ける（最後に、開くファイルが出る）。
3. **確かめる。** `python triage.py check`。設定、ボットのトークン、Discord を読めるか、Webhook、Claude を、順に確かめる。
   `[NG]` が出たら、すぐ下の「→」のとおりに直して、もう一度打つ。

   ```text
   [OK] 設定: ...（見張る人: taro）
   [OK] ボットのトークン: あり
   [OK] Discord を読める（検索を 1 回引いた。何も書いていない）
   [OK] Webhook: あり
   [NG] Claude: 動かなかった（claude_auth）
          → Claude にログインできていない。`claude setup-token` の合鍵を ... に書く
   ```

4. **1 回動かす。** `python triage.py run` を 1 回打つ（初回は、「いまから見張りを始める」の合図になる）。
   同僚に、自分宛てのメンションを 1 通頼み、リアクションも返信もせずに 2〜3 分待って、もう 1 回打つ。仕分けが動いて、専用チャンネルに結果が届く。
5. **自動起動を登録する。** `python triage.py install`。4 を 2 分おきに動かす登録（タスクスケジューラに 1 つだけ）。動いているかは `python triage.py status`。

うまくいかないときは [困ったとき](troubleshooting.md)。

## コマンド

| コマンド | すること |
|---|---|
| `init` | 名前、ID 2 つ、鍵 3 つを聞いて、設定と鍵の置き場（`~/.discord-mention-triage/`）を作る。すでにあれば何もしない。`--force` で作り直す（古い設定は名前を変えて残し、鍵のファイルは触らない）。`--no-input` は、質問せずに、鍵を空欄で作る |
| `check` | 設定、ボットの鍵、Discord の読み取り、Webhook、Claude を、順に確かめる。`--no-claude` は Claude を動かさない。`--post-test` は、専用チャンネルへ確認用の投稿を 1 通送る |
| `run` | 1 回動かす。`--dry-run` は、検索は引くが、状態を進めず、Claude も起動しない。`--backfill-hours N` は、過去 N 時間ぶんも拾う |
| `install` | 2 分おきの自動起動を、タスクスケジューラに 1 つ登録する。`--interval-minutes 5` で間隔を変える |
| `uninstall` | 自動起動の登録を外す。設定と鍵は残る |
| `status` | 自動起動、Discord を読めているか、溜まっている件、最近の記録 |

- `install` は、`scripts\install-task.ps1` を `powershell -ExecutionPolicy Bypass -File ...` の形で呼ぶ。`.\scripts\install-task.ps1` と直接打つと、
  新しい PC では弾かれる（Windows PowerShell は、既定でスクリプトの実行が無効）。
- 自動起動は、PC が起きていて、ログオンしている間だけ動く。デスクトップ版のアプリを開いておく必要はない。

## `claude` を入れる

PowerShell で `claude` と打って「認識されません」と出る PC には、CLI が入っていない（デスクトップ版だけでは入らない）。
公式のインストーラーは `https://claude.ai/install.ps1`。中身を保存して読んでから実行するのがおすすめ
（`downloads.claude.ai` から取得し、SHA256 を照合して、`claude.exe install` を呼ぶだけの短いスクリプト）。

```text
Invoke-WebRequest https://claude.ai/install.ps1 -OutFile $env:TEMP\claude-install.ps1
notepad $env:TEMP\claude-install.ps1
& $env:TEMP\claude-install.ps1
```

インストーラーは `C:\Users\<名前>\.local\bin` を PATH に入れないことがある（`Setup notes: ... is not in your PATH` と出る）。
そのときは、ユーザーの PATH にそのフォルダを足して、端末を開き直す。自動起動の側は、PATH に無くても `~\.local\bin\claude.exe` を直接探す。

入れたら `claude` と打ち、ブラウザで一度ログインする。

## 設定

設定は `%USERPROFILE%\.discord-mention-triage\config.json`。`init` が作り、あとから直すときはメモ帳で開く（読ませるフォルダを変えるだけなら、Claude Code で「このフォルダも読ませて」と頼めば、スキルが書き換える）。全項目は `config/config.example.json`。

| 項目 | 意味 |
|---|---|
| `watchers[].read_dirs` | 事実確認のために、Claude が読んでよい手元のフォルダ（読み取りのみ） |
| `watchers[].channel_read_dirs` | チャンネルごとに足すフォルダ。`{"チャンネルの ID": ["フォルダ"]}` と書く。ID はスレッドの親（フォーラムなど）でもよく、その中のスレッドの件にも当たる。その場所の件が来た回だけ読めて、Claude はまずそこで確かめる。1 回の仕分けにほかの場所の件が混ざると、その回は混ざった分も読める（[ADR-0034](adr/0034-per-channel-read-dirs.md)） |
| `watchers[].triage_model` / `triage_effort` | 仕分けに使うモデルと effort（既定 `opus` / `xhigh`）。利用枠に当たりやすいなら軽くする |
| `watchers[].pr_policy` | PR の URL を、条件を満たした依頼と、満たさなかった依頼に分ける条件。PR の自動レビューは無効で、有効にする設定は無い（`pr_auto_review: true` と書くと、読み込みで止まる） |
| `claude_bin` | `claude` の場所。空なら、PATH、デスクトップ版に同梱の最新版、の順で探す |
| `search_overlap_minutes` | 検索を、前回の続きより何分手前から重ねて引くか（既定 30）。検索の索引の遅れを吸収する |
| `catchup_max_age_hours` | PC が止まっていた間の分を、どこまでさかのぼって拾うか（既定 72） |
| `keep_transcripts` | `true` で、Claude のセッション記録（本文を含む）を `~/.claude/projects/` に残す（雛形は `true`。コードの既定は、設定を忘れても本文が残らないよう `false`）。記録は 30 日で自動削除される。開くときは、実行記録（`runs/`）の `session_id` を使い、`claude --resume <ID>`。`true` のとき、同じ流れの続きのメンションは、前の仕分けの会話を写したセッションで仕分ける（[ADR-0036](adr/0036-continue-the-same-flow.md)） |
| `desktop_import` | `true` で、仕分けのたびに、そのセッションをデスクトップ版の左側の一覧へ取り込む（`keep_transcripts: true` が前提。雛形は `false`）。名前は `<送り主>「<用件の頭>」#<場所> ほかN件` で、結果の各件の最後の行にも書く（長押しで写せる）。同じ流れの続きは、前のセッションに書き足す（一覧は増えず、上へも上がらない。前のセッションの裏の処理が待機中なら、それを終わらせてから書き足す。画面に出しているときや、話している最中などは、前の会話を写して、名前の後ろに「（続き）」。[しくみ](how-it-works.md)）。取り込みに失敗しても、仕分けは成功のまま。`true` のときは、仕分けを「自動」モードで動かす（取り込んだセッションが「自動」で開く。[ADR-0033](adr/0033-auto-mode-when-importing.md)）。取り込むと、デスクトップ版の表示が一瞬そのセッションへ移り、すぐ見ていたセッションへ戻る（Windows。[ADR-0035](adr/0035-return-to-viewed-session.md)） |
| `desktop_link_page` | 中継ページ（[`docs/open.html`](open.html)）を開ける http(s) の URL。書くと、各件のセッションの行の下に「Claudeで開く(PCのみ)」リンクが付き、PC の Discord から押すと、ブラウザを通ってデスクトップ版でそのセッションが開く（ブラウザが一度「Claude を開きますか」と聞く。スマホからは開けない）。ページは自分で置く（GitHub Pages、PC の中の http サーバーなど）。空（雛形）なら付かない（[ADR-0040](adr/0040-open-from-discord.md)） |

鍵は、リポジトリの外の 2 つのファイルに置く（`config.py` は、リポジトリの中を指す設定を拒否する）。メモ帳で「UTF-8（BOM 付き）」で保存しても読める。

| ファイル | 中身 |
|---|---|
| `%USERPROFILE%\.discord-mention-triage\.env` | `DISCORD_BOT_TOKEN=...`（受付だけが読む） |
| `%USERPROFILE%\.discord-mention-triage\<名前>\.env` | `DIGEST_WEBHOOK=...`、`CLAUDE_CODE_OAUTH_TOKEN=...`、任意で `DIGEST_MENTION_USER_ID=...` |

## Claude の合鍵

無人実行は、`claude setup-token` で作る 1 年有効の合鍵で動く（[ADR-0012](adr/0012-pin-unattended-runs-to-a-token.md)）。合鍵があれば、端末の `claude` で
どのアカウントにログインしていても、ログアウトしていても、仕分けは同じアカウントで動き続ける。合鍵が無いと、PC に 1 つしか無い `claude` のログインで動くので、
アカウントを切り替えると、仕分けも一緒に切り替わる。

- どのアカウントで承認するかで、どの契約の利用枠を使うか、入力がモデルの学習に使われるかが決まる（[security.md](security.md#データがどこを通るか)）。
- 合鍵は 1 年で切れる。切れると「Claude にログインできていない」の通知が届き、届いた分は溜めて待つ。作り直して `.env` を書き換える。
- 合鍵は、モデルへの要求にしか使えない（Remote Control やコネクタは使えない。この仕組みには要らない）。
- `ANTHROPIC_API_KEY` などの API 系の環境変数は、`claude` に渡さない（`-p` では、あると API の従量課金になる）。
- `--bare` は付けない（付けると合鍵を読まず、API キーが要る）。公式は、将来 `-p` の既定にするとしているので、Claude Code を更新したら `check` で確かめる。
