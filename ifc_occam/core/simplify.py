"""軽量化 (design.md Phase3 Task3)。

純粋計算(bbox/hull/decimate)は numpy in/out で ifcopenshell 非依存。
replace_representation / count_shared_elements は ifcopenshell 依存の書き戻し層。
"""

from __future__ import annotations

import warnings

import numpy as np

import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.unit
from scipy.spatial import ConvexHull

try:
    import fast_simplification
except ImportError as exc:  # pragma: no cover - 依存導入漏れの早期検知用
    raise ImportError(
        "fast-simplification がインストールされていません。"
        "`pip install fast-simplification` を実行してください。"
    ) from exc


# ---------------------------------------------------------------------------
# 純粋関数
# ---------------------------------------------------------------------------

_BBOX_LOCAL_FACES = np.array(
    [
        [0, 2, 1], [0, 3, 2],  # z0 (底面)
        [4, 5, 6], [4, 6, 7],  # z1 (上面)
        [0, 5, 4], [0, 1, 5],  # y0
        [1, 6, 5], [1, 2, 6],  # x1
        [2, 7, 6], [2, 3, 7],  # y1
        [3, 4, 7], [3, 0, 4],  # x0
    ],
    dtype=np.int64,
)


def bbox_mesh(vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """頂点群のローカル座標AABBを表す直方体メッシュ(8頂点12面)を返す。"""
    v = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    mn = v.min(axis=0)
    mx = v.max(axis=0)
    x0, y0, z0 = mn
    x1, y1, z1 = mx

    verts = np.array(
        [
            [x0, y0, z0],
            [x1, y0, z0],
            [x1, y1, z0],
            [x0, y1, z0],
            [x0, y0, z1],
            [x1, y0, z1],
            [x1, y1, z1],
            [x0, y1, z1],
        ],
        dtype=np.float64,
    )
    return verts, _BBOX_LOCAL_FACES.copy()


def convex_hull_mesh(vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """頂点群の凸包メッシュを返す(内部点は消える)。scipy.spatial.ConvexHull を使用。"""
    v = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    hull = ConvexHull(v)

    hull_vertex_indices = hull.vertices  # 凸包上の点の元インデックス(ユニーク)
    old_to_new = {old: new for new, old in enumerate(hull_vertex_indices)}

    out_verts = v[hull_vertex_indices]
    out_faces = np.array(
        [[old_to_new[i] for i in simplex] for simplex in hull.simplices],
        dtype=np.int64,
    )
    return out_verts, out_faces


def decimate_mesh(
    vertices: np.ndarray, faces: np.ndarray, ratio: float
) -> tuple[np.ndarray, np.ndarray]:
    """メッシュを間引く。ratio=残す三角形の割合(0<ratio<1)。

    fast_simplification.simplify の target_reduction は「削除する割合」なので
    target_reduction = 1 - ratio に変換する(README の記載通り。値は経験的に
    ratio=0.1/0.5/0.9 で kept_ratio がほぼ一致することを検証済み)。
    """
    if not (0 < ratio < 1):
        raise ValueError(f"ratio は 0<ratio<1 である必要があります: {ratio!r}")

    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    target_reduction = 1.0 - ratio

    out_verts, out_faces = fast_simplification.simplify(
        v, f, target_reduction=target_reduction
    )
    return np.asarray(out_verts, dtype=np.float64), np.asarray(out_faces, dtype=np.int64)


# ---------------------------------------------------------------------------
# IFC書き戻し
# ---------------------------------------------------------------------------


def _find_body_shape_representation(element):
    rep = getattr(element, "Representation", None)
    if rep is None:
        return None
    for r in rep.Representations or []:
        if r.RepresentationIdentifier == "Body":
            return r
    return None


def _representation_type_for_schema(schema: str) -> str:
    return "Brep" if schema == "IFC2X3" else "Tessellation"


def _verts_to_native_units(ifc_file, verts: np.ndarray) -> np.ndarray:
    """SI/メートル座標(verts)を、ファイルの実寸法単位(ミリメートル等)に変換する。

    ifc_project_length * unit_scale = si_meters (ifcopenshell.util.unit の定義) なので、
    逆変換は verts_native = verts_si / unit_scale。unit_scale が 0/None など不正な場合は
    変換せずに警告する(メートル単位ファイル(unit_scale=1.0)では実質no-op)。
    """
    scale = ifcopenshell.util.unit.calculate_unit_scale(ifc_file)
    if not scale:
        warnings.warn(
            "長さ単位のスケールを取得できませんでした(calculate_unit_scale が "
            f"{scale!r} を返しました)。座標を変換せずに書き込みます。",
            stacklevel=2,
        )
        return verts
    return verts / scale


def _build_triangulated_faceset_items(ifc_file, verts: np.ndarray, faces: np.ndarray):
    coord_list = ifc_file.create_entity(
        "IfcCartesianPointList3D",
        CoordList=[tuple(float(c) for c in row) for row in verts],
    )
    coord_index = [tuple(int(i) + 1 for i in tri) for tri in faces]  # IFCは1-based
    tfs = ifc_file.create_entity(
        "IfcTriangulatedFaceSet",
        Coordinates=coord_list,
        CoordIndex=coord_index,
    )
    return [tfs]


def _build_faceted_brep_items(ifc_file, verts: np.ndarray, faces: np.ndarray):
    points = [
        ifc_file.create_entity("IfcCartesianPoint", Coordinates=tuple(float(c) for c in row))
        for row in verts
    ]
    face_entities = []
    for tri in faces:
        loop = ifc_file.create_entity("IfcPolyLoop", Polygon=[points[i] for i in tri])
        bound = ifc_file.create_entity("IfcFaceOuterBound", Bound=loop, Orientation=True)
        face_entities.append(ifc_file.create_entity("IfcFace", Bounds=[bound]))
    shell = ifc_file.create_entity("IfcClosedShell", CfsFaces=face_entities)
    brep = ifc_file.create_entity("IfcFacetedBrep", Outer=shell)
    return [brep]


def _build_representation_items(ifc_file, verts: np.ndarray, faces: np.ndarray):
    verts_native = _verts_to_native_units(ifc_file, verts)
    if ifc_file.schema == "IFC2X3":
        return _build_faceted_brep_items(ifc_file, verts_native, faces)
    return _build_triangulated_faceset_items(ifc_file, verts_native, faces)


def _styled_items_for_item(item) -> list:
    """item(IfcRepresentationItem)を指す IfcStyledItem のリストを返す

    (inverse属性 StyledByItem。付いていなければ空リスト)。"""
    return list(getattr(item, "StyledByItem", []) or [])


# 走査を打ち切る型。スタイルが付くことは実質なく、数だけが爆発する
# (small.ifc の IfcCartesianPoint は61,716件)。ここで刈らないと1要素あたり
# 数千ノードを辿ることになる。
_STYLE_SEARCH_LEAF_TYPES = ("IfcCartesianPoint", "IfcCartesianPointList3D", "IfcDirection")


def _iter_child_entities(value):
    """属性値からエンティティだけを取り出す(入れ子のタプル/リストを平坦化する)。"""
    if isinstance(value, ifcopenshell.entity_instance):
        yield value
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from _iter_child_entities(child)


def _map_is_exclusive(mapped_item) -> bool:
    """この IfcMappedItem が参照する IfcRepresentationMap を、他の誰も使っていないか。

    IfcRepresentationMap の inverse 属性 MapUsage が、そのマップを参照している
    IfcMappedItem の集合。自分1件だけなら「専有している」とみなす。
    """
    source = getattr(mapped_item, "MappingSource", None)
    if source is None:
        return False
    return len(getattr(source, "MapUsage", []) or []) <= 1


def _layer_assignments_for_node(node) -> list:
    """node(IfcRepresentation または IfcRepresentationItem)を AssignedItems に
    持つ IfcPresentationLayerAssignment のリストを返す(無ければ空リスト)。

    inverse 属性名はスキーマと対象で異なる: rep 側は IFC4/IFC2X3 とも
    LayerAssignments(複数形)、item 側は IFC4 が LayerAssignment(単数形)、
    IFC2X3 が LayerAssignments。存在しない属性は AttributeError になるため
    getattr の既定値で吸収し、両方の名前を試して重複なく集める。
    """
    found: list = []
    seen: set[int] = set()
    for attr in ("LayerAssignments", "LayerAssignment"):
        for assignment in getattr(node, attr, None) or []:
            if assignment.id() not in seen:
                seen.add(assignment.id())
                found.append(assignment)
    return found


def _layer_assignments_in_subtree(item) -> list[tuple]:
    """item(IfcRepresentationItem)または rep(IfcRepresentation)を根に取り、
    その子孫のうちレイヤー割当が付いたノードを (assignment, node) のペアで
    返す。引数名は item だが、_unshare_and_replace からは rep(body_rep)が
    渡される(IfcLayeredItem は rep|item の SELECT で、どちらを根にしても
    走査規則は同じため)。

    走査のリーフ刈り・共有マップガード(他の要素も使っている共有
    IfcRepresentationMap の内部には入らない)は _styled_items_in_subtree と
    同じ。理由も同じ:
    (1) レイヤー割当は実データでは要素側 IfcShapeRepresentation に付くが、
        スキーマ上は任意の IfcRepresentationItem にも付き得る(IfcLayeredItem)。
        深さで打ち切ると黙って取りこぼし、旧形状が inverse に掴まれたまま
        remove_deep2 が無言で退く(この修正の発端のバグと同じ形)。
    (2) 共有マップ内部のノードは差し替え後も生き残るため、そのレイヤー所属を
        奪ってはならない。

    _styled_items_in_subtree と厳密には異なる点が1つ: rep を根に取った場合、
    ContextOfItems 経由で表現コンテキストの部分木まで無害に歩く(IfcLayeredItem
    は rep|item の SELECT のため、コンテキスト側にレイヤー割当が付くことは
    スキーマ上ない。害はないが、styled側(rep を根にしない)とは走査対象が
    完全一致するわけではないことに注意)。
    """
    found: list[tuple] = []
    seen_nodes: set[int] = set()

    def visit(node) -> None:
        if not isinstance(node, ifcopenshell.entity_instance):
            return
        node_id = node.id()
        if node_id in seen_nodes:
            return
        seen_nodes.add(node_id)
        for assignment in _layer_assignments_for_node(node):
            found.append((assignment, node))
        if node.is_a() in _STYLE_SEARCH_LEAF_TYPES:
            return
        if node.is_a("IfcMappedItem"):
            if _map_is_exclusive(node):
                visit(node.MappingSource)
            visit(node.MappingTarget)
            return
        for value in node:
            for child in _iter_child_entities(value):
                visit(child)

    visit(item)
    return found


def _transfer_layer_assignments(ifc_file, doomed_roots, rep_target, item_target) -> None:
    """doomed_roots(捨てる予定の rep / representation item の部分木)に付いた
    レイヤー所属を新しい形状へ引き継ぐ。

    AssignedItems から部分木内のノードを外し、代わりに同じ層の新ノードを
    加える: IfcRepresentation に付いていた所属には rep_target、
    IfcRepresentationItem に付いていた所属には item_target(どちらか一方が
    None ならもう一方で代用、両方 None なら外すだけ)。extract.py の
    _analyze_representation は rep 側の LayerAssignments しか読まないため、
    rep の所属を rep へ返すことが GUI のレイヤー表示を保つ条件になる。

    外した結果 AssignedItems が空になった割当は削除する(IFC4 の
    AssignedItems は SET[1:?] で空はスキーマ違反。ifcopenshell は空代入を
    検証しない(実測)ため、放置すると壊れた割当が出力に残る。空の
    IfcStyledRepresentation を _remove_styled_item が消すのと同じ理屈)。
    削除する割当が IfcPresentationLayerWithStyle の場合、LayerStyles
    (IfcSurfaceStyle等)も割当と一緒に孤児化し得るため、割当を消した後で
    各styleに ifcopenshell.util.element.remove_deep2 を掛ける(_remove_styled_item
    の Styles 処理と同じ理屈: 他の割当からまだ参照されているstyleは
    remove_deep2 が自動的に保護し、本当に孤立したものだけが消える)。

    これをせずに旧ノードを _cleanup_items に渡すと、レイヤー割当という
    inverse が1本残るだけで remove_deep2 が無言の no-op になり、旧形状が
    丸ごと出力に残った上にレイヤー情報は死んだ旧ノードに付いたままになる
    (test-donuts_mini.ifc の decimate で実測した二重欠陥、2026-07-29)。

    既知の限界(スタイル側 _transfer_styled_items と同じ挙動に揃えた):
    doomed_roots のトップアイテムが共有マップを介さず別の rep からも直接
    共有されている場合(実データでは未観測)、生き残るノードから所属を
    外してしまう。その場合も _cleanup_items の残置警告で可視化される。
    """
    if not ifc_file.by_type("IfcPresentationLayerAssignment"):
        return  # レイヤー割当が無いファイルでは部分木走査ごと省く

    doomed_by_assignment: dict[int, tuple] = {}
    for root in doomed_roots:
        for assignment, node in _layer_assignments_in_subtree(root):
            entry = doomed_by_assignment.setdefault(assignment.id(), (assignment, []))
            entry[1].append(node)

    for assignment, doomed_nodes in doomed_by_assignment.values():
        doomed_ids = {n.id() for n in doomed_nodes}
        kept = [x for x in (assignment.AssignedItems or []) if x.id() not in doomed_ids]
        kept_ids = {x.id() for x in kept}
        needs_rep = any(n.is_a("IfcRepresentation") for n in doomed_nodes)
        needs_item = any(not n.is_a("IfcRepresentation") for n in doomed_nodes)
        for needed, primary, fallback in (
            (needs_rep, rep_target, item_target),
            (needs_item, item_target, rep_target),
        ):
            target = primary if primary is not None else fallback
            if needed and target is not None and target.id() not in kept_ids:
                kept.append(target)
                kept_ids.add(target.id())
        if kept:
            assignment.AssignedItems = kept
        else:
            layer_styles = (
                list(assignment.LayerStyles or [])
                if assignment.is_a("IfcPresentationLayerWithStyle")
                else []
            )
            ifc_file.remove(assignment)
            for style in layer_styles:
                ifcopenshell.util.element.remove_deep2(ifc_file, style)


def _styled_items_in_subtree(item) -> list:
    """item とその子孫に付いた IfcStyledItem を重複なく返す(先行順)。

    IfcStyledItem.Item は必ずしもトップレベルの representation item ではない。
    Rebro出力では IfcFacetedBrep ではなく内側の IfcClosedShell に付く。
    トップレベルしか見ないとスタイルの付け替えが空振りし、旧形状が IfcStyledItem に
    参照されたまま残って remove_deep2 で消えなくなる(出力が入力より太る原因)。

    走査の規則:
      - _STYLE_SEARCH_LEAF_TYPES では止まる(点・方向にスタイルは付かない)。
      - IfcMappedItem からは、共有 IfcRepresentationMap を他の要素も使っている場合、
        その内部へは入らない。入って付け替えると他の要素の色を奪うため。
        自分だけが使っているマップ(= この差し替えで丸ごとゴミになるマップ)には入る。
      - それ以外は深さ制限なしで辿る。深さで打ち切ると、深い位置に付いたスタイルを
        黙って取りこぼす(= 旧形状が黙って残る)。

    戻り値の先頭は、トップレベルにスタイルが付いていれば必ずそれになる
    (「自分 -> 子」の先行順。呼び出し側が先頭を代表として使う)。
    """
    found: list = []
    seen_styled: set[int] = set()
    seen_nodes: set[int] = set()

    def visit(node) -> None:
        if not isinstance(node, ifcopenshell.entity_instance):
            return
        node_id = node.id()
        if node_id in seen_nodes:
            return
        seen_nodes.add(node_id)
        for styled_item in _styled_items_for_item(node):
            if styled_item.id() not in seen_styled:
                seen_styled.add(styled_item.id())
                found.append(styled_item)
        if node.is_a() in _STYLE_SEARCH_LEAF_TYPES:
            return
        if node.is_a("IfcMappedItem"):
            # 共有マップを他の要素も使っている場合、その内部(MappingSource)には
            # 入らない。入って付け替え/削除すると他の要素の色を奪ってしまうため。
            # MappingTarget(変換行列側)にスタイルが付くことは通常ないが、
            # 塞ぐ理由も無いので通常どおり辿る。
            if _map_is_exclusive(node):
                visit(node.MappingSource)
            visit(node.MappingTarget)
            return
        for value in node:
            for child in _iter_child_entities(value):
                visit(child)

    visit(item)
    return found


def _resolve_surface_rgb(style) -> tuple[float, float, float] | None:
    """IfcSurfaceStyle から IfcSurfaceStyleShading(のサブタイプ Rendering含む)の
    SurfaceColour(RGB)を取り出す。最初に見つかったものを返す。取れなければNone。

    IfcStyledItem.Styles の要素は IfcSurfaceStyle が直接入っている場合(合成フィクスチャは
    この形)と、IFC2X3由来のdeprecatedなラッパー IfcPresentationStyleAssignment を
    経由する場合がある。実測: small.ifc(Rebro2026出力)の IfcStyledItem 4,053件は
    全てこのラッパー経由だった(展開しないと一度もRGBが取れず、style_signatureの
    「同一RGB」経由の一致判定が実データで機能していなかった不具合)。ラッパーは
    はがしてから同じ探索を行う。

    ラッパーは入れ子にも循環にもなり得る(スキーマ上妥当な多段連鎖も、壊れた
    ファイルの相互参照も、ifcopenshell は生成時に止めない)。再帰で書くと
    2000段程度の連鎖で RecursionError になり、呼び出し元のファイル読込ごと
    失敗させてしまうため、明示スタックで反復する。深さで打ち切ると深い位置の
    色を黙って取りこぼすので、上限は設けず訪問済みidだけで循環を止める。
    探索順は再帰版と同じ先行順(スタックには逆順に積む)。
    """
    seen: set[int] = set()
    stack = [style]
    while stack:
        current = stack.pop()
        if current is None or current.id() in seen:
            continue
        seen.add(current.id())
        if current.is_a("IfcPresentationStyleAssignment"):
            stack.extend(reversed(list(getattr(current, "Styles", []) or [])))
            continue
        for sub_style in getattr(current, "Styles", []) or []:
            if sub_style.is_a("IfcSurfaceStyleShading"):
                colour = sub_style.SurfaceColour
                if colour is not None:
                    return (float(colour.Red), float(colour.Green), float(colour.Blue))
    return None


def style_signature(items) -> frozenset | None:
    """items(representation itemのリスト)とその部分木に付いた全IfcStyledItemから
    比較用シグネチャを作る((styleのentity id, RGB or None)のペア集合)。
    付いたスタイルが1つもなければNone。

    部分木まで見るのは、Rebro出力ではスタイルが IfcClosedShell 等の子アイテムに
    付くため。トップレベルしか見ないと色付き要素同士が「スタイル無し同士」と
    誤判定され、色の違う形状が consolidate で1つに統合されてしまう。
    """
    sig: set[tuple[int, tuple[float, float, float] | None]] = set()
    for item in items:
        for styled_item in _styled_items_in_subtree(item):
            for style in styled_item.Styles or []:
                sig.add((style.id(), _resolve_surface_rgb(style)))
    return frozenset(sig) if sig else None


def styles_match(sig_a, sig_b) -> bool:
    """同一スタイルentity id、または同一RGBのいずれかが一致すれば「同じ色」とみなす
    (design.md契約: 「同一style entity id OR 同一RGB」の保守的な比較)。両方Noneなら
    (スタイル無し同士)一致とみなす。"""
    if sig_a is None and sig_b is None:
        return True
    if sig_a is None or sig_b is None:
        return False
    ids_a = {entity_id for entity_id, _ in sig_a}
    ids_b = {entity_id for entity_id, _ in sig_b}
    if ids_a & ids_b:
        return True
    rgb_a = {rgb for _, rgb in sig_a if rgb is not None}
    rgb_b = {rgb for _, rgb in sig_b if rgb is not None}
    return bool(rgb_a & rgb_b)


def _remove_styled_item(ifc_file, styled_item) -> None:
    """IfcStyledItem を削除する。参照元の IfcStyledRepresentation が空になったら
    そちらも削除し、孤立した Styles(IfcSurfaceStyle等)も併せて削除する。

    IFC4 の IfcRepresentation.Items は SET[1:?] であり、空集合はスキーマ違反。
    ifcopenshell の remove() は inverse 参照を自動でパッチするため dangling は
    生まれないが、空の IfcStyledRepresentation が黙って残ってしまう。

    owner側の削除は ifcopenshell.util.element.remove_deep2 ではなく素の
    ifc_file.remove() を使う(ブリーフのコードから変更、詳細は fix-report参照)。
    remove_deep2 は「開始要素が他から参照されていないこと」が前提
    (ソースコメント: "The start element must have no inverses.")で、他に
    inverse参照が1つでもあり also_consider で説明できなければ即座に何もせず
    returnする。owner(IfcStyledRepresentation)は通常
    IfcProductDefinitionShape.Representationsから参照されているため、この
    前提を常に破りremove_deep2が無言のno-opになる(実測で確認済み: レビュアの
    攻撃フィクスチャで再現)。owner.Itemsは既に空(cascadeで消すべき子が無い)
    ので、素のremove()で十分かつ正しい(inverse参照側は自動でパッチされる)。

    Styles側はこれとは逆にremove_deep2を使う(ブリーフのコードには無かった処理。
    詳細はfix-report参照)。styled_item.Item(旧形状)は呼び出し元が別途
    cleanup_targetsで掃除する前提なので、ここではItemには触れずStylesだけを
    見る。他のIfcStyledItemがまだ参照している(=共有元)styleはremove_deep2が
    「他から参照されている」として自動的に保護しno-opになるため、本当に孤立した
    ものだけが削除される。consolidateで代表メンバーの色を共有ソース側の新規
    IfcStyledItemへ引き継いだ後、代表メンバー自身の旧IfcStyledItemを破棄する
    ケースでは、styleは新規IfcStyledItemからまだ参照されているため保護される。
    一方、色は同じだがentityが別(RGB一致のみ)の非代表メンバーのstyleは、
    削除後に他から参照されなくなるため、ここで正しく掃除される
    (掃除しないとIfcSurfaceStyle/IfcSurfaceStyleRendering/IfcColourRgbが孤立して
    残り、consolidateしたのにファイルサイズが純増する)。
    """
    owners = [
        inverse
        for inverse in ifc_file.get_inverse(styled_item)
        if inverse.is_a("IfcStyledRepresentation")
    ]
    styles = list(getattr(styled_item, "Styles", []) or [])
    ifc_file.remove(styled_item)
    for owner in owners:
        if not owner.Items:
            ifc_file.remove(owner)
    for style in styles:
        ifcopenshell.util.element.remove_deep2(ifc_file, style)


def _transfer_styled_items(ifc_file, old_items, new_items) -> int:
    """old_items(とその部分木)上の IfcStyledItem を new_items へ付け替える。

    新アイテムは1つ(_build_representation_items は常に単一アイテムを返す)なので、
    1アイテムにつき引き継げるスタイルも1つ。部分木で複数見つかった場合は先頭
    (トップレベルに付いていたものがあればそれ)を付け替え、残りは削除する。
    残したまま放置すると旧形状を参照し続け、remove_deep2 が旧形状を消せなくなる。
    IfcStyledItem を参照する記録は存在しない(small.ifc で実測0件)ため削除は安全。

    new_items が空(呼び出し元の異常系)の場合は付け替え先が無いため全て削除する。

    戻り値: 引き継げずに破棄した IfcStyledItem の数。
    """
    discarded = 0
    for i, old_item in enumerate(old_items):
        styled_items = _styled_items_in_subtree(old_item)
        if not styled_items:
            continue
        if not new_items:
            for styled_item in styled_items:
                _remove_styled_item(ifc_file, styled_item)
                discarded += 1
            continue
        target = new_items[0] if len(new_items) == 1 else new_items[min(i, len(new_items) - 1)]
        styled_items[0].Item = target
        for extra in styled_items[1:]:
            _remove_styled_item(ifc_file, extra)
            discarded += 1
    return discarded


def _cleanup_items(ifc_file, old_items) -> list[str]:
    """旧 representation アイテムを、他から参照されない場合のみ再帰的に削除する。

    掃除の失敗は書き戻し自体を失敗させず、警告文字列として呼び出し元に返す。

    remove_deep2 は開始要素に inverse が残っていると、例外を出さずに何も
    しない(契約: "The start element must have no inverses.")。レイヤー割当
    バグはこの無言の退出のせいで警告ゼロのまま2日間気付かれなかったため、
    削除後に開始要素の生死を確認し、生き残っていたら参照元の型を添えて警告
    する。この警告は正常系では出ない(出たら「何かがまだ旧形状を掴んでいる」
    の合図であり、新たな同類バグの検出線になる)。
    """
    warnings: list[str] = []
    for item in old_items:
        item_id = item.id()
        item_type = item.is_a()
        try:
            ifcopenshell.util.element.remove_deep2(ifc_file, item)
        except Exception as exc:  # noqa: BLE001 - 掃除の失敗は警告に留め、書き戻し自体は成功させる
            warnings.append(f"旧形状アイテムの掃除に失敗: {exc}")
            continue
        if getattr(ifc_file, "to_delete", None) is not None:
            # batch_remove_deep2 中は削除が遅延されるため、この時点で生きて
            # いるのは正常(unbatch でまとめて消える)。残置チェックは誤検知
            # になるのでスキップする。
            continue
        try:
            survivor = ifc_file.by_id(item_id)
        except RuntimeError:
            continue  # 削除成功
        referrer_types = sorted({inv.is_a() for inv in ifc_file.get_inverse(survivor)})
        warnings.append(
            f"旧形状 {item_type} #{item_id} が他から参照されているため"
            f"削除できませんでした(参照元: {', '.join(referrer_types) or '不明'})"
        )
    return warnings


def _discard_or_collect(ifc_file, old_items, doomed_sink) -> list[str]:
    """旧アイテムを掃除する(doomed_sink=None、従来どおり remove_deep2)か、
    捨てたルートidを記録して後段の書き出し時GC(textops/gc.py)に委ねる
    (doomed_sink=list)。

    GC経路では要素ごとの remove_deep2 を呼ばない。donuts族データでは
    remove_deep2 が47秒/要素かかり(2026-07-30実測、456要素で約6時間)、
    記録+書き出し時GC(約80秒の固定費)の方が桁で速い。記録だけなら失敗も
    残置も起きないので警告は常に空(残置の検出はGC側の doomed_survivors が
    同じ検出線を張る)。
    """
    if doomed_sink is None:
        return _cleanup_items(ifc_file, old_items)
    doomed_sink.extend(item.id() for item in old_items)
    return []


def _replace_items_in_place(
    ifc_file, shape_representation, new_items, doomed_sink=None
) -> list[str]:
    old_items = list(shape_representation.Items)
    discarded = _transfer_styled_items(ifc_file, old_items, new_items)
    # rep_target には生き残る shape_representation 自身を渡す(fallbackで
    # item_target に降格させない。フェーズ最終レビュー M-1)。この経路では
    # rep は差し替えられずItemsだけが入れ替わるため、rep直付けの所属は
    # そのままこのrepへ返すのが正しい(_analyze_representationはrep側の
    # LayerAssignmentsしか読まないため、item側へ降格するとGUIのレイヤー
    # 表示が壊れる)。
    _transfer_layer_assignments(
        ifc_file, old_items, shape_representation, new_items[0] if new_items else None
    )
    shape_representation.Items = new_items
    shape_representation.RepresentationType = _representation_type_for_schema(ifc_file.schema)
    warnings = _discard_or_collect(ifc_file, old_items, doomed_sink)
    if discarded:
        warnings.append(
            f"新形状は単一アイテムのため、複数あったスタイルのうち1件だけを引き継ぎました"
            f"(破棄 {discarded}件)"
        )
    return warnings


def _transform_operator_matrix(op) -> np.ndarray | None:
    """IfcCartesianTransformationOperator3D を 4x4 アフィン行列にする。

    Axis1/Axis2/Axis3(IfcDirection, optional)・LocalOrigin(IfcCartesianPoint,
    optional)・Scale(float, optional, 既定1.0)を扱う。未指定の軸は既定
    (1,0,0)/(0,1,0)/(0,0,1)。指定された軸はGram-Schmidtで正規直交化するため、
    回転部分は常に直交行列(一様スケールSCALEを乗じたもの)になる。

    op が None(MappingTargetなし)なら恒等行列を返す。
    IfcCartesianTransformationOperator3DnonUniform(非一様スケール、Scale2/Scale3
    を持つ)は Scale のみでは行列を再現できないため None を返す(=安全に逆変換
    できないことを示すシグナル。呼び出し側はscope="element"へフォールバックする)。
    """
    if op is None:
        return np.eye(4)
    if op.is_a("IfcCartesianTransformationOperator3DnonUniform"):
        return None

    axis1 = np.array(op.Axis1.DirectionRatios, dtype=np.float64) if op.Axis1 else np.array([1.0, 0.0, 0.0])
    axis2 = np.array(op.Axis2.DirectionRatios, dtype=np.float64) if op.Axis2 else np.array([0.0, 1.0, 0.0])
    axis3 = np.array(op.Axis3.DirectionRatios, dtype=np.float64) if op.Axis3 else np.array([0.0, 0.0, 1.0])
    origin = np.array(op.LocalOrigin.Coordinates, dtype=np.float64) if op.LocalOrigin else np.zeros(3)
    scale = op.Scale if op.Scale is not None else 1.0

    d1 = axis1 / np.linalg.norm(axis1)
    d2 = axis2 - np.dot(axis2, d1) * d1
    n2 = np.linalg.norm(d2)
    d2 = d2 / n2 if n2 > 1e-12 else np.array([0.0, 1.0, 0.0])
    d3 = axis3 - np.dot(axis3, d1) * d1 - np.dot(axis3, d2) * d2
    n3 = np.linalg.norm(d3)
    d3 = d3 / n3 if n3 > 1e-12 else np.cross(d1, d2)

    rotation = np.column_stack([d1, d2, d3]) * scale
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = origin
    return matrix


def _is_identity_matrix(matrix: np.ndarray) -> bool:
    return np.allclose(matrix, np.eye(4), atol=1e-9)


def _is_safe_similarity_matrix(matrix: np.ndarray | None) -> bool:
    """一様スケール・回転(鏡映なし、行列式>0)であり、安全に逆変換できるか。

    _transform_operator_matrix の構築上、回転部分は常に直交行列(スケール倍)に
    なるため、ここでは非退化(スケール!=0)と鏡映なし(行列式>0、右手系維持)のみ
    確認すれば十分。"""
    if matrix is None:
        return False
    rotation = matrix[:3, :3]
    scale_estimate = np.linalg.norm(rotation[:, 0])
    if scale_estimate < 1e-9:
        return False
    return np.linalg.det(rotation) > 1e-9


def _invert_similarity_matrix(matrix: np.ndarray) -> np.ndarray:
    """_is_safe_similarity_matrix が True を返す行列の逆行列を返す。"""
    rotation = matrix[:3, :3]
    origin = matrix[:3, 3]
    scale = np.linalg.norm(rotation[:, 0])
    rotation_unit = rotation / scale
    inv_rotation = rotation_unit.T / scale
    inv = np.eye(4)
    inv[:3, :3] = inv_rotation
    inv[:3, 3] = -inv_rotation @ origin
    return inv


def _apply_matrix_to_verts(matrix: np.ndarray, verts: np.ndarray) -> np.ndarray:
    v = np.asarray(verts, dtype=np.float64).reshape(-1, 3)
    homogeneous = np.hstack([v, np.ones((v.shape[0], 1))])
    transformed = (matrix @ homogeneous.T).T
    return transformed[:, :3]


def _unshare_and_replace(
    ifc_file, element, body_rep, verts: np.ndarray, faces: np.ndarray, doomed_sink=None
) -> list[str]:
    """共有 IfcMappedItem を解いて、この要素専用の新規 IfcShapeRepresentation に
    差し替える(scope="element" 本体、および scope="shared" の逆変換フォールバック
    先としても再利用する)。"""
    new_items = _build_representation_items(ifc_file, verts, faces)
    # 破棄件数は scope="shared" 側(_replace_items_in_place)と同じく警告に出す。
    # ここだけ黙って捨てていると、「深さで打ち切ると黙って取りこぼす」を理由に
    # 探索の深さ上限を撤廃したこのフェーズの方針と矛盾する(フェーズ最終レビュー M-1)。
    discarded = _transfer_styled_items(ifc_file, list(body_rep.Items), new_items)
    new_body_rep = ifc_file.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_rep.ContextOfItems,
        RepresentationIdentifier="Body",
        RepresentationType=_representation_type_for_schema(ifc_file.schema),
        Items=new_items,
    )
    product_shape = element.Representation
    reps = list(product_shape.Representations)
    idx = reps.index(body_rep)
    reps[idx] = new_body_rep
    product_shape.Representations = reps

    # 旧ラッパー(body_rep)とその配下(専有マップ含む)のレイヤー所属を
    # 新しい rep / item へ引き継いでから掃除する。
    _transfer_layer_assignments(ifc_file, [body_rep], new_body_rep, new_items[0])

    # 古い個別ラッパー(body_rep)はこの要素からしか参照されていないはずなので、
    # まずそれを起点に掃除する。共有マップ/ジオメトリが他から参照されていれば
    # remove_deep2 が自動的に保護する。
    warnings = _discard_or_collect(ifc_file, [body_rep], doomed_sink)
    if discarded:
        warnings.append(
            f"新形状は単一アイテムのため、複数あったスタイルのうち1件だけを引き継ぎました"
            f"(破棄 {discarded}件)"
        )
    return warnings


def replace_representation(
    ifc_file, element, verts: np.ndarray, faces: np.ndarray, scope: str = "element",
    doomed_sink=None,
) -> list[str]:
    """要素の Body 表現を verts/faces の三角形メッシュに差し替える。

    verts は SI単位(メートル)であることを前提とする(ifcopenshell.geom / core/extract.py
    が返す座標系と同じ)。ファイルの実寸法単位がメートル以外(ミリメートル等)の場合、
    書き込み前に ifcopenshell.util.unit.calculate_unit_scale(ifc_file) を用いて
    ファイル側の単位に変換してから IfcCartesianPointList3D / IfcCartesianPoint に
    格納する(単位不整合で形状が縮小/拡大するバグの回避)。

    scope="element": 対象要素だけを差し替える。共有 IfcRepresentationMap を
        参照している場合は、その要素専用の新規 IfcShapeRepresentation を割り当てて
        個別化する(共有マップ自体は変更しない)。
    scope="shared": 参照している IfcRepresentationMap の MappedRepresentation を
        直接差し替える。同じマップを参照する全要素に波及する。

        verts は呼び出し元(export.py の _current_mesh)が ifcopenshell.geom で
        抽出したこの要素のローカル座標であり、この要素の IfcMappedItem.MappingTarget
        (共有マップ座標系→この要素のローカル座標系への変換)が既に適用済みである
        (Final Review Fix3で判明した前提)。MappedRepresentation(共有マップの
        座標系)へそのまま書き込むと、次にこの要素(または他の共有要素)を再抽出した
        際に MappingTarget がもう一度適用され、2重変換になってしまう。
        そのためMappingTargetが恒等でない場合は、書き込み前にその逆行列を verts に
        適用して共有マップの座標系に戻す。逆行列が安全に求まらない場合(非一様スケール
        等)は、この要素だけ scope="element" にフォールバックし、警告を返す
        (共有マップ自体は変更されない)。

    戻り値: 旧アイテムの掃除中に発生した警告文字列のリスト(全成功時は空リスト)。
    書き戻し自体は掃除の失敗に関わらず成功する。

    doomed_sink に list を渡すと、旧形状の掃除を行わず捨てたルートの
    record id を記録する(呼び出し側が書き出し後に textops.gc.gc_rewrite で
    一括回収する前提)。None(既定)は従来どおりその場で掃除する。
    """
    if scope not in ("element", "shared"):
        raise ValueError(f"不正な scope です: {scope!r}")

    body_rep = _find_body_shape_representation(element)
    if body_rep is None:
        raise ValueError(f"要素に Body representation がありません: {element}")

    old_items = list(body_rep.Items)
    is_mapped = len(old_items) == 1 and old_items[0].is_a("IfcMappedItem")

    if not is_mapped:
        # 個別所有の representation。scope に関わらずその場で差し替える。
        new_items = _build_representation_items(ifc_file, verts, faces)
        return _replace_items_in_place(ifc_file, body_rep, new_items, doomed_sink)

    mapped_item = old_items[0]
    mapping_source = mapped_item.MappingSource
    mapped_representation = mapping_source.MappedRepresentation

    if scope == "shared":
        matrix = _transform_operator_matrix(mapped_item.MappingTarget)

        if matrix is not None and _is_identity_matrix(matrix):
            new_items = _build_representation_items(ifc_file, verts, faces)
            return _replace_items_in_place(ifc_file, mapped_representation, new_items, doomed_sink)

        if matrix is not None and _is_safe_similarity_matrix(matrix):
            verts_in_shared_frame = _apply_matrix_to_verts(_invert_similarity_matrix(matrix), verts)
            new_items = _build_representation_items(ifc_file, verts_in_shared_frame, faces)
            return _replace_items_in_place(ifc_file, mapped_representation, new_items, doomed_sink)

        # MappingTargetを安全に逆変換できない(非一様スケール等)。共有マップは
        # 変更せず、この要素だけscope="element"にフォールバックする。
        result = _unshare_and_replace(ifc_file, element, body_rep, verts, faces, doomed_sink)
        result.append(
            "scope=\"shared\"の書き戻しでMappingTargetを安全に逆変換できないため、"
            f"この要素(GlobalId={getattr(element, 'GlobalId', '?')})はscope=\"element\""
            "にフォールバックしました(共有マップは変更されていません)。"
        )
        return result

    # scope == "element": 共有を解いて個別化する。
    return _unshare_and_replace(ifc_file, element, body_rep, verts, faces, doomed_sink)


def _shared_element_group(ifc_file, gid: str) -> list:
    """gid の要素が参照する形状(共有 RepresentationMap 含む)を使う要素の
    entity リストを返す(gid自身を含む)。Body representationが無ければ空リスト。
    count_shared_elements/get_shared_element_gids の共通の内部実装。"""
    element = ifc_file.by_guid(gid)
    body_rep = _find_body_shape_representation(element)
    if body_rep is None:
        return []

    items = list(body_rep.Items)
    if len(items) == 1 and items[0].is_a("IfcMappedItem"):
        mapped_representation = items[0].MappingSource.MappedRepresentation
        return list(
            ifcopenshell.util.element.get_elements_by_representation(
                ifc_file, mapped_representation
            )
        )

    return [element]


def count_shared_elements(ifc_file, gid: str) -> int:
    """gid の要素が参照する形状(共有 RepresentationMap 含む)を使う要素数を返す。"""
    return len(_shared_element_group(ifc_file, gid))


def get_shared_element_gids(ifc_file, gid: str) -> list[str]:
    """gid の要素と同一形状(共有 RepresentationMap)を参照する、gid自身を除いた
    兄弟要素のGlobalIdリストを返す(共有波及の着色 §Phase4 Task4 で使用)。
    共有していない、またはBody representationが無い要素は空リストを返す。"""
    return [e.GlobalId for e in _shared_element_group(ifc_file, gid) if e.GlobalId != gid]
