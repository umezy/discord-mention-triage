# 別の PC へ移す

いま動いている PC から、別の PC へ、この仕組みを移すときの手順。はじめて入れるときは [setup.md](setup.md)。

**同じボットのトークンで、2 台を同時に動かさない。** 同じメンションの結果が、2 通届く。古い PC を止めてから、新しい PC を動かし始める。

他の人の PC へ入れるときは、「移す」ではなく、その人の新しい導入になる（ボット・ユーザー ID・Webhook・Claude のログインが、その人のもの）。
**ボットも、その人が自分で作る。あなたのボットのトークンは、渡さない**（[ADR-0018](adr/0018-per-person-bot-no-shared-host.md)。作り方は [Discord 側と鍵を用意する](get-the-keys.md)）。

## 手順

| | どちらの PC で | すること |
|---|---|---|
| 1 | 新しい PC | Python（3.10 以上）、Git、`claude` を入れる。`claude` と打って、ブラウザで一度ログインする（[`claude` を入れる](setup.md#claude-を入れる)） |
| 2 | 新しい PC | フォルダを置く。`git clone`、または、古い PC で作った zip（`git archive --format=zip -o discord-mention-triage.zip HEAD`）を展開する |
| 3 | 新しい PC | `python triage.py init`。ボットのトークンと Webhook の URL は、パスワードマネージャーなどから貼る（**チャットや Discord で送らない**）。Claude の合鍵は、**新しい PC で** `claude setup-token` を実行して作る |
| 4 | 新しい PC | `python triage.py check`。`[NG]` が無くなるまで直す。`read_dirs`（Claude が読んでよいフォルダ）は、新しい PC のパスにする |
| 5 | 古い PC | **止める。** `python triage.py uninstall`。`python triage.py status` で「登録されていない」と出ることを確かめる |
| 6 | 両方（任意） | 状態を引き継ぐ。古い PC の `%USERPROFILE%\.discord-mention-triage\<名前>\` にある `state.json`（もう拾ったメンションの控え）と `poll.json`（どこまで読んだか）を、新しい PC の同じ場所へコピーする。止めていた間の分も、二重にならずに拾える |
| 7 | 新しい PC | `python triage.py run`。6 をしなかったときは、止めていた時間ぶんを足して `python triage.py run --backfill-hours 6` のようにする（しないと、「いまから」見張りを始める） |
| 8 | 新しい PC | 別のアカウントから自分にメンションしてもらい、2〜3 分待って、もう一度 `python triage.py run`。専用チャンネルに届くことを確かめる |
| 9 | 新しい PC | `python triage.py install`。翌日、`python triage.py status` で、動き続けているかを見る |
| 10 | 古い PC | 1 日問題が無ければ、`%USERPROFILE%\.discord-mention-triage\` を消す（鍵と、メッセージの本文が入っている） |
