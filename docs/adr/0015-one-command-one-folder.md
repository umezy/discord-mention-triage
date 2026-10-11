---
status: accepted
date: 2026-10-10
amended-by: [ADR-0028]
---

# ADR-0015: 入口のコマンドを 1 つ、設定と鍵の置き場を 1 つにする。配るときは、フォルダをそのまま渡す

## 背景と課題

人に配るときに引っかかる点を、2026-10-05 に洗い出した。2026-10-09〜10 に、2 台目の PC へ持ち込んで、実際に引っかかった点も足す。

- **設定が 3 か所に分かれていた。** `config/config.json`（リポジトリの中）、`~/.discord-mention-triage/.env`、`~/.discord-mention-triage/<名前>/.env`。
- **入口が多かった。** `watcher --check`、`check_claude.py`、`dispatch.py`、`tick.py`、`post_digest.py`、`install-*.ps1` を、手順書どおりに打ち分ける。
- **雛形を手で直すと、直し忘れに気づきにくい。** 2 台目では、`user_id` が雛形のままで止まった。機械が読む 1 行は ASCII なので、
  日本語のエラー文が `??????` になり、何が悪いのかが画面から分からなかった。
- **メモ帳で保存した `.env` の先頭の行が、読めないことがあった**（BOM。別の変更で直した）。
- **`claude` のログインが切れていた。** 2 台目では、黒い画面の `claude` のログインが期限切れで、合鍵（[ADR-0012](0012-pin-unattended-runs-to-a-token.md)）を置くまで動かなかった。
- **`desktop_import` の雛形が `true` だった。** デスクトップ版が無い PC では、毎回失敗が記録される。
- **`.ps1` を直接実行すると、新しい PC では弾かれる**（Windows PowerShell は、既定でスクリプトの実行が無効）。
- **private リポジトリの clone が前提だった。** 受け取る人の全員に、GitHub の権限が要る。
- 検索を引く形にしたので（[ADR-0014](0014-poll-search-api-single-entry.md)）、Python の標準ライブラリだけで動く。venv も pip も要らない。

## 決定

1. **入口のコマンドは `triage.py` の 1 つ。** 人が打つのは、これだけにする。
   - `init`: 質問に答えると、設定と、鍵の置き場を作る。鍵は、貼っても画面に出さない。すでにある鍵のファイルは、聞かず、上書きもしない。
   - `check`: 設定 → ボットの鍵 → Discord の読み取り（検索を 1 回引くだけ）→ Webhook → Claude（小さいモデルで 1 回）を、順に確かめ、直し方を日本語で出す。
   - `run`: 1 回動かす（自動起動がやっているのと同じ）。`install` / `uninstall`: 自動起動の登録と解除。`status`: いまの状態。
   - 自動起動が呼ぶのは、これまでどおり `pipeline/dispatch.py`（機械が読む、ASCII の 1 行を出す）。`triage.py` は、人のための入口。
2. **設定と鍵の置き場は `~/.discord-mention-triage/` の 1 つ。** `config.json` も、ここに置く。
   リポジトリの `config/config.json`（これまでの置き場）も読めるが、両方にあるときは、`~/.discord-mention-triage/` のほうを読む。
   鍵のファイルを 2 つに分けているのは、そのまま（ボットの鍵は受付だけが、Webhook と合鍵は本人のフォルダの下で、投稿と秘書だけが読む）。
3. **`init` が書く設定は、デスクトップ版への取り込み（`desktop_import`）を `false` にする。雛形も `false` にする。**
   取り込みは、デスクトップ版がある PC で、使いたい人が `true` にする（[ADR-0009](0009-import-sessions-into-desktop.md) の機能は、残す）。
4. **配るときは、リポジトリのフォルダを、そのまま渡す**（zip にして渡す。`git archive --format=zip -o discord-mention-triage.zip HEAD`）。
   受け取った人は、展開して、`python triage.py init` から始める。Git も、GitHub の権限も、要らない。
5. **古い鍵の名前（`DISCORD_WATCH_WEBHOOK` など）は、いまは読めるままにする。** `check` が、書き換えを促す。

### 結果

- 得るもの: 新しい PC での手順が、「展開する → `init` → `check` → `run` → `install`」になる。つまずいたときは、`check` と `status` が、どこが悪いかを言う。
- 引き受けるもの: 入口が 1 つ増える（`pipeline/` の各スクリプトは、部品として残る）。`init` の質問と、`config.example.json` の項目を、そろえ続ける必要がある。
- 残るもの（コードでは解けない）:
  - **ボットのトークンを、どう渡すか。** 同じトークンを配ると、ボットが見える範囲を、全員に渡すことになる（各自が自分のボットを作ると決めた。[ADR-0018](0018-per-person-bot-no-shared-host.md)）。
  - **Claude の契約。** どのアカウントで、メッセージを処理するか。使う人が選ぶ。
  - 古い鍵の名前を、いつ外すか。

## 採らなかった案

- **1 つのファイルにまとめた実行形式（zipapp）や、pip で入れるパッケージにする**: 受け取る側の手数は、zip と変わらない（Python は要る）。
  自動起動の登録が、ファイルの置き場所に依存するので、作りが増える。いまの規模では、フォルダを渡すほうが、中身を読めて、直しやすい。
- **設定を、質問ではなく、雛形の書き換えのままにする**: 直し忘れ（`REPLACE_WITH_...`）と、鍵のファイルの作り間違いが、いちばん多いつまずきだった。
- **鍵のファイルを 1 つにまとめる**: 受付が、Webhook と Claude の合鍵まで読める置き方になる。分けたままにする。
- **`init` に、Claude の合鍵の発行（`claude setup-token`）までやらせる**: ブラウザでの承認が要り、どのアカウントで動かすかを本人が選ぶ場面なので、
  本人の操作のままにする。

## 補足

- 一部を変えた ADR:
  - [ADR-0028](0028-distribute-with-git-clone.md): 配り方。zip ではなく `git clone`
- 関連: [ADR-0014](0014-poll-search-api-single-entry.md)、[setup.md](../setup.md)
