---
status: accepted
date: 2026-10-11
---

# ADR-0027: `wiki/` をやめて `docs/` に 1 本化し、1 つの話は 1 ページにだけ書く

## 背景と課題

- 説明が、README、`wiki/`、`docs/` の 3 段になっていて、同じ話が 2〜3 か所にあった。
  しくみは README の「しくみ」、`wiki/how-it-works.md`、`docs/architecture.md`。安全は `wiki/promises.md` と `docs/security.md`。
  困ったときは README、`wiki/when-notified.md`、`docs/setup.md` の「止まったとき」。手順は README、`wiki/setup-guide.md`、`docs/setup.md`。
- umezy さんから、2026-10-11 に「そもそも wiki と README の役割が被っている」と指摘があった。
- `wiki/` という名前は、GitHub の Wiki 機能（本体とは別のリポジトリ）とも紛らわしい。スキルが読むページは、`git clone` で手元に来る必要がある。

## 決定

- **README と `docs/` の 2 段にする。** README は、何の物か、どう始めるか、困ったときの入口と要約。その先は、すべて `docs/`。
- **1 つの話は、1 ページにだけ書く。** wiki のページは、`docs/` の同じ話のページにまとめた。
  しくみ（`how-it-works.md`。旧 architecture を含む）、安全面（`security.md`。約束を含む）、Discord 側と鍵を用意する（`get-the-keys.md`）、
  コマンドで進める（`setup.md`）、困ったとき（`troubleshooting.md`。通知と、自分で調べる見方）。
- README の「結果が届かない」の折りたたみは、`troubleshooting.md` と重なるので外し、リンクにした。
- GitHub の Wiki 機能は使わない。

### 結果

- 得るもの: 書く場所の迷いが無くなる。直す所が 1 か所になる。
- 引き受けるもの: README の要約（しくみ、鍵の扱い、よくある `[NG]`）と、`docs/` の詳しいページの両方に、同じ事実が短く出る所は残る。変えるときは、両方を直す。

## 採らなかった案

- 3 段のまま、重なりだけを削る: どの段に何を書くかの線引きが、また崩れる。
- GitHub の Wiki 機能に移す: `git clone` で手元に来ず、スキルが読めない。コードと同じ変更で直せず、テストも届かない。

## 補足

- 関連: [ADR-0025](0025-readme-from-the-engineer-setup-page.md)（wiki の部分を置き換える）、[ADR-0026](0026-docs-carry-only-software-reasons.md)、[docs の目次](../README.md)
