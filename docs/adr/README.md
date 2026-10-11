# ADR（設計上の決定の記録）

「何を決めたか」だけでなく、「なぜそう決めたか」「何を捨てたか」を残す。1 つの決定につき 1 ファイル。
形は [MADR](https://adr.github.io/madr/) 4.0 に合わせている（[ADR-0029](0029-adopt-madr.md)）。

## 一覧

| 番号 | 決定 | 状態 | 日付 |
|---|---|---|---|
| [0001](0001-record-decisions-as-adr.md) | 決定は ADR に残す | accepted（一部を ADR-0029・ADR-0030 で変更） | 2026-10-04 |
| [0003](0003-scheduler-outside-claude.md) | 定期実行は Claude の外に置き、新着があるときだけ `claude -p` を起こす | accepted（一部を ADR-0014 で変更） | 2026-10-03 |
| [0004](0004-unattended-triage-without-pr-review.md) | 仕分けは無人化し、PR の自動レビューは無効にする | accepted | 2026-10-03 |
| [0005](0005-minimal-claude-permissions.md) | 仕分けの Claude に渡す権限を最小にする | accepted（一部を ADR-0012 で変更） | 2026-10-03 |
| [0008](0008-keep-session-transcripts.md) | セッション記録は本番も残す | accepted | 2026-10-04 |
| [0009](0009-import-sessions-into-desktop.md) | 仕分けのたびに、セッションをデスクトップ版へ取り込む | accepted（一部を ADR-0021・ADR-0015 で変更） | 2026-10-04 |
| [0011](0011-wait-when-claude-never-ran.md) | モデルが動く前のエラーは、失敗として数えずに待つ | accepted | 2026-10-05 |
| [0012](0012-pin-unattended-runs-to-a-token.md) | 無人実行は専用の長期トークンで動かし、`claude` のログインに頼らない | accepted | 2026-10-05 |
| [0013](0013-no-pr-review-path-until-enabled.md) | PR レビュー用の先行実装は、有効にする決定まで経路から外す | accepted | 2026-10-09 |
| [0014](0014-poll-search-api-single-entry.md) | 取得は検索 API を数分おきに引く形にし、常駐をやめて、入口を 1 つにする | accepted（一部を ADR-0022 で変更） | 2026-10-09 |
| [0015](0015-one-command-one-folder.md) | 入口のコマンドを 1 つ、設定と鍵の置き場を 1 つにする。配るときは、フォルダをそのまま渡す | accepted（一部を ADR-0028 で変更） | 2026-10-10 |
| [0016](0016-short-readme-details-in-wiki.md) | README は「課題とソリューション」と「セットアップ」だけにし、詳しいことは wiki に書く | superseded by ADR-0025 | 2026-10-10 |
| [0017](0017-setup-as-a-skill.md) | セットアップは、リポジトリに同梱したスキルで、Claude Code に進めてもらう | accepted（一部を ADR-0025・ADR-0027 で変更） | 2026-10-10 |
| [0018](0018-per-person-bot-no-shared-host.md) | ほかの人が使うときも、各自が自分のボットを作る。共有のホストは作らない | accepted | 2026-10-10 |
| [0019](0019-collect-keys-with-chrome-in-the-skill.md) | 鍵と ID を集める所も、スキルが、相手の Chrome で一緒に進める。README のセットアップの図は外す | accepted（一部を ADR-0025・ADR-0027 で変更） | 2026-10-10 |
| [0020](0020-keep-the-bot-no-user-token.md) | メンションの取得には、ボットを使い続ける。本人のログイントークンで読む経路は、採らない | accepted | 2026-10-10 |
| [0021](0021-readable-session-names.md) | セッションの名前は、送り主・用件の頭・場所から作る。通す文字は、決め打ちにする | accepted | 2026-10-10 |
| [0022](0022-no-settle-wait.md) | 投稿から 60 秒待つのを、やめる（検索に出るまでの約 100 秒が、様子見を兼ねる） | accepted | 2026-10-10 |
| [0023](0023-skill-handles-after-setup.md) | 動き出したあとの相談（状態を見る・調べる・外す）も、セットアップのスキルが受ける | accepted | 2026-10-10 |
| [0024](0024-no-ping-when-nothing-to-do.md) | 対応不要だけの配達には、メンション（通知）を付けない | accepted | 2026-10-10 |
| [0025](0025-readme-from-the-engineer-setup-page.md) | README は、エンジニア向けのセットアップのページを元に作り、README だけで始められるようにする | accepted（一部を ADR-0027 で変更） | 2026-10-10 |
| [0026](0026-docs-carry-only-software-reasons.md) | 文書には、ソフトとしての理由だけを書く。リポジトリは 1 つのまま | accepted | 2026-10-11 |
| [0027](0027-one-docs-folder-no-wiki.md) | `wiki/` をやめて `docs/` に 1 本化し、1 つの話は 1 ページにだけ書く | accepted | 2026-10-11 |
| [0028](0028-distribute-with-git-clone.md) | 配るときは、公開リポジトリを `git clone` してもらう | accepted | 2026-10-11 |
| [0029](0029-adopt-madr.md) | ADR は MADR 4.0 の形で書き、決定の一部を変えたときも、古い ADR に記す | accepted | 2026-10-11 |
| [0030](0030-redact-past-adrs.md) | 秘密、組織の事情、ほかの人の名前は、過去の ADR からも消してよい | accepted | 2026-10-11 |

0002・0006・0007・0010 は欠番（公開版には含めていない。[ADR-0026](0026-docs-carry-only-software-reasons.md)）。

## 書き方

[template.md](template.md) を `NNNN-短い英語の題.md`（次の番号）に写して書き、上の一覧に 1 行足す。1 画面に収まる長さにする。

- `status` は `proposed` / `accepted` / `rejected` / `deprecated` / `superseded by ADR-NNNN` のどれか。
- `date` は決めた日。

## 運用のルール

- **過去の ADR は書き換えない。** 決定を変えるときは、新しい ADR を足す。
  - 丸ごと変えたら、古い方の `status` を `superseded by ADR-NNNN` にする。
  - 一部だけ変えたら、古い方の front matter に `amended-by: [ADR-NNNN]` を足し、補足に何を変えたかを書く（[ADR-0029](0029-adopt-madr.md)）。
  - どちらも、新しい ADR と同じ変更で直す。
- そのまま直してよいのは、誤字、リンク切れ、秘密・組織の事情・ほかの人の名前（[ADR-0030](0030-redact-past-adrs.md)）。
- まだ決まっていないことは [未決事項](../open-questions.md) に書く。決まったら、ADR を足し、未決事項から外す。
- 実装の細かい手順や使い方は、ADR ではなく [しくみ](../how-it-works.md) / [setup.md](../setup.md) / [security.md](../security.md) に書く。
  ADR は「なぜ」を残す場所。
