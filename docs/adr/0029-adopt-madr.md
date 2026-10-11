---
status: accepted
date: 2026-10-11
---

# ADR-0029: ADR は MADR 4.0 の形で書き、決定の一部を変えたときも、古い ADR に記す

## 背景と課題

- ADR は、独自の形（箇条書きの状態・日付・関連と、背景・決定・検討した代替案・結果）で書いていた。
  状態は「採用（〇〇は ADR-MMMM で置き換えた。…）」のような文で、決まった値が無かった。
- 公開の前に読み直すと、あとの ADR で決定の一部が変わったのに、状態が「採用」のままのものが 5 本あった（0005、0009、0014、0017、0019）。
  決まりは「決定を変えたら、古い方を置き換え済みにする」だけで、一部だけ変えたときの書き方が無かった。
- 公開されている ADR の書き方では、[MADR](https://adr.github.io/madr/)（4.0、2024 年）が広く使われている。
  状態を front matter に決まった値で書き、置き換えは `superseded by ADR-NNNN` と書く。
  一部の変更は、[adr-tools](https://github.com/npryce/adr-tools) が「Amends / Amended by」の対で記す。

## 決定

- **ADR は、MADR 4.0 の形で書く。** 雛形は [template.md](template.md)。
  - front matter に `status`（`proposed` / `accepted` / `rejected` / `deprecated` / `superseded by ADR-NNNN`）と `date` を書く。
  - 節は、背景と課題、決定（その下に結果）、採らなかった案、補足（関連と、一部を変えた ADR）。
- **決定の一部を変えたときは、古い ADR の front matter に `amended-by` を足し、補足に何を変えたかを書く。** 新しい ADR と同じ変更で直す。
- これまでの ADR も、この形に組み替えた。中身は変えていない。

### 結果

- 得るもの: 外から読む人が、見慣れた形で読める。古い ADR を開いたとき、どこがもう有効でないかが、先頭で分かる。
- 引き受けるもの: 新しい ADR を書くたびに、関連する古い ADR を開いて、`amended-by` を足す手間が増える。

## 採らなかった案

- 独自の形のまま、状態の文を直す: 一部の変更の書き方を、また自分で決めることになる。
- 状態と中身の食い違いを、テストで見つける: 「一部を変えた」かどうかは、機械では決められない。
- [adr-kit](https://github.com/rvdbreemen/adr-kit) のような道具で、ADR をフックや CI で守らせる: この規模では、道具を入れて保守する手間のほうが大きい。

## 補足

- 関連: [ADR-0001](0001-record-decisions-as-adr.md)（運用のルールを補う）、[ADR の書き方](README.md)
