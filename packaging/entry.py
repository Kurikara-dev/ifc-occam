"""PyInstaller onedir ビルドのエントリポイント。

`python -m ifc_occam serve` 相当を、フリーズされた exe から起動できるようにする。
"""

from __future__ import annotations

import sys

from ifc_occam import cli

if __name__ == "__main__":
    cli.main(["serve", *sys.argv[1:]])
