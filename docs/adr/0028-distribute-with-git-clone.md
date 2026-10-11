---
status: accepted
date: 2026-10-11
---

# ADR-0028: 配るときは、公開リポジトリを `git clone` してもらう

## 背景と課題

- [ADR-0015](0015-one-command-one-folder.md) では、リポジトリが private で、clone に GitHub の権限が要ったので、フォルダを zip にして渡すことにした。
- リポジトリを公開したので、権限が無くても clone できる。README と setup.md は、すでに `git clone` を案内している。

## 決定

- **配るときは、公開リポジトリの URL を渡し、`git clone` してもらう。** 新しい版は `git pull` で取る。
- zip は、手元の PC から別の PC へ持ち込むときの手段として残す（[別の PC へ移す](../porting.md)）。

### 結果

- 得るもの: 新しい版が `git pull` で届く。どの版かを、コミットで言える。
- 引き受けるもの: 使う人の PC に Git が要る（README の「必要なもの」に書く）。

## 採らなかった案

- zip で渡し続ける: 新しい版のたびに作り直して渡すことになる。受け取った側は、手元がどの版かを言えない。
- リリースに zip を付ける: 作る手間が増える。いまは `git clone` で足りる。

## 補足

- 関連: [ADR-0015](0015-one-command-one-folder.md)（配り方を置き換える）、[README](../../README.md#セットアップ)、[setup.md](../setup.md)
