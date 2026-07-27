"""共有データ構造 (design.md §3)。全フェーズの契約。"""

from dataclasses import dataclass

import numpy as np


@dataclass
class ShapeInfo:
    """1つの幾何実体。共有形状なら複数要素から参照される。"""

    shape_id: str  # 抽出層が付与する一意キー
    vertices: np.ndarray  # (n, 3) float64。ローカル座標(配置変換を含まない)
    faces: np.ndarray  # (m, 3) int64。三角形インデックス

    @property
    def triangle_count(self) -> int:
        return len(self.faces)


@dataclass
class ElementInfo:
    """1つのIFC製品インスタンス。幾何は shape_id 経由で参照。"""

    global_id: str
    ifc_class: str  # 例 "IfcFlowFitting"
    name: str | None
    shape_id: str | None  # 幾何なし要素は None
    is_mapped: bool  # IfcMappedItem (共有形状) 経由か
    representation_types: tuple[str, ...]  # 例 ("SweptSolid",)
    layer: str | None  # IfcPresentationLayerAssignment 名
    placement: np.ndarray | None = None  # (4,4) float64 同次変換行列。幾何なし要素は None
    color: tuple[float, float, float] | None = None
    """IFCのスタイルから解決した拡散色(sRGB, 0..1)。スタイルが無ければ None。

    値は IfcColourRgb の生値(表示色=sRGB)。線形化はしない。表示側の責務とする。
    """


@dataclass
class ModelData:
    """抽出結果一式。計算層への入力。"""

    schema: str  # "IFC4" | "IFC2X3"
    elements: list[ElementInfo]
    shapes: dict[str, ShapeInfo]  # shape_id → ShapeInfo


@dataclass
class ClassStats:
    """診断: クラス別集計行。"""

    ifc_class: str
    element_count: int
    unique_shape_count: int
    total_triangles: int  # Σ(要素ごとの形状三角形数) = 展開後総三角形数
    mapped_count: int  # 共有形状経由の要素数
    max_single_shape_triangles: int
