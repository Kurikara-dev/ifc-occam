"""重複検出: 量子化ハッシュ + 幾何比較 (design.md §4.2)。純粋関数、ifcopenshell 非依存。

アルゴリズム (design.md §4.2 / 要件 §5.1):
1. 正規化: 各形状の頂点から重心を引く (平行移動不変)。回転・鏡像はスコープ外。
2. 量子化ハッシュ: round(v / tol) で格子に量子化し、頂点列を辞書順ソートした上で
   (量子化頂点bytes, 頂点数, 三角形数) をキーにバケツ分け。
3. 幾何比較で確定: 同一バケツ内で、バケツの先頭を代表としてソート済み実座標の
   最大距離 < tol であり、かつ面接続が一致する(正準ソートの置換で面インデックス
   を再マップし、三角形内インデックスをソート・三角形列を辞書順ソートして比較。
   巻き方向の差は同一視)場合にのみ同一群とみなす。

このモジュールが保証するのは「面接続まで含めて一致する形状のみを同一群とする
(誤結合はしない)」ことであり、次の2ケースでは逆方向(本来同一だが検出されない
=見逃し)の誤差が出ることがある。誤って無関係な形状を同一groupにまとめることは
ない:
- 量子化格子の境界ちょうどに乗る座標は round() の丸め方向次第で別バケツに分かれる
  場合がある。
- 同一位置に重複頂点がある形状は、lexsort による正準順序(どちらの重複頂点を
  先にするか)が一意に定まらず、面の再マップ結果が頂点の並び方に依存してしまう
  場合がある。
実データでの tol 調整は §9 参照。
"""

from dataclasses import dataclass

import numpy as np

from ifc_occam.core.types import ShapeInfo


@dataclass
class DuplicateGroup:
    """重複と判定された形状の集まり。"""

    shape_ids: list[str]  # 2件以上
    triangle_count: int  # 1形状分の三角形数
    savable_triangles: int  # = triangle_count × (len(shape_ids) - 1)


def _canonical(shape: ShapeInfo, tol: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """重心差引 → 量子化 → 頂点を辞書順ソート。(量子化頂点, 実座標, 置換順) を返す。"""
    v = shape.vertices - shape.vertices.mean(axis=0)
    q = np.round(v / tol).astype(np.int64)
    order = np.lexsort((q[:, 2], q[:, 1], q[:, 0]))
    return q[order], v[order], order


def _canonical_faces(faces: np.ndarray, order: np.ndarray) -> np.ndarray:
    """旧→新の頂点インデックス置換で面を再マップし、正準形に揃える。

    - order[new_idx] = old_idx (lexsort の戻り値) なので、旧インデックスから
      新インデックスへの逆写像 inv を作って面を再マップする。
    - 三角形内のインデックスをソート(巻き方向の差を同一視)。
    - 三角形の行を辞書順ソート(面の列挙順の差を同一視)。

    既知の限界: 同一位置に重複頂点がある形状は order が一意に定まらないため、
    ここでの再マップ結果も曖昧になりうる(誤結合はしないが見逃す方向)。
    """
    inv = np.argsort(order)
    remapped = inv[faces]
    remapped = np.sort(remapped, axis=1)
    row_order = np.lexsort(tuple(remapped[:, i] for i in range(remapped.shape[1] - 1, -1, -1)))
    return remapped[row_order]


def find_duplicates(
    shapes: dict[str, ShapeInfo], tol: float = 1e-6
) -> list[DuplicateGroup]:
    """平行移動不変な重複形状検出。単独形状(バケツ内1件)は結果に含めない。

    carry-forward Phase L: shapes.items() をそのまま辞書順(=到着順)で走査すると、
    extract_model のマルチスレッドgeometry iteratorがshapes dictへ書き込む順序が
    実行ごとに変わるため、バケツ内メンバー順・group.shape_ids・代表(先頭)選択が
    実行ごとに揺れ、consolidate側の共有先選択が非決定になる
    (.superpowers/sdd/cfi-phase-final-review.md I-1で実測)。shape_id自体は
    geometry.idに基づき実行間で安定しているため、sorted()でキーの昇順に固定する
    だけで走査順序を決定的にできる(抽出の並列度=iteratorのスレッド数は変えない。
    ここで直列化しているのはfind_duplicatesのバケツ構築であって抽出そのものではない)。"""
    buckets: dict[tuple, list[tuple[str, np.ndarray, np.ndarray]]] = {}

    for shape_id, shape in sorted(shapes.items()):
        q, v_sorted, order = _canonical(shape, tol)
        key = (q.tobytes(), len(shape.vertices), len(shape.faces))
        faces_c = _canonical_faces(shape.faces, order)
        buckets.setdefault(key, []).append((shape_id, v_sorted, faces_c))

    groups: list[DuplicateGroup] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        _, rep_v, rep_faces = members[0]
        matched_ids = [members[0][0]]
        for shape_id, v_sorted, faces_c in members[1:]:
            max_dist = np.max(np.linalg.norm(v_sorted - rep_v, axis=1))
            if max_dist < tol and np.array_equal(faces_c, rep_faces):
                matched_ids.append(shape_id)
        if len(matched_ids) < 2:
            continue
        triangle_count = len(shapes[matched_ids[0]].faces)
        savable = triangle_count * (len(matched_ids) - 1)
        groups.append(
            DuplicateGroup(
                shape_ids=matched_ids,
                triangle_count=triangle_count,
                savable_triangles=savable,
            )
        )

    groups.sort(key=lambda g: g.savable_triangles, reverse=True)
    return groups
