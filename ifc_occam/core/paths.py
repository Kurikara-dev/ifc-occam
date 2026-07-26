"""出力先と入力ファイルの同一性判定(原本非破壊の契約を守るための唯一の判定器)。

このツールの契約は**原本非破壊**である。出力先が入力ファイルと同一実体を指した
場合の被害は経路によって異なるが、どちらも契約違反である:

- テキスト経路(`textops/rewrite.py`)は `open(out_path, "wb")` を
  `iter_records(src_path)` より先に実行するため、入力が1バイトも読まれる前に
  **truncate される**(実測: 21,529,266 bytes の入力が、例外も警告も出さずに
  453バイトへ破壊された上で「完了」と表示された)。
- フルオープン経路(`core/export.py`)は全体をメモリに読み込んでから書くため
  truncate はしないが、**原本を軽量化結果で上書きする**。

同じ判定を各所で独立に書くと、片方だけ修正されて食い違う。判定器はこの1箇所に
置き、ライブラリ層(`core/export.py`・`textops/rewrite.py`)と UI 層
(`cui/repl.py`)がこれを共有する。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["refers_to_same_file"]


def refers_to_same_file(a: str | Path, b: str | Path) -> bool:
    """a と b が同一のファイル実体を指しているかを判定する。

    判定順序(取り違えを避けるため): 両方が既に存在するなら
    `os.path.samefile`(inode/ファイルID比較。Windows の大文字小文字違い・
    8.3短縮名・シンボリックリンクを正しく同一実体と判定できる——パス文字列
    だけの比較では検出できない別名化に強い)。出力先はこの検証時点では通常
    まだ存在しない(まさにこれから書き込む対象)ため、その場合は
    `Path.resolve()` の比較にフォールバックする(同一の絶対パス文字列に
    正規化されるかどうかで判定する、通常ケース用の軽い比較)。

    衝突している場合は出力先が必然的に既存ファイルになるため、危険な側の
    判定は常に `os.path.samefile` を通る。`resolve()` 比較は「出力先が存在
    しない=衝突していない」通常ケース専用の経路である。
    """
    pa, pb = Path(a), Path(b)
    if pa.exists() and pb.exists():
        return os.path.samefile(pa, pb)
    return pa.resolve() == pb.resolve()
