"""PyInstaller onedir ビルドのエントリポイント。

無引数(start-exe.bat のダブルクリック)なら GUI(serve)、サブコマンド付きなら
それをそのまま実行する。分岐は cli.resolve_entry_argv(pytest でテスト済みの
純粋関数)に委ねる。かつては serve 決め打ちで、exe から CUI が起動できなかった
(docs/testing-guide.md 7.1 の exe 手順が実態と食い違っていた)。
"""

from __future__ import annotations

import sys

from ifc_occam import cli

if __name__ == "__main__":
    cli.main(cli.resolve_entry_argv(sys.argv[1:]))
