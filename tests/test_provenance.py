"""core/provenance.py(由来刻印の文字列生成)のTDD。

`export.py:_stamp_provenance`(CUI Phase2 Task1)から挙動変更ゼロで抽出された
純粋関数 `build_provenance_lines`。textops(CUI Phase3、フルオープンを避ける
ことが存在意義)から安全にimportできる中立モジュールであること
(datetime/ifc_occam.__version__以外に依存しない)が監督者裁定の要件であり、
本モジュール自体はそれをコード上で自明に満たす(numpy/ifcopenshellをimport
しない)。挙動不変の主な証明は tests/test_export.py の刻印テスト群が
無変更でgreenのままであること(docs/plans/2026-07-25-cui-phase3.md Task 4 参照)。
"""

from __future__ import annotations

import datetime

from ifc_occam import __version__
from ifc_occam.core.provenance import build_provenance_lines


def test_build_provenance_lines_matches_expected_template():
    lines = build_provenance_lines("model.ifc", deleted_count=5, simplified_count=2)
    today = datetime.date.today().isoformat()
    assert lines == (
        f"Lightweighted by IFC Occam {__version__} on {today}"
        " - non-authoritative derivative; verify against the source model",
        "Source: model.ifc",
        "Deleted 5 elements (incl. cascade); simplified 2",
    )


def test_build_provenance_lines_returns_plain_str_tuple_of_three():
    lines = build_provenance_lines("x.ifc", 0, 0)
    assert isinstance(lines, tuple)
    assert len(lines) == 3
    assert all(isinstance(s, str) for s in lines)


def test_build_provenance_lines_does_not_escape_source_name():
    """エスケープは呼び出し側の責務(モジュールdocstring参照)。ここでは
    非ASCII/クォートを含む文字列もそのまま素通りさせることを確認する
    (呼び出し側: core/export.pyはifcopenshellのheader setterに委ね、
    textops/rewrite.pyは自前でSTEPエスケープする)。"""
    lines = build_provenance_lines("図面's data.ifc", 1, 0)
    assert lines[1] == "Source: 図面's data.ifc"


def test_build_provenance_lines_zero_counts():
    lines = build_provenance_lines("x.ifc", 0, 0)
    assert lines[2] == "Deleted 0 elements (incl. cascade); simplified 0"
