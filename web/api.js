// api.js -- サーバAPI(/api/load, /api/status, /api/diagnostics, /api/mesh)への
// アクセスとメッシュバイナリのパースを担う。DOM操作は行わない。

/**
 * IFCファイルの読み込みを開始する。
 * @param {string} path サーバのcwdからの相対、または絶対パス
 * @returns {Promise<object>} {status: "loading"} 相当のJSON
 */
export async function loadModel(path) {
  const res = await fetch("/api/load", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`load failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * 読み込み状態を1回取得する。
 * @returns {Promise<{state: string, message: string, elapsed_sec: number}>}
 */
export async function pollStatus() {
  const res = await fetch("/api/status");
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`status failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * 診断結果(スキーマ/要素数/クラス別ランキング等)を取得する。
 * model が ready でない場合は 409。
 */
export async function fetchDiagnostics() {
  const res = await fetch("/api/diagnostics");
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`diagnostics failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * メッシュバイナリを取得しパースする。
 *
 * 形式(little-endian):
 *   [uint32 json_len][json_len bytes UTF-8 JSON meta]
 *   [float32 x vertex_count*3 positions][uint32 x triangle_count*3 indices]
 *
 * 注意: positions の開始オフセット(4+json_len)は4バイト境界に必ずしも
 * 揃っていない。Float32Array/Uint32Arrayはbyte offsetが要素サイズの倍数で
 * ないと例外になるため、その場合は該当区間を slice() してコピーしてから
 * 型付き配列化する。
 *
 * @returns {Promise<{meta: object, positions: Float32Array, indices: Uint32Array}>}
 */
export async function fetchMesh() {
  const res = await fetch("/api/mesh");
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`mesh failed (${res.status}): ${detail}`);
  }
  const buffer = await res.arrayBuffer();
  return parseMeshBuffer(buffer);
}

/**
 * ArrayBufferをメッシュ構造体にパースする(単体テスト/self-test用にexport)。
 * @param {ArrayBuffer} buffer
 */
export function parseMeshBuffer(buffer) {
  const headerView = new DataView(buffer);
  const jsonLen = headerView.getUint32(0, true);
  const jsonBytes = new Uint8Array(buffer, 4, jsonLen);
  const meta = JSON.parse(new TextDecoder("utf-8").decode(jsonBytes));

  const vertexFloatCount = meta.vertex_count * 3;
  const indexIntCount = meta.triangle_count * 3;

  const positionsOffset = 4 + jsonLen;
  const positionsByteLength = vertexFloatCount * 4;
  const positions = _readTypedArray(
    buffer,
    positionsOffset,
    positionsByteLength,
    Float32Array
  );

  const indicesOffset = positionsOffset + positionsByteLength;
  const indicesByteLength = indexIntCount * 4;
  const indices = _readTypedArray(
    buffer,
    indicesOffset,
    indicesByteLength,
    Uint32Array
  );

  return { meta, positions, indices };
}

/**
 * offset が TypedArray の要素サイズに整列していない場合は該当範囲を
 * slice() してコピーしてから型付き配列化する(整列していればゼロコピーで
 * ビューを作る)。
 */
function _readTypedArray(buffer, byteOffset, byteLength, TypedArrayCtor) {
  const elementSize = TypedArrayCtor.BYTES_PER_ELEMENT;
  if (byteOffset % elementSize === 0) {
    return new TypedArrayCtor(buffer, byteOffset, byteLength / elementSize);
  }
  const copy = buffer.slice(byteOffset, byteOffset + byteLength);
  return new TypedArrayCtor(copy);
}

/**
 * 現在の操作リストをサーバへ全置換する。
 * @param {Array<object>} operations {op, targets, scope, params} のリスト
 * @returns {Promise<{warnings: string[]}>}
 */
export async function postOps(operations) {
  const res = await fetch("/api/ops", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operations }),
  });
  if (!res.ok) {
    const detail = await _safeDetail(res);
    const err = new Error(`ops post failed (${res.status}): ${detail}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/**
 * サーバ側の現在の操作リストを取得する。
 * @returns {Promise<{operations: Array<object>}>}
 */
export async function getOps() {
  const res = await fetch("/api/ops");
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`ops fetch failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * 削除の連鎖プレビューを取得する(適用はしない)。
 * @param {string[]} targets
 * @returns {Promise<{direct:number, cascaded:Array<object>, total:number}>}
 */
export async function previewDelete(targets) {
  const res = await fetch("/api/ops/preview-delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ targets }),
  });
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`preview-delete failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * 要素の共有数(同一IfcRepresentationMap参照要素数)を取得する。
 * @param {string} gid
 * @returns {Promise<{shared_count: number}>}
 */
export async function fetchSharing(gid) {
  const res = await fetch(`/api/element/${encodeURIComponent(gid)}/sharing`);
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`sharing failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * 複数gidの共有数を一括取得する(選択要素数分のfetch fan-outを避けるための1回POST)。
 * 未知gidはエラーにならずcount=0で返る。
 * @param {string[]} gids
 * @returns {Promise<{counts: Record<string, number>}>}
 */
export async function fetchSharingBatch(gids) {
  const res = await fetch("/api/elements/sharing", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gids }),
  });
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`sharing batch failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * 操作リストを適用した新規IFCの出力を開始する(非同期、/api/statusでポーリング)。
 * @param {string} outputPath
 * @param {boolean} [consolidate=false] 重複形状を共有化して出力するか(既定false)。
 * @param {boolean} [inlineCleanup=false] 省メモリ方式(逐次ゴミ回収)で書き出すか。
 *   true で geometry_cleanup="inline"(既定はサーバ側と同じ "gc")。
 * @returns {Promise<{status: string}>}
 */
export async function startExport(outputPath, consolidate = false, inlineCleanup = false) {
  const res = await fetch("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      output_path: outputPath,
      consolidate,
      geometry_cleanup: inlineCleanup ? "inline" : "gc",
    }),
  });
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`export failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * 保存済みプリセット一覧を取得する。
 * @returns {Promise<Array<{name:string, description:string, rules:Array<object>}>>}
 */
export async function fetchPresets() {
  const res = await fetch("/api/presets");
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`presets fetch failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * プリセット一覧を全置換で保存する。
 * @param {Array<object>} presets
 * @returns {Promise<Array<object>>} 保存後の一覧
 */
export async function postPresets(presets) {
  const res = await fetch("/api/presets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(presets),
  });
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`presets post failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * 名前を指定してプリセット(操作パターン)を削除する(GUI改修Task6)。
 * サーバはクエリパラメータ方式(DELETE /api/presets?name=...)を採る——
 * パスパラメータ方式(DELETE /api/presets/{name})だと name が "/" を含む
 * 場合にStarletteの既定コンバータがマッチせず404になるため
 * (ifc_occam/server/app.py delete_preset_endpoint 参照)。
 * 存在しない名前は404(detail付きErrorとしてthrow)。
 * @param {string} name
 * @returns {Promise<Array<object>>} 削除後の一覧
 */
export async function deletePreset(name) {
  const res = await fetch(`/api/presets?name=${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const detail = await _safeDetail(res);
    const err = new Error(`preset delete failed (${res.status}): ${detail}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/**
 * 名前を指定してプリセットを現在のモデルに対して解決する(適用はしない)。
 * @param {string} name
 * @returns {Promise<{rules:Array<object>, warnings:string[]}>}
 */
export async function resolvePreset(name) {
  const res = await fetch("/api/presets/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`presets resolve failed (${res.status}): ${detail}`);
  }
  return res.json();
}

/**
 * サーバ起動フォルダ(root)配下のディレクトリ一覧を取得する(GUI改修Task4)。
 * root外を指すpathは400、存在しないpathは404(エラーはdetailに理由文字列を
 * 持つErrorとしてthrowする。filedialog.jsがモーダル内に日本語で表示する)。
 * @param {string} [path] root相対パス("" ならroot直下)
 * @returns {Promise<{path:string, parent:string|null, entries:Array<{name:string,is_dir:boolean,size:number|null,mtime:number}>}>}
 */
export async function fetchFileList(path = "") {
  const res = await fetch(`/api/files?path=${encodeURIComponent(path)}`);
  if (!res.ok) {
    const detail = await _safeDetail(res);
    const err = new Error(`file list failed (${res.status}): ${detail}`);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.json();
}

/**
 * サーバ側の定数(フルオープン推定倍率・警告閾値・読込時間推定の一次式係数)を
 * 取得する(GUI改修Task4)。JS側にこれらの値を写経しないための唯一の取得経路。
 * @returns {Promise<{fullopen_bytes_multiplier:number, fullopen_warn_bytes:number,
 *   load_estimate:{sec_per_mb:number, base_sec:number, band_low:number, band_high:number}}>}
 */
export async function fetchConfig() {
  const res = await fetch("/api/config");
  if (!res.ok) {
    const detail = await _safeDetail(res);
    throw new Error(`config fetch failed (${res.status}): ${detail}`);
  }
  return res.json();
}

async function _safeDetail(res) {
  try {
    const data = await res.json();
    return data.detail ?? JSON.stringify(data);
  } catch (_err) {
    return res.statusText;
  }
}
