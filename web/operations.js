// operations.js -- 操作リスト(OperationList)とlast-wins解決(resolve_effective)を
// フロントで再現する。ifc_occam/core/ops.py の契約(Operation: op/targets/scope/params、
// resolve_effective の後方勝ち)に一致させる。DOM/Three.jsには関与しない(純粋ロジック)。

// docs/plans/2026-07-23-phase3-operations.md
// 「3D上の操作ステータス表示 (Task 7)」のステータス色契約。選択赤(既存)はここでは
// 扱わず、呼び出し側(app.js)で選択が優先される。
const STATUS_COLORS = {
  delete: [0.25, 0.25, 0.25],
  simplify: [0.3, 0.5, 1.0],
  keep: [0.3, 0.9, 0.4],
};

/**
 * 操作(op)に対応するステータス色を返す。未知のopは白を返す(呼び出し側で
 * 想定外を検知しやすくするため)。
 * @param {{op: string}} operation
 * @returns {[number, number, number]}
 */
export function statusColor(operation) {
  return STATUS_COLORS[operation.op] ?? [1, 1, 1];
}

/**
 * 操作リストから gid ごとの有効操作を求める(後方勝ち)。
 * core/ops.py の resolve_effective と同じ規則: 同一 GlobalId に複数の
 * 操作がある場合、リストの後方が勝つ。keep は「対象外に確定」を表す
 * マーカーであり、それ自体が結果に含まれる。
 * @param {Array<{op:string, targets:string[], scope?:string, params?:object}>} operations
 * @returns {Map<string, object>} global_id -> 有効な operation
 */
export function resolveEffective(operations) {
  const effective = new Map();
  for (const operation of operations) {
    for (const gid of operation.targets) {
      effective.set(gid, operation);
    }
  }
  return effective;
}

/**
 * 操作リストを配列で保持し、変更を購読者に通知する。
 * サーバ同期(POST /api/ops のデバウンス)は行わない(呼び出し側の責務)。
 */
export class OperationList {
  constructor() {
    this._operations = [];
    this._listeners = [];
  }

  /** @returns {Array<object>} 現在の操作リスト(読み取り専用として扱うこと) */
  get operations() {
    return this._operations;
  }

  /**
   * @param {(operations: Array<object>) => void} callback
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
    for (const cb of this._listeners) cb(this._operations);
  }

  /** @param {{op:string, targets:string[], scope?:string, params?:object}} operation */
  add(operation) {
    this._operations.push(operation);
    this._emit();
  }

  /** @param {number} index */
  remove(index) {
    if (index < 0 || index >= this._operations.length) return;
    this._operations.splice(index, 1);
    this._emit();
  }

  clear() {
    this._operations = [];
    this._emit();
  }

  /** @returns {Map<string, object>} 現在のリストの有効操作(resolve_effective 相当) */
  resolveEffective() {
    return resolveEffective(this._operations);
  }

  /** @returns {string} JSON文字列化(サーバ /api/ops のoperations配列と同じ形) */
  toJson() {
    return JSON.stringify(this._operations);
  }

  /** @param {string} json toJson() の出力を復元する */
  fromJson(json) {
    this._operations = JSON.parse(json);
    this._emit();
  }
}
