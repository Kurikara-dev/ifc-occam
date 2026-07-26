"""出力IFCヘッダへ刻印する由来情報(非正本マーク)の文字列を生成する
純粋関数 (cui-design.md、docs/plans/2026-07-25-cui-phase2.md Task 1、
docs/plans/2026-07-25-cui-phase3.md Task 4 監督者裁定1)。

## 切り出しの経緯と理由

文言・書式は `core/export.py` の `_stamp_provenance`(CUI Phase2 Task1、
フルオープン経路)が元々インラインで持っていたものを、挙動変更ゼロで
このモジュールへ抽出した。`core/export.py` は ifcopenshell の幾何スタック
(`ifcopenshell.geom` 等)を import する重量モジュールであり、CUI Phase3
(`textops/rewrite.py`)がそれを import すると「フルオープンを避ける」という
本フェーズの存在意義そのものを壊してしまう。そのため、由来刻印の文字列
生成だけを `datetime` と `ifc_occam.__version__` だけに依存する中立モジュール
として本ファイルに切り出し、`core/export.py`(フルオープン経路)と
`textops/rewrite.py`(テキスト経路)の両方から共用する。

**numpy も ifcopenshell も import しない**(このモジュールの import 文が
それを直接示す——textops側から見て安全であることの根拠)。

挙動不変の証明: `core/export.py:_stamp_provenance` はこのヘルパーを呼ぶだけに
書き換えられており、`tests/test_export.py` の刻印テスト群は1行も変更せずに
green のままである(docs/plans/2026-07-25-cui-phase3.md Task 4 参照)。

## エスケープはこのモジュールの責務ではない

戻り値はプレーンな Python `str` のタプルであり、STEPエスケープ(引用符の
二重化・非ASCII文字のエスケープ)は一切行わない。呼び出し側がそれぞれの
出力経路に応じた方法でエスケープする:
  - `core/export.py`(フルオープン経路): ifcopenshell の
    `header.file_description.description` セッターに文字列をそのまま渡し、
    STEPシリアライズ時のエスケープは ifcopenshell に委ねる。
  - `textops/rewrite.py`(テキスト経路): ifcopenshell を経由しないため、
    自前でSTEPエスケープ(引用符二重化 + 非ASCII エスケープ)を行った上で
    ヘッダのバイト列に直接埋め込む。非ASCIIエスケープは単一の `\\X2\\` では
    なく、BMP(U+0080〜U+FFFF)は `\\X2\\...\\X0\\`、非BMP(U+10000以上)は
    `\\X4\\...\\X0\\` に分岐する(2026-07-25レビューでの訂正——ifcopenshell
    0.8.5 が `\\X2\\` 内のサロゲートペアを合成復号できず該当文字を無音で
    消失させるため。詳細・実測根拠は `textops/rewrite.py` モジュール
    docstring「非ASCIIエスケープの分岐(修正1)」参照)。この訂正は
    `textops/rewrite.py` 側の実装にのみ関わり、本モジュール(エスケープ
    しない中立な文字列生成のみ)自体の挙動には影響しない。
"""

from __future__ import annotations

import datetime

from ifc_occam import __version__

__all__ = ["build_provenance_lines"]


def build_provenance_lines(
    source_name: str, deleted_count: int, simplified_count: int
) -> tuple[str, str, str]:
    """出力IFCヘッダの FILE_DESCRIPTION.description に追記する3行
    (非正本マーク + 出典 + 削除/簡略化件数)を返す。

    文言は cui-design.md / docs/plans/2026-07-25-cui-phase2.md Task 1 から
    verbatim(`core/export.py` の元実装から挙動変更なしで抽出)。

    呼び出し側の契約: deleted_count/simplified_count は呼び出し時点で確定済みの
    値を渡すこと(`core/export.py` では `apply_operations` の `deleted`/
    `simplified` の件数、`textops/rewrite.py` では
    `plan.stats["seeds"] + plan.stats["cascade"]` / 0 —— textopsは削除のみの
    フェーズのため simplified は常に0。理由はrewrite.pyのdocstring参照)。
    """
    today = datetime.date.today().isoformat()
    return (
        f"Lightweighted by IFC Occam {__version__} on {today}"
        " - non-authoritative derivative; verify against the source model",
        f"Source: {source_name}",
        f"Deleted {deleted_count} elements (incl. cascade); simplified {simplified_count}",
    )
