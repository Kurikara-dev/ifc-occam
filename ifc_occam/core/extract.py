"""IFC抽出層 (design.md §4.3)。ifcopenshell 依存をこのモジュールに隔離する。"""

from __future__ import annotations

import os
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import numpy as np

from ifc_occam.core.types import ElementInfo, ModelData, ShapeInfo

# geomが子アイテムのみのスタイルを解決できない場合のフォールバックに、simplify.pyの
# 既存スタイル探索ヘルパ(Task3で作成・テスト済み)を再利用する。同じ探索ロジックを
# ここに複製しない。
from ifc_occam.core.simplify import _resolve_surface_rgb, _styled_items_in_subtree


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


def _dominant_diffuse(geometry) -> tuple[float, float, float] | None:
    """shape の geometry から、面数が最も多い material の拡散色を返す。

    要素内で面ごとに色が違う場合は多数決で1色に潰す。ビューアの塗り分けが
    要素単位(vertex_start/vertex_count)であり、面単位の色を保持できないため。
    materials が空、または diffuse が取れなければ None。

    ifcopenshell 0.8.5 の style.diffuse は colour オブジェクトで、r/g/b は
    プロパティではなく *メソッド* である(`diffuse.r()`)。tuple() では展開できない。
    """
    materials = list(geometry.materials)
    if not materials:
        return None
    ids = np.asarray(geometry.material_ids, dtype=np.int64)
    valid = ids[(ids >= 0) & (ids < len(materials))]
    if valid.size:
        index = int(np.bincount(valid).argmax())
    elif ids.size == 0 and len(materials) == 1:
        # 面ごとの割り当てが無く material が1つだけなら、それが形状全体の色。
        index = 0
    else:
        # 面が1つも指していない material しか無い。支持のない色を「多数決の勝者」と
        # して返すと、色情報が無いのに色があることになるため None を返す。
        return None
    diffuse = getattr(materials[index], "diffuse", None)
    if diffuse is None:
        return None
    return (float(diffuse.r()), float(diffuse.g()), float(diffuse.b()))


def _graph_fallback_diffuse(product) -> tuple[float, float, float] | None:
    """_dominant_diffuse が None を返した要素だけに使う、エンティティグラフ直接探索の
    フォールバック。

    geom(ifcopenshell.geom)は IfcStyledItem がトップレベルの representation item
    ではなく内側の子アイテム(IfcClosedShell等)に付いている場合、そのスタイルを
    解決できない(実測で確認: 合成フィクスチャで geometry.materials が空になる。
    small.ifc実データではRebroがトップレベルにも同じスタイルを冗長に付けているため
    表面化していなかったが、他の作図ツールの出力では黙って色が抜ける恐れがある)。

    product の Body representation の Items を起点に _styled_items_in_subtree
    (simplify.py、Task3で作成・テスト済み)で子孫まで辿り、最初に見つかった
    IfcStyledItem の Styles から _resolve_surface_rgb で RGB を取る。
    _styled_items_in_subtree は自分(トップレベル)->子の先行順で返すため、
    トップレベルにスタイルがあればそれが優先される。取れなければ None。

    全要素で毎回エンティティグラフを辿ると読込時間が伸びるため、呼び出し側
    (extract_model)は _dominant_diffuse が None を返した要素にだけ限定して呼ぶこと。
    """
    rep = getattr(product, "Representation", None)
    if rep is None:
        return None
    for r in getattr(rep, "Representations", []) or []:
        if getattr(r, "RepresentationIdentifier", None) != "Body":
            continue
        for item in getattr(r, "Items", []) or []:
            for styled_item in _styled_items_in_subtree(item):
                for style in styled_item.Styles or []:
                    rgb = _resolve_surface_rgb(style)
                    if rgb is not None:
                        return rgb
    return None


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
    # apply-default-materials を False にすると、スタイルを解決できなかった要素に
    # 無彩色の "DefaultMaterial"(実測値: diffuse=(0.7, 0.7, 0.7))を合成しなくなり、
    # geometry.materials が単に空になる。これにより「本物の色が無い」ことを
    # 誤りなく判定でき、_graph_fallback_diffuse を呼ぶべきかどうかの判断に使える。
    # small.ifc の全1381要素(bulk iterator経由)で True/False を比較し、
    # vertices/faces/material_ids/materials件数のいずれも変化しないことを実測で
    # 確認済み(実データはこの合成に依存していない)。
    settings.set("apply-default-materials", False)

    products = list(model.by_type("IfcProduct"))

    warnings: list[str] = []
    shapes: dict[str, ShapeInfo] = {}
    guid_to_shape_id: dict[str, str] = {}
    guid_to_placement: dict[str, np.ndarray] = {}
    guid_to_color: dict[str, tuple[float, float, float] | None] = {}

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
                guid_to_color[elem.guid] = _dominant_diffuse(geometry)
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
            guid_to_color[global_id] = _dominant_diffuse(shape.geometry)
        except Exception as exc:  # noqa: BLE001 - 幾何化失敗は警告に積んで続行する
            warnings.append(
                f"GlobalId={global_id} ({product.is_a()}): 幾何生成に失敗しました: {exc}"
            )

    elements: list[ElementInfo] = []
    for product in products:
        rep = getattr(product, "Representation", None)
        representation_types, is_mapped, layer = _analyze_representation(rep)
        shape_id = guid_to_shape_id.get(product.GlobalId)
        color = guid_to_color.get(product.GlobalId)
        if color is None and shape_id is not None:
            # geom は幾何(shape_id)は作れたが色は解決できなかった。子アイテムのみの
            # スタイルを見落としている可能性があるため、エンティティグラフを直接
            # 辿って探す(幾何が無い要素はどうせ描画されないので試さない)。
            try:
                color = _graph_fallback_diffuse(product)
            except Exception as exc:  # noqa: BLE001 - 色が取れないだけで抽出全体を止めない
                # 上の2つのループと同じ方針。壊れたスタイルグラフを持つ1要素のせいで
                # ファイル全体の読込が0要素で失敗するのを防ぐ(色は None のまま続行)。
                warnings.append(
                    f"GlobalId={product.GlobalId} ({product.is_a()}): "
                    f"スタイルからの色の解決に失敗しました: {exc}"
                )
        elements.append(
            ElementInfo(
                global_id=product.GlobalId,
                ifc_class=product.is_a(),
                name=getattr(product, "Name", None),
                shape_id=shape_id,
                is_mapped=is_mapped,
                representation_types=representation_types,
                layer=layer,
                placement=guid_to_placement.get(product.GlobalId),
                color=color,
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
