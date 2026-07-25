"""出力時共有形状化 (design.md §5.4, Phase4 Task2)。

duplicates.find_duplicates が検出した各群を入力に、代表メンバーの幾何から
IfcRepresentationMap を1つ作成し、群の各メンバー要素の Body 表現を
IfcMappedItem(MappingTarget=平行移動のみ)に差し替える(ファイルサイズ削減)。

MappingTarget の導出 (実測で検証済み、design.md §5.4 契約通り):
  代表メンバーのローカル頂点 rep_verts の重心を rep_centroid とすると、
  MappedRepresentation(共有ソース)には rep_verts - rep_centroid (重心が原点) を
  格納する。各メンバー(代表自身も含む)のローカル頂点 member_verts は、
  duplicates.py が平行移動不変で検出しているため
      member_verts = (rep_verts - rep_centroid) + member_centroid
  という関係にある(重心を引いた形が全メンバーで一致するのが検出条件)。
  よって MappingTarget の平行移動は「代表重心との差」ではなく
  「メンバー自身のローカル重心 member_centroid」そのものである
  (代表自身もメンバーの1つとして rep_centroid を平行移動に使う)。

各メンバー要素の ObjectPlacement は変更しない(Body 表現の Items のみ差し替える)。
群メンバーに削除済み(ifc_file に存在しない)要素や Body representation を持たない
要素が含まれる場合は、その群全体をスキップする(部分適用による不整合を避ける)。
shape_id が1つしかない、または解決できる要素が2件未満の群は対象外とする。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ifc_occam.core.duplicates import DuplicateGroup
from ifc_occam.core.simplify import (
    _build_representation_items,
    _cleanup_items,
    _find_body_shape_representation,
    _representation_type_for_schema,
    _styled_items_for_item,
    _verts_to_native_units,
    style_signature,
    styles_match,
)
from ifc_occam.core.types import ModelData


@dataclass
class ConsolidateReport:
    """consolidate_duplicates の結果報告。"""

    groups_applied: int = 0
    elements_remapped: int = 0
    warnings: list[str] = field(default_factory=list)


# 選別ルール(design.md §5.4追補, Phase4 Task2: 「小さい群の統合でファイルサイズが
# 純増する」という実測結果を受けた供監督判断)。
#
# 校正根拠(開発時の使い捨て計測スクリプトで実測、IFC4 STEPテキストのバイト数):
#   IfcCartesianPointList3D への1頂点(realistic float, 座標3つ)追加コストは
#   実測 ~33 バイト/頂点(量子化なしの短い座標)だが、実際の consolidate 対象では
#   頂点数に比例して CoordIndex(三角形)も増えるため、削減される旧ジオメトリ全体
#   (座標配列+面インデックス)を「頂点数だけの一次関数」として近似すると、
#   n_members×n_verts を変えた22パターンの前後バイト数差を最小二乗フィットして
#   BYTES_PER_VERTEX ≈ 73、MEMBER_OVERHEAD_BYTES ≈ 160 と求まった
#   (フィット残差はいずれも数%以内)。仕様のガイド値(30-60/200-400)より実測値が
#   高めだったため、実測を優先しつつ安全側(小さめの節約見込み)に丸めている:
#     BYTES_PER_VERTEX = 70 (実測73を保守的に丸め)
#     MEMBER_OVERHEAD_BYTES = 160
#       (= IfcMappedItem + IfcCartesianTransformationOperator3D + IfcCartesianPoint
#          の3エンティティ分。IfcRepresentationMap等の群固定オーバーヘッドは
#          頂点数に対して無視できる小さな定数なので、メンバー単価に含めない)
BYTES_PER_VERTEX = 70
MEMBER_OVERHEAD_BYTES = 160


def _estimate_savings_and_overhead(vertex_count: int, member_count: int) -> tuple[int, int]:
    """群を共有化した場合の推定節約バイト数と推定オーバーヘッドバイト数を返す。

    savings: (member_count - 1) 件分の旧ジオメトリ(頂点配列相当)が削除される分。
    overhead: member_count 件分の IfcMappedItem 一式(薄い参照3エンティティ/件)。
    """
    savings = (member_count - 1) * vertex_count * BYTES_PER_VERTEX
    overhead = member_count * MEMBER_OVERHEAD_BYTES
    return savings, overhead


def _shape_id_to_gids(model: ModelData) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for elem in model.elements:
        if elem.shape_id is not None:
            mapping.setdefault(elem.shape_id, []).append(elem.global_id)
    return mapping


def _create_representation_map(ifc_file, verts, faces, context, styles=None):
    """代表メンバーの幾何から共有 IfcRepresentationMap を作る。

    styles(IfcSurfaceStyle等のリスト)が渡された場合、新規に作成した共有ソース
    アイテムへ IfcStyledItem(既存のstyleエンティティを再利用、複製しない)を1つ
    付与する(代表メンバーの色を共有アイテムへ引き継ぐ)。
    """
    items = _build_representation_items(ifc_file, verts, faces)
    if styles:
        ifc_file.create_entity("IfcStyledItem", Item=items[0], Styles=list(styles))
    mapped_representation = ifc_file.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=context,
        RepresentationIdentifier="Body",
        RepresentationType=_representation_type_for_schema(ifc_file.schema),
        Items=items,
    )
    identity = ifc_file.create_entity(
        "IfcAxis2Placement3D",
        Location=ifc_file.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    )
    return ifc_file.create_entity(
        "IfcRepresentationMap",
        MappingOrigin=identity,
        MappedRepresentation=mapped_representation,
    )


def _translation_mapped_item(ifc_file, rep_map, translation_native) -> object:
    point = ifc_file.create_entity(
        "IfcCartesianPoint", Coordinates=tuple(float(c) for c in translation_native)
    )
    operator = ifc_file.create_entity(
        "IfcCartesianTransformationOperator3D",
        Axis1=None,
        Axis2=None,
        Axis3=None,
        LocalOrigin=point,
        Scale=None,
    )
    return ifc_file.create_entity(
        "IfcMappedItem", MappingSource=rep_map, MappingTarget=operator
    )


def consolidate_duplicates(
    ifc_file,
    groups: list[DuplicateGroup],
    model: ModelData,
    min_benefit_ratio: float = 1.5,
) -> ConsolidateReport:
    """groups の各群を、共有 IfcRepresentationMap + IfcMappedItem に置き換える。

    ifc_file を直接変更する(export.py の apply_operations が呼び出す想定で、
    削除・軽量化の後に実行される)。戻り値の ConsolidateReport で適用群数・
    差し替え要素数・警告を報告する。

    min_benefit_ratio: 選別ルールの安全マージン。推定節約バイト数が推定
    オーバーヘッドバイト数の min_benefit_ratio 倍を超えない群は、ファイルサイズが
    純増する見込みとして共有化せずスキップする(小さい群の consolidate が実測で
    サイズ純増を招いたための供監督判断、design.md §5.4追補)。
    """
    report = ConsolidateReport()
    shape_to_gids = _shape_id_to_gids(model)

    for group in groups:
        shape_gid_pairs = [
            (shape_id, gid)
            for shape_id in group.shape_ids
            for gid in shape_to_gids.get(shape_id, [])
        ]
        if len(shape_gid_pairs) < 2:
            continue  # 単独(実質1要素)は対象外

        resolved: list[tuple[str, object, object]] = []
        aborted = False
        for shape_id, gid in shape_gid_pairs:
            try:
                element = ifc_file.by_guid(gid)
            except RuntimeError:
                report.warnings.append(
                    f"群をスキップしました(削除済みメンバーを含む): GlobalId={gid}"
                )
                aborted = True
                break
            body_rep = _find_body_shape_representation(element)
            if body_rep is None:
                report.warnings.append(
                    f"群をスキップしました(Body representationが見つかりません): GlobalId={gid}"
                )
                aborted = True
                break
            resolved.append((shape_id, element, body_rep))
        if aborted:
            continue

        rep_shape_id = group.shape_ids[0]
        rep_entry = next((r for r in resolved if r[0] == rep_shape_id), resolved[0])
        rep_old_items = list(rep_entry[2].Items)
        rep_signature = style_signature(rep_old_items)

        # 色が代表メンバーと異なるメンバーは共有化対象外(幾何dedupで色を誤って
        # 統合しないため)。一致するメンバーだけを consolidate する。
        matched: list[tuple[str, object, object]] = []
        for shape_id, element, body_rep in resolved:
            member_signature = style_signature(list(body_rep.Items))
            if not styles_match(rep_signature, member_signature):
                report.warnings.append(
                    "色が異なるため共有化対象外: "
                    f"GlobalId={getattr(element, 'GlobalId', '?')}"
                )
                continue
            matched.append((shape_id, element, body_rep))

        if len(matched) < 2:
            continue  # 色フィルタ後、共有化できるメンバーが1件以下

        rep_shape = model.shapes[rep_shape_id]
        vertex_count = len(rep_shape.vertices)
        savings, overhead = _estimate_savings_and_overhead(vertex_count, len(matched))
        if savings <= overhead * min_benefit_ratio:
            report.warnings.append(
                "節約見込みなし: "
                f"shape_id={rep_shape_id} members={len(matched)} "
                f"vertex_count={vertex_count} 推定節約={savings}バイト "
                f"推定オーバーヘッド={overhead}バイト "
                f"(閾値={overhead * min_benefit_ratio:.0f}バイト)"
            )
            continue

        rep_centroid = rep_shape.vertices.mean(axis=0)
        source_verts = rep_shape.vertices - rep_centroid

        # 代表メンバーに付いていたIfcStyledItemのStyles(既存のstyleエンティティを
        # 再利用、複製しない)を共有ソースアイテムへ引き継ぐ。
        rep_styled_items = [
            si for item in rep_old_items for si in _styled_items_for_item(item)
        ]
        rep_styles = rep_styled_items[0].Styles if rep_styled_items else None

        context = matched[0][2].ContextOfItems
        rep_map = _create_representation_map(
            ifc_file, source_verts, rep_shape.faces, context, styles=rep_styles
        )

        centroid_cache: dict[str, object] = {}
        for shape_id, element, body_rep in matched:
            if shape_id not in centroid_cache:
                centroid_cache[shape_id] = model.shapes[shape_id].vertices.mean(axis=0)
            centroid = centroid_cache[shape_id]
            translation_native = _verts_to_native_units(
                ifc_file, centroid.reshape(1, 3)
            )[0]
            mapped_item = _translation_mapped_item(ifc_file, rep_map, translation_native)

            old_items = list(body_rep.Items)
            # 個々のメンバーのIfcStyledItemは共有ソース側へ集約済みなので冗長。
            # remove_deep2で削除する(代表メンバーのスタイルは新規StyledItemからも
            # 参照されているため保護され、削除対象外になる)。IfcStyledItemを
            # 削除するとforward参照先の旧形状アイテムも(他から参照されなければ)
            # 連鎖的に削除されるため、スタイルが付いていたアイテムはcleanup対象から
            # 外す(remove_deep2で既に削除済みのエンティティを二重に渡すとクラッシュ
            # するため)。スタイルが付いていないアイテムだけ個別にcleanupする。
            cleanup_targets: list = []
            for item in old_items:
                styled_items = _styled_items_for_item(item)
                if styled_items:
                    cleanup_targets.extend(styled_items)
                else:
                    cleanup_targets.append(item)

            body_rep.Items = [mapped_item]
            body_rep.RepresentationType = "MappedRepresentation"
            report.warnings.extend(_cleanup_items(ifc_file, cleanup_targets))

        report.groups_applied += 1
        report.elements_remapped += len(matched)

    return report
