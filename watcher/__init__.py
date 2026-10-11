"""discord-mention-triage の受付。Discord の検索 API を読み取り専用で引き、
自分宛てのメンションを inbox/pending-*.json に書き出す。常駐しない（数分おきに 1 回動く）。

このパッケージは Discord へ何も書き込まない。Discord と通信するのは rest.py の GET 1 つだけで、
tests/test_readonly_guard.py が、コードの形で担保する。標準ライブラリのみ。
"""

__version__ = "0.2.0"
