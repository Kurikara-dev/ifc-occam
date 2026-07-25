"""IFC抽出層 (design.md §4.3)。ifcopenshell 依存をこのモジュールに隔離する。"""

from __future__ import annotations

import os
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import numpy as np

from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo


def _analyze_representation(
    rep,
) -> tuple[tuple[str, ...], bool, str | None]:
    """製品の Representation から representation_types / is_mapped / layer を導出する。

    IfcMappedItem の場合は共有ターゲット (MappingSource.MappedRepresentation) の
    RepresentationType を実際の幾何種別として使う("MappedRepresentation" という
    ラッパー名ではなく "SweptSolid" 等)。
    """
    if rep is None:
        return (), False, None

    types: list[str] = []
    is_mapped = False
    layer: str | None = None

    for r in getattr(rep, "Representations", []) or []:
        items = getattr(r, "Items", []) or []
        if not items:
            types.append(r.RepresentationType)
        for item in items:
            if item.is_a("IfcMappedItem"):
                is_mapped = True
                target = item.MappingSource.MappedRepresentation
                types.append(target.RepresentationType)
            else:
                types.append(r.RepresentationType)

        if layer is None:
            try:
                assignments = r.LayerAssignments
            except Exception:
                assignments = ()
            if assignments:
                layer = assignments[0].Name

    # 順序を保ったまま重複除去
    unique_types = tuple(dict.fromkeys(types))
    return unique_types, is_mapped, layer


def _shape_from_geometry(geometry) -> ShapeInfo:
    shape_id = str(geometry.id)
    verts = np.array(geometry.verts, dtype=np.float64).reshape(-1, 3)
    faces = np.array(geometry.faces, dtype=np.int64).reshape(-1, 3)
    return ShapeInfo(shape_id=shape_id, vertices=verts, faces=faces)


def _placement_from_matrix(matrix) -> np.ndarray:
    """ifcopenshell の transformation.matrix を (4,4) 同次行列に正規化する。

    ifcopenshell 0.8.5 では flat な16要素の列優先(column-major)リストで返る
    (実データで検証済み: scripts/investigate_shape_sharing.py の調査結果)。
    reshape(4, 4, order="F") で行優先の (4,4) 同次行列に変換すると、
    最下行が正しく [0, 0, 0, 1] になる。
    """
    flat = np.array(list(matrix), dtype=np.float64)
    return flat.reshape(4, 4, order="F")


def extract_model(source: str | Path | ifcopenshell.file) -> tuple[ModelData, list[str]]:
    """IFCファイルを読み込み、ModelData と幾何化失敗の警告リストを返す。

    source はパス(str/Path)または既に開いた ifcopenshell.file を受け付ける。
    後者の場合は再オープンせず、そのまま使う(サーバが load 時に1度だけ open した
    ファイルオブジェクトを再利用するため)。

    座標系はローカル(配置変換なし)。ワールド座標変換は行わない
    (design.md §4.3: 重複検出の平行移動不変性のため)。
    """
    model = source if isinstance(source, ifcopenshell.file) else ifcopenshell.open(str(source))
    schema = model.schema

    settings = ifcopenshell.geom.settings()
    settings.set("weld-vertices", True)
    # use-world-coords はデフォルトで無効(ローカル座標を使う)。明示的に有効化しない。

    products = list(model.by_type("IfcProduct"))

    warnings: list[str] = []
    shapes: dict[str, ShapeInfo] = {}
    guid_to_shape_id: dict[str, str] = {}
    guid_to_placement: dict[str, np.ndarray] = {}

    num_threads = os.cpu_count() or 1
    iterator = ifcopenshell.geom.iterator(settings, model, num_threads)
    if iterator.initialize():
        while True:
            try:
                elem = iterator.get()
                geometry = elem.geometry
                shape_id = str(geometry.id)
                if shape_id not in shapes:
                    shapes[shape_id] = _shape_from_geometry(geometry)
                guid_to_shape_id[elem.guid] = shape_id
                guid_to_placement[elem.guid] = _placement_from_matrix(
                    elem.transformation.matrix
                )
            except Exception as exc:  # noqa: BLE001 - イテレータ途中の失敗は警告に積んで継続する
                warnings.append(
                    f"ジオメトリイテレータが要素の処理中に失敗しました: {exc}"
                )
            try:
                if not iterator.next():
                    break
            except Exception as exc:  # noqa: BLE001 - next() も失敗したら走査を打ち切る
                warnings.append(
                    f"ジオメトリイテレータの next() が失敗したため走査を中断します: {exc}"
                )
                break
    else:
        warnings.append(
            "geometry iterator failed to initialize; falling back to per-element processing"
        )

    # iterator は失敗した要素を黙ってスキップするため、Representation を持つのに
    # 幾何が得られなかった要素だけ個別に再試行し、失敗理由を警告として記録する。
    for product in products:
        rep = getattr(product, "Representation", None)
        if rep is None:
            continue
        global_id = product.GlobalId
        if global_id in guid_to_shape_id:
            continue
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            shape_info = _shape_from_geometry(shape.geometry)
            shapes[shape_info.shape_id] = shape_info
            guid_to_shape_id[global_id] = shape_info.shape_id
            guid_to_placement[global_id] = _placement_from_matrix(
                shape.transformation.matrix
            )
        except Exception as exc:  # noqa: BLE001 - 幾何化失敗は警告に積んで続行する
            warnings.append(
                f"GlobalId={global_id} ({product.is_a()}): 幾何生成に失敗しました: {exc}"
            )

    elements: list[ElementInfo] = []
    for product in products:
        rep = getattr(product, "Representation", None)
        representation_types, is_mapped, layer = _analyze_representation(rep)
        elements.append(
            ElementInfo(
                global_id=product.GlobalId,
                ifc_class=product.is_a(),
                name=getattr(product, "Name", None),
                shape_id=guid_to_shape_id.get(product.GlobalId),
                is_mapped=is_mapped,
                representation_types=representation_types,
                layer=layer,
                placement=guid_to_placement.get(product.GlobalId),
            )
        )

    return ModelData(schema=schema, elements=elements, shapes=shapes), warnings


def extract_elements_light(ifc_file) -> list[tuple[str, str, str | None]]:
    """(global_id, ifc_class, name) の列を返す軽量要素列挙 (design.md §5)。

    IfcProduct を素朴に列挙するだけで、ifcopenshell.geom には一切触れない
    (settings/iterator/create_shape のいずれも呼ばない)。CUIが一度だけ
    フルオープンした後、対話で決めた対象GlobalIdの実在確認や表示名解決に
    使う軽量経路(メッシュ化コストが掛かる extract_model とは別用途)。
    """
    return [
        (product.GlobalId, product.is_a(), getattr(product, "Name", None))
        for product in ifc_file.by_type("IfcProduct")
    ]
