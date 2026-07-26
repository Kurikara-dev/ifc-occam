// selection.js -- 選択状態(SelectionModel)と三角形index→要素の二分探索を担う。
// DOM/Three.jsには関与しない(純粋ロジック)。

/**
 * 三角形indexから対応する要素(meta.elements の1件)を二分探索で求める。
 * elements は tri_start 昇順であることを前提とする。
 * @param {Array<{tri_start:number, tri_count:number}>} elements
 * @param {number} triIndex
 * @returns {object|null} 見つかった要素、範囲外なら null
 */
export function triangleToElement(elements, triIndex) {
  if (!elements || elements.length === 0) return null;
  if (triIndex < 0) return null;

  let lo = 0;
  let hi = elements.length - 1;

  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const el = elements[mid];
    const start = el.tri_start;
    const end = start + el.tri_count; // exclusive
    if (triIndex < start) {
      hi = mid - 1;
    } else if (triIndex >= end) {
      lo = mid + 1;
    } else {
      return el;
    }
  }
  return null;
}

/**
 * 選択状態(global_idのSet)を保持し、変更を購読者に通知する。
 */
export class SelectionModel {
  constructor() {
    this._selected = new Set();
    this._listeners = [];
  }

  /** @returns {Set<string>} 現在選択中のglobal_id集合(読み取り専用として扱うこと) */
  get selected() {
    return this._selected;
  }

  /**
   * @param {(selected: Set<string>) => void} callback
   * @returns {() => void} 解除関数
   */
  onChange(callback) {
    this._listeners.push(callback);
    return () => {
      const idx = this._listeners.indexOf(callback);
      if (idx >= 0) this._listeners.splice(idx, 1);
    };
  }

  _emit() {
    for (const cb of this._listeners) cb(this._selected);
  }

  /**
   * 指定したglobal_id集合を既存選択に追加する(既存選択は保持したまま増やす)。
   * selectByGidsと同じ既知gid判定方式で、elements(meta.elements)に存在しない
   * gidは黙って除外する(未知gidを含めて渡してもエラーにはしない)。すでに
   * 選択済みのgidを追加してもSetの性質上重複はしない。
   * GUI改修 Task2: 一覧行のShift+クリック(選択に追加)で使う。
   * @param {Iterable<string>} gids
   * @param {Array<object>} elements meta.elements(既知gid判定用)
   */
  addGids(gids, elements) {
    const known = new Set(elements.map((el) => el.global_id));
    const next = new Set(this._selected);
    for (const gid of gids) {
      if (known.has(gid)) next.add(gid);
    }
    this._selected = next;
    this._emit();
  }

  /**
   * 指定したglobal_id集合を既存選択から除外する(既存選択のうち該当gidのみ
   * 外す。他は変えない)。選択に含まれていないgidを渡しても何も起きない
   * (Set.deleteは存在しないキーに対しても安全)。addGidsと異なりelementsに
   * よる既知gid判定は不要(選択されていないgidの除外は常に無害)。
   * GUI改修 Task2: 一覧行のCtrl/Cmd+クリック(選択から除外)で使う。
   * @param {Iterable<string>} gids
   */
  removeGids(gids) {
    const next = new Set(this._selected);
    for (const gid of gids) {
      next.delete(gid);
    }
    this._selected = next;
    this._emit();
  }

  /**
   * 単一要素の選択をトグルする(既存選択は保持したまま追加/削除)。
   * @param {string} gid
   */
  toggleElement(gid) {
    const next = new Set(this._selected);
    if (next.has(gid)) {
      next.delete(gid);
    } else {
      next.add(gid);
    }
    this._selected = next;
    this._emit();
  }

  /** 選択を全解除する。 */
  clear() {
    this._selected = new Set();
    this._emit();
  }

  /**
   * 指定したglobal_id集合を選択する(既存選択は置き換える)。
   * 重複群パネルなど、あらかじめ確定したgidリストを一括選択する用途。
   * elements(meta.elements)に存在しないgidは黙って除外する(フィルタ方式。
   * 未知gidを含めて渡してもエラーにはしない)。
   * @param {Iterable<string>} gids
   * @param {Array<object>} elements meta.elements(既知gid判定用)
   */
  selectByGids(gids, elements) {
    const known = new Set(elements.map((el) => el.global_id));
    const next = new Set();
    for (const gid of gids) {
      if (known.has(gid)) next.add(gid);
    }
    this._selected = next;
    this._emit();
  }
}
