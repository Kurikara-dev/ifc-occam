// app.js -- UIワイヤリング(薄い層)。api.js/viewer.jsのロジックを結線するのみ。

import {
  loadModel,
  pollStatus,
  fetchDiagnostics,
  fetchMesh,
  postOps,
  previewDelete,
  fetchSharingBatch,
  startExport,
  fetchPresets,
  postPresets,
  resolvePreset,
} from "./api.js";
import { initViewer, classColor } from "./viewer.js";
import { SelectionModel, triangleToElement } from "./selection.js";
import { OperationList, resolveEffective, statusColor } from "./operations.js";

const canvas = document.getElementById("canvas");
const pathInput = document.getElementById("path-input");
const loadButton = document.getElementById("load-button");
const statusLine = document.getElementById("status-line");
const errorBanner = document.getElementById("error-banner");
const diagnosticsSummary = document.getElementById("diagnostics-summary");
const classTableTitle = document.getElementById("class-table-title");
const classTableWrap = document.getElementById("class-table-wrap");
const layerTitle = document.getElementById("layer-title");
const layerList = document.getElementById("layer-list");
const clearButton = document.getElementById("clear-button");
const infoTitle = document.getElementById("info-title");
const infoPanel = document.getElementById("info-panel");
const selectionNotice = document.getElementById("selection-notice");
const duplicatesTitle = document.getElementById("duplicates-title");
const duplicatesList = document.getElementById("duplicates-list");
const duplicatesNote = document.getElementById("duplicates-note");
const opsControlsTitle = document.getElementById("ops-controls-title");
const opsControls = document.getElementById("ops-controls");
const opDeleteButton = document.getElementById("op-delete-button");
const opSimplifyMethod = document.getElementById("op-simplify-method");
const opSimplifyRatio = document.getElementById("op-simplify-ratio");
const opSimplifyButton = document.getElementById("op-simplify-button");
const opKeepButton = document.getElementById("op-keep-button");
const opsPanelTitle = document.getElementById("ops-panel-title");
const opsPanelList = document.getElementById("ops-panel-list");
const opsClearButton = document.getElementById("ops-clear-button");
const exportPathInput = document.getElementById("export-path-input");
const exportConsolidateCheckbox = document.getElementById("export-consolidate-checkbox");
const exportButton = document.getElementById("export-button");
const exportResult = document.getElementById("export-result");
const presetList = document.getElementById("preset-list");
const presetEmptyNotice = document.getElementById("preset-empty-notice");
const presetLoadSamplesButton = document.getElementById("preset-load-samples-button");
const presetSaveButton = document.getElementById("preset-save-button");

// ifc_occam/core/ops.py の _VALID_OPS/_VALID_SCOPES/_VALID_SIMPLIFY_METHODS を
// フロントでも検証できるよう鏡写しにする(Task3から持ち越されたギャップ。
// プリセット適用モーダルで不正なルールをエラー行として弾くために使う)。
const VALID_OPS = new Set(["delete", "simplify", "keep"]);
const VALID_SCOPES = new Set(["element", "shared"]);
const VALID_SIMPLIFY_METHODS = new Set(["bbox", "convex_hull", "decimate"]);

/**
 * プリセットルールのop辞書(resolve応答のrule.op)が有効か判定する。
 * core/ops.py validate_operations の検証内容と揃える(サーバ非依存の事前チェック)。
 * @param {object} op {op, scope?, params?}
 * @returns {boolean}
 */
function isValidRuleOp(op) {
  if (!op || typeof op !== "object" || !VALID_OPS.has(op.op)) return false;
  const scope = op.scope ?? "element";
  if (!VALID_SCOPES.has(scope)) return false;
  if (op.op === "simplify") {
    const method = (op.params ?? {}).method;
    if (!VALID_SIMPLIFY_METHODS.has(method)) return false;
    if (method === "decimate") {
      const ratio = (op.params ?? {}).ratio;
      if (typeof ratio !== "number" || !(ratio > 0 && ratio < 1)) return false;
    }
  }
  return true;
}

const viewer = initViewer(canvas);
const selectionModel = new SelectionModel();
const operationList = new OperationList();

let pollHandle = null;
let lastLoadedPath = ""; // 読込に使ったパス文字列(export既定パスの元名導出に使う)
let currentMeta = null; // /api/mesh の meta (elements配列を含む)
let gidToElement = new Map(); // global_id -> element (meta.elements の1件)
let selectionNoticeTimer = null;
// クラス/レイヤーの行ハイライトをO(全要素)全走査せずに判定するための前計算索引。
// onModelReadyで構築し、renderSelectionでは各クラス/レイヤー自身の要素数分だけを見る。
let classIndex = new Map(); // ifc_class -> Set(global_id)
let layerIndex = new Map(); // layer -> Set(global_id)
// 前回選択されていたgid集合。差分のみ色を戻すために保持する(全頂点リセットの代替)。
let prevSelected = new Set();
// 重複群パネルの行と、その行が指す要素gid集合(全選択判定用)。
let duplicateRows = []; // Array<{el: HTMLElement, gids: Set<string>}>
// 操作リストの有効操作(resolve_effective相当)。色戻し(revert)判定に使うため
// 常に最新のものを保持する。前回分は差分反映(変化したgidのみ)のために保持。
let currentEffective = new Map(); // global_id -> operation
let prevEffective = new Map();
// Final Review Fix2 (frontend): 確定した削除操作(operation)ごとに、そのプレビュー
// 時点で連鎖していたgid集合(keep上書き分含む)を保持する。closureの子(開口・充填要素)
// はcurrentEffectiveに自分自身のOperationを持たない(operationのtargetsは直接対象の
// gidのみ)ため、通常のapplyOperationColorsでは削除色にならない。これを補うための
// 追加着色専用マップ。key=operation object(参照)、value=string[](gid配列)。
// クリア: op-cancel-buttonクリック時とops-clear-button(全消去)時。
let cascadePreviewByOp = new Map();
// scope="shared"のsimplify確定時に着色する兄弟gid(同一RepresentationMap参照要素、
// export時に実際に一緒に変わる範囲を正直に見せる。Phase4 Task4 §3)。
// cascadePreviewByOpと同じ寿命(op-cancel-button/ops-clear-button/reloadでクリア)。
// key=operation object(参照)、value=string[](sibling gid配列)。
let sharedSiblingsByOp = new Map();
let opsSyncTimer = null;
// export中はサーバがopsを409で拒否する(exporting中は状態変更不可のゲート)。
// これはエラーではなく想定挙動なので、フラグを立てておきready復帰後に再送する。
let opsResyncPending = false;

/**
 * 選択パネル付近に短時間だけ非モーダルの通知を表示する(alert()は使わない)。
 * @param {string} message
 */
function showSelectionNotice(message) {
  if (selectionNoticeTimer) clearTimeout(selectionNoticeTimer);
  selectionNotice.textContent = message;
  selectionNotice.style.display = "block";
  selectionNoticeTimer = setTimeout(() => {
    selectionNotice.style.display = "none";
    selectionNotice.textContent = "";
  }, 4000);
}

function clearSelectionNotice() {
  if (selectionNoticeTimer) {
    clearTimeout(selectionNoticeTimer);
    selectionNoticeTimer = null;
  }
  selectionNotice.style.display = "none";
  selectionNotice.textContent = "";
}

function showError(message) {
  errorBanner.textContent = message;
  errorBanner.style.display = "block";
}

function clearError() {
  errorBanner.style.display = "none";
  errorBanner.textContent = "";
}

function setLoadingUI(isLoading) {
  loadButton.disabled = isLoading;
  pathInput.disabled = isLoading;
}

async function handleLoadClick() {
  const path = pathInput.value.trim();
  if (!path) {
    showError("ファイルパスを入力してください。");
    return;
  }
  clearError();
  clearSelectionNotice();
  setLoadingUI(true);
  diagnosticsSummary.innerHTML = "";
  classTableWrap.innerHTML = "";
  classTableTitle.style.display = "none";
  statusLine.textContent = "読込を開始しています...";
  lastLoadedPath = path;

  try {
    await loadModel(path);
  } catch (err) {
    showError(String(err.message || err));
    setLoadingUI(false);
    return;
  }

  startPolling();
}

function startPolling() {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(pollOnce, 1000);
  pollOnce();
}

function stopPolling() {
  if (pollHandle) {
    clearInterval(pollHandle);
    pollHandle = null;
  }
}

async function pollOnce() {
  let status;
  try {
    status = await pollStatus();
  } catch (err) {
    stopPolling();
    setLoadingUI(false);
    showError(String(err.message || err));
    return;
  }

  if (status.state === "loading") {
    statusLine.textContent = `読込中... (${status.elapsed_sec.toFixed(1)}秒)`;
    return;
  }

  stopPolling();
  setLoadingUI(false);

  if (status.state === "error") {
    statusLine.textContent = "";
    showError(status.message || "読込に失敗しました。");
    return;
  }

  if (status.state === "ready") {
    statusLine.textContent = `読込完了 (${status.elapsed_sec.toFixed(1)}秒)`;
    await onModelReady();
    resyncOpsIfPending();
  }
}

/**
 * 読込パス(ユーザーが入力した文字列)から export の既定出力ファイル名を導出する。
 * 「<元名>_light.ifc」。ディレクトリ部分は捨てる(出力先解決はサーバ側で
 * 読込モデルのディレクトリ基準で行うため、ここではファイル名だけでよい)。
 * @param {string} loadedPath
 * @returns {string}
 */
function deriveDefaultExportName(loadedPath) {
  const base = loadedPath.split(/[/\\]/).pop() || "output.ifc";
  const dot = base.lastIndexOf(".");
  const stem = dot > 0 ? base.slice(0, dot) : base;
  return `${stem}_light.ifc`;
}

async function onModelReady() {
  try {
    const [diagnostics, meshData] = await Promise.all([
      fetchDiagnostics(),
      fetchMesh(),
    ]);
    currentMeta = meshData.meta;
    exportPathInput.value = deriveDefaultExportName(lastLoadedPath);
    gidToElement = new Map(currentMeta.elements.map((el) => [el.global_id, el]));
    buildSelectionIndexes(currentMeta.elements);
    prevSelected = new Set();
    selectionModel.clear();
    renderDiagnostics(diagnostics);
    renderLayers(diagnostics.layers || []);
    renderDuplicates(diagnostics.duplicate_groups || []);
    viewer.setMesh(meshData.meta, meshData.positions, meshData.indices);
    // 新規読込ではサーバ側の操作リストも空(state.set_readyでリセット済み)なので
    // フロントも追従させる(古いgidの色戻し試行はel未検出で黙って無視される)。
    exportResult.innerHTML = "";
    // cascadePreviewByOpはoperation object参照をキーにした追加着色マップ。
    // operationList.clear()のonChange->applyOperationColors再描画で古い連鎖gidが
    // ghost paintされないよう、clear()より前に空にしておく。
    cascadePreviewByOp.clear();
    sharedSiblingsByOp.clear();
    operationList.clear();
  } catch (err) {
    showError(String(err.message || err));
  }
}

/**
 * class/layer -> Set(global_id) の索引を構築する。
 * renderSelection の行ハイライト判定を「全要素の全走査」から
 * 「そのクラス/レイヤー自身の要素数分」に落とすための前計算。
 * @param {Array<object>} elements meta.elements
 */
function buildSelectionIndexes(elements) {
  classIndex = new Map();
  layerIndex = new Map();
  for (const el of elements) {
    if (!classIndex.has(el.ifc_class)) classIndex.set(el.ifc_class, new Set());
    classIndex.get(el.ifc_class).add(el.global_id);

    if (el.layer != null) {
      if (!layerIndex.has(el.layer)) layerIndex.set(el.layer, new Set());
      layerIndex.get(el.layer).add(el.global_id);
    }
  }
}

/**
 * group(Set)の全要素がselectedに含まれるかを判定する。
 * groupの要素数分だけ見れば済み、selectedやelements全体を走査しない。
 * @param {Set<string>} group
 * @param {Set<string>} selected
 */
function isFullySelected(group, selected) {
  if (!group || group.size === 0) return false;
  for (const gid of group) {
    if (!selected.has(gid)) return false;
  }
  return true;
}

function renderDiagnostics(diagnostics) {
  const excludedCount =
    currentMeta && typeof diagnostics.element_count === "number"
      ? diagnostics.element_count - currentMeta.elements.length
      : 0;
  const excludedLine =
    excludedCount > 0
      ? `<div id="excluded-line">描画対象外: ${excludedCount}要素</div>`
      : "";

  diagnosticsSummary.innerHTML = `
    <div>スキーマ: ${escapeHtml(diagnostics.schema)}</div>
    <div>要素数: ${diagnostics.element_count}</div>
    <div>総三角形数: ${diagnostics.total_triangles}</div>
    <div>警告数: ${diagnostics.warnings.length}</div>
    ${excludedLine}
  `;

  const sorted = [...diagnostics.class_stats].sort(
    (a, b) => b.total_triangles - a.total_triangles
  );

  const rows = sorted
    .map(
      (s) => `
      <tr data-class="${escapeHtml(s.ifc_class)}">
        <td>${escapeHtml(s.ifc_class)}</td>
        <td>${s.element_count}</td>
        <td>${s.unique_shape_count}</td>
        <td>${s.total_triangles}</td>
        <td>${s.mapped_count}</td>
        <td>${s.max_single_shape_triangles}</td>
      </tr>`
    )
    .join("");

  classTableWrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>クラス</th><th>要素数</th><th>形状数</th>
          <th>三角形数</th><th>共有経由</th><th>最大単体</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
  classTableTitle.style.display = "block";

  for (const row of classTableWrap.querySelectorAll("tbody tr")) {
    row.addEventListener("click", () => {
      if (!currentMeta) return;
      selectionModel.selectByClass(row.dataset.class, currentMeta.elements);
      if (selectionModel.selected.size === 0) {
        showSelectionNotice("この分類に描画対象の要素はありません");
      } else {
        clearSelectionNotice();
      }
    });
  }
}

/**
 * レイヤー一覧を描画する。空なら要件§5.2に従いセクション非表示。
 * @param {string[]} layers
 */
function renderLayers(layers) {
  layerList.innerHTML = "";
  if (!layers || layers.length === 0) {
    layerTitle.style.display = "none";
    layerList.style.display = "none";
    return;
  }
  layerTitle.style.display = "block";
  layerList.style.display = "block";

  for (const layer of layers) {
    const li = document.createElement("li");
    li.textContent = layer;
    li.dataset.layer = layer;
    li.addEventListener("click", () => {
      if (!currentMeta) return;
      selectionModel.selectByLayer(layer, currentMeta.elements);
      if (selectionModel.selected.size === 0) {
        showSelectionNotice("この分類に描画対象の要素はありません");
      } else {
        clearSelectionNotice();
      }
    });
    layerList.appendChild(li);
  }
}

/**
 * gidリストを選択する(重複群パネルの行/ボタン共通処理)。空選択なら通知を出す。
 * @param {string[]} gids
 * @returns {boolean} 何かが選択されたか
 */
function selectDuplicateGroup(gids) {
  if (!currentMeta) return false;
  selectionModel.selectByGids(gids, currentMeta.elements);
  if (selectionModel.selected.size === 0) {
    showSelectionNotice("この分類に描画対象の要素はありません");
    return false;
  }
  clearSelectionNotice();
  return true;
}

/**
 * 操作セクションへスクロールし、1秒間ハイライトする(重複群「選択して操作」導線)。
 */
function scrollToOpsControls() {
  opsControlsTitle.scrollIntoView({ behavior: "smooth", block: "center" });
  opsControlsTitle.classList.add("flash-highlight");
  opsControls.classList.add("flash-highlight");
  setTimeout(() => {
    opsControlsTitle.classList.remove("flash-highlight");
    opsControls.classList.remove("flash-highlight");
  }, 1000);
}

const DUPLICATE_DISPLAY_CAP = 50;

/**
 * 重複形状グループ一覧を描画する。/api/diagnostics の duplicate_groups
 * (各要素group_gidsは shape_id毎のリストのlist、平坦化して使う)。
 * 節約可能三角形数降順、上位50件までのみDOMに出す(件数が超える場合は注記表示)。
 * @param {Array<object>} groups diagnostics.duplicate_groups
 */
function renderDuplicates(groups) {
  duplicatesList.innerHTML = "";
  duplicateRows = [];

  if (!groups || groups.length === 0) {
    duplicatesTitle.style.display = "none";
    duplicatesList.style.display = "none";
    duplicatesNote.style.display = "none";
    return;
  }

  const sorted = [...groups].sort((a, b) => b.savable_triangles - a.savable_triangles);
  const capped = sorted.slice(0, DUPLICATE_DISPLAY_CAP);

  duplicatesTitle.style.display = "block";
  duplicatesList.style.display = "block";
  duplicatesNote.style.display = sorted.length > DUPLICATE_DISPLAY_CAP ? "block" : "none";
  if (sorted.length > DUPLICATE_DISPLAY_CAP) {
    duplicatesNote.textContent = `上位50件を表示(全${sorted.length}件)`;
  }

  for (const group of capped) {
    const flatGids = group.element_gids.flat();
    const gidSet = new Set(flatGids);

    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `件数=${group.shape_ids.length} 節約可能三角形数=${group.savable_triangles}`;
    label.addEventListener("click", () => selectDuplicateGroup(flatGids));
    const batchButton = document.createElement("button");
    batchButton.type = "button";
    batchButton.className = "dup-batch-button";
    batchButton.textContent = "選択して操作";
    batchButton.addEventListener("click", (evt) => {
      evt.stopPropagation();
      if (!selectDuplicateGroup(flatGids)) return;
      scrollToOpsControls();
    });
    li.appendChild(label);
    li.appendChild(batchButton);
    li.addEventListener("click", () => selectDuplicateGroup(flatGids));
    duplicatesList.appendChild(li);
    duplicateRows.push({ el: li, gids: gidSet });
  }
}

/**
 * 選択状態を3Dビューと行のハイライト・情報パネルに反映する。
 * @param {Set<string>} selected
 */
function renderSelection(selected) {
  // 色戻しは「前回選択されていて今回は選択されていない要素」の頂点範囲のみ
  // 戻す(resetColors()による全頂点走査はもう選択経路では呼ばない)。
  // 戻す先は有効操作のステータス色(あれば)、なければクラス色(選択赤が優先、
  // 3D上の操作ステータス表示契約: docs/plans/2026-07-23-phase3-operations.md)。
  for (const gid of prevSelected) {
    if (selected.has(gid)) continue;
    const el = gidToElement.get(gid);
    if (!el) continue;
    const operation = currentEffective.get(gid);
    const [r, g, b] = operation ? statusColor(operation) : classColor(el.ifc_class);
    viewer.setElementColor(el, r, g, b);
  }
  for (const gid of selected) {
    const el = gidToElement.get(gid);
    if (el) viewer.setElementColor(el, 1, 0.2, 0.2);
  }
  prevSelected = new Set(selected);

  opsControlsTitle.style.display = selected.size > 0 ? "block" : "none";
  opsControls.style.display = selected.size > 0 ? "block" : "none";

  for (const row of classTableWrap.querySelectorAll("tbody tr")) {
    const group = classIndex.get(row.dataset.class);
    row.classList.toggle("selected-row", isFullySelected(group, selected));
  }

  for (const li of layerList.querySelectorAll("li")) {
    const group = layerIndex.get(li.dataset.layer);
    li.classList.toggle("selected-row", isFullySelected(group, selected));
  }

  for (const { el, gids } of duplicateRows) {
    el.classList.toggle("selected-row", isFullySelected(gids, selected));
  }

  renderInfoPanel(selected);
}

function renderInfoPanel(selected) {
  if (selected.size === 0) {
    infoTitle.style.display = "none";
    infoPanel.innerHTML = "";
    return;
  }
  infoTitle.style.display = "block";

  if (selected.size === 1) {
    const gid = [...selected][0];
    const el = gidToElement.get(gid);
    if (!el) {
      infoPanel.innerHTML = "";
      return;
    }
    infoPanel.innerHTML = `
      <div>GlobalId: ${escapeHtml(el.global_id)}</div>
      <div>クラス: ${escapeHtml(el.ifc_class)}</div>
      <div>名前: ${escapeHtml(el.name ?? "(なし)")}</div>
      <div>レイヤー: ${escapeHtml(el.layer ?? "(なし)")}</div>
      <div>三角形数: ${el.tri_count}</div>
    `;
  } else {
    infoPanel.innerHTML = `<div>${selected.size} 要素選択中</div>`;
  }
}

/**
 * 有効操作(currentEffective)が変化したgidのみ3D上の色を更新する。
 * 選択中(赤)のgidは触らない(選択が解除された時にrenderSelectionが反映する)。
 */
function applyOperationColors() {
  const changed = new Set([...prevEffective.keys(), ...currentEffective.keys()]);
  for (const gid of changed) {
    if (selectionModel.selected.has(gid)) continue;
    const el = gidToElement.get(gid);
    if (!el) continue;
    const operation = currentEffective.get(gid);
    const [r, g, b] = operation ? statusColor(operation) : classColor(el.ifc_class);
    viewer.setElementColor(el, r, g, b);
  }
  prevEffective = currentEffective;

  // Final Review Fix2: 確定済み削除操作のプレビュー連鎖(cascadePreviewByOp)も
  // 削除色で塗る。これらのgidはcurrentEffectiveに自分自身のOperationを持たない
  // (openings/fillings等はtargetsに含まれない)が、export時には一緒に消える。
  if (cascadePreviewByOp.size > 0) {
    const deleteColor = statusColor({ op: "delete" });
    for (const gids of cascadePreviewByOp.values()) {
      for (const gid of gids) {
        if (selectionModel.selected.has(gid)) continue;
        const el = gidToElement.get(gid);
        if (!el) continue;
        viewer.setElementColor(el, deleteColor[0], deleteColor[1], deleteColor[2]);
      }
    }
  }

  // Phase4 Task4 §3: scope="shared"のsimplify確定時、同一RepresentationMapを
  // 参照する兄弟要素(export時に実際に一緒に変わる範囲)も軽量化色で着色する。
  if (sharedSiblingsByOp.size > 0) {
    const simplifyColor = statusColor({ op: "simplify" });
    for (const gids of sharedSiblingsByOp.values()) {
      for (const gid of gids) {
        if (selectionModel.selected.has(gid)) continue;
        if (currentEffective.has(gid)) continue; // 自身の操作色を優先(上書きしない)
        const el = gidToElement.get(gid);
        if (!el) continue;
        viewer.setElementColor(el, simplifyColor[0], simplifyColor[1], simplifyColor[2]);
      }
    }
  }
}

function opLabel(op) {
  return { delete: "削除", simplify: "軽量化", keep: "残す" }[op] ?? op;
}

function summarizeParams(operation) {
  const scopeLabel = operation.scope === "shared" ? "共有波及" : "";
  if (operation.op !== "simplify") return scopeLabel;
  const method = operation.params.method;
  const methodLabel =
    method === "decimate" ? `${method} ratio=${operation.params.ratio}` : method;
  return scopeLabel ? `${methodLabel} / ${scopeLabel}` : methodLabel;
}

/**
 * 操作リストパネルを描画する。各行に op種別/対象数/paramsと取消ボタン。
 * @param {Array<object>} operations
 */
function renderOpsPanel(operations) {
  opsPanelList.innerHTML = "";
  const hasOps = operations.length > 0;
  opsPanelTitle.style.display = hasOps ? "block" : "none";
  opsPanelList.style.display = hasOps ? "block" : "none";
  opsClearButton.style.display = hasOps ? "block" : "none";

  operations.forEach((operation, index) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <span>${escapeHtml(opLabel(operation.op))}</span>
      <span>${operation.targets.length}件</span>
      <span>${escapeHtml(summarizeParams(operation))}</span>
      <button class="op-cancel-button" data-index="${index}">取消</button>
    `;
    opsPanelList.appendChild(li);
  });

  for (const btn of opsPanelList.querySelectorAll(".op-cancel-button")) {
    btn.addEventListener("click", () => {
      const index = Number(btn.dataset.index);
      const removedOp = operationList.operations[index];
      cascadePreviewByOp.delete(removedOp);
      sharedSiblingsByOp.delete(removedOp);
      operationList.remove(index);
    });
  }
}

/**
 * 操作リストの変更をデバウンス(300ms)してサーバへ同期する。
 * サーバから返る警告は非致命的通知としてエラーバナーに表示する。
 * @param {Array<object>} operations
 */
function scheduleOpsSync(operations) {
  if (opsSyncTimer) clearTimeout(opsSyncTimer);
  opsSyncTimer = setTimeout(async () => {
    try {
      const result = await postOps(operations);
      opsResyncPending = false;
      if (result.warnings && result.warnings.length > 0) {
        showError(`操作リストの警告: ${result.warnings.join(" / ")}`);
      } else {
        clearError();
      }
    } catch (err) {
      if (err && err.status === 409) {
        // export中の一時的な拒否は想定挙動。エラーバナーは出さず、
        // ready復帰後にresyncOpsIfPendingが現在のops一覧を再送する。
        opsResyncPending = true;
        return;
      }
      showError(String(err.message || err));
    }
  }, 300);
}

/**
 * export中の409で送れなかった操作リストを、ready復帰後に再送する。
 * ポーリング(pollOnce)やexport完了ハンドラから状態がreadyになった時点で呼ぶ。
 */
async function resyncOpsIfPending() {
  if (!opsResyncPending) return;
  opsResyncPending = false;
  try {
    await postOps(operationList.operations);
  } catch (err) {
    if (err && err.status === 409) {
      opsResyncPending = true;
      return;
    }
    showError(String(err.message || err));
  }
}

/**
 * 最小限の再利用可能モーダル(alert/confirmは使わない)。
 * @param {{title:string, bodyHtml:string, actions:Array<{label:string,value:*,primary?:boolean}>}} opts
 * @returns {Promise<*>} クリックされたactionのvalue
 */
function showModal({ title, bodyHtml, actions }) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";

    const box = document.createElement("div");
    box.className = "modal-box";
    box.innerHTML = `<h3>${escapeHtml(title)}</h3><div class="modal-body">${bodyHtml}</div>`;

    const actionsDiv = document.createElement("div");
    actionsDiv.className = "modal-actions";
    for (const action of actions) {
      const btn = document.createElement("button");
      btn.textContent = action.label;
      if (action.primary) btn.className = "modal-btn-primary";
      btn.addEventListener("click", () => {
        overlay.remove();
        resolve(action.value);
      });
      actionsDiv.appendChild(btn);
    }
    box.appendChild(actionsDiv);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  });
}

// ---------------------------------------------------------------------------
// プリセット (Phase4 Task4 §1)
// ---------------------------------------------------------------------------

/**
 * match辞書(ifc_class/layer/min_triangles)を人間可読な短い文字列にする。
 * @param {object} match
 */
function summarizeMatch(match) {
  const parts = [];
  if (match.ifc_class != null) parts.push(`クラス=${match.ifc_class}`);
  if (match.layer != null) parts.push(`レイヤー=${match.layer}`);
  if (match.min_triangles != null) parts.push(`三角形数≥${match.min_triangles}`);
  const unknownKeys = Object.keys(match).filter(
    (k) => !["ifc_class", "layer", "min_triangles"].includes(k)
  );
  for (const k of unknownKeys) parts.push(`${k}=${match[k]}(不明なキー)`);
  return parts.length > 0 ? parts.join(" / ") : "(条件なし)";
}

/** プリセットパネルをサーバから再取得して再描画する。 */
async function loadPresetsPanel() {
  let presets;
  try {
    presets = await fetchPresets();
  } catch (err) {
    showError(String(err.message || err));
    return;
  }
  renderPresetsPanel(presets);
}

/**
 * プリセット一覧を描画する。空なら「サンプルを読み込む」ボタンを表示する
 * (自動読み込みはしない、ユーザー操作が必要)。
 * @param {Array<object>} presets
 */
function renderPresetsPanel(presets) {
  presetList.innerHTML = "";
  presetEmptyNotice.style.display = presets.length === 0 ? "block" : "none";

  for (const preset of presets) {
    const li = document.createElement("li");
    const infoDiv = document.createElement("div");
    infoDiv.innerHTML = `
      <div class="preset-name">${escapeHtml(preset.name)}</div>
      <div class="preset-desc">${escapeHtml(preset.description || "")}</div>
    `;
    const applyButton = document.createElement("button");
    applyButton.type = "button";
    applyButton.className = "preset-apply-button";
    applyButton.textContent = "適用";
    applyButton.addEventListener("click", () => handlePresetApplyClick(preset.name));
    li.appendChild(infoDiv);
    li.appendChild(applyButton);
    presetList.appendChild(li);
  }
}

/**
 * 「サンプルを読み込む」: web/preset-samples.json を取得し POST /api/presets で
 * 保存する(ユーザー操作起点。自動読み込みはしない)。
 */
async function handleLoadSamplePresetsClick() {
  presetLoadSamplesButton.disabled = true;
  try {
    const res = await fetch("./preset-samples.json");
    if (!res.ok) throw new Error(`preset-samples.json取得失敗 (${res.status})`);
    const samples = await res.json();
    await postPresets(samples);
    await loadPresetsPanel();
  } catch (err) {
    showError(String(err.message || err));
  } finally {
    presetLoadSamplesButton.disabled = false;
  }
}

/**
 * 「適用」: POST /api/presets/resolve → ルール別件数モーダルで人間確認(§5.4契約)
 * → 確定で各ルールをOperationとして操作リストに追加する。0件ルール/不正な
 * op(_VALID_OPS/_VALID_SCOPES外)のルールはエラー行として表示し追加しない。
 * @param {string} name
 */
async function handlePresetApplyClick(name) {
  let resolved;
  try {
    resolved = await resolvePreset(name);
  } catch (err) {
    showError(String(err.message || err));
    return;
  }
  const rules = resolved.rules || [];
  const warnings = resolved.warnings || [];

  const rowsHtml = rules
    .map((rule) => {
      const matchLabel = escapeHtml(summarizeMatch(rule.match));
      if (!isValidRuleOp(rule.op)) {
        return `<li>${matchLabel} — <span class="rule-error">エラー: 不正な操作のため追加されません</span></li>`;
      }
      const opLabelStr = escapeHtml(opLabel(rule.op.op));
      const countHtml =
        rule.count === 0
          ? `<span class="rule-count-warning">0件(スキップ)</span>`
          : `${rule.count}件`;
      return `<li>${matchLabel} / ${opLabelStr} / ${countHtml}</li>`;
    })
    .join("");
  const warningsHtml =
    warnings.length > 0
      ? `<div class="modal-warning">${warnings.map((w) => escapeHtml(w)).join("<br>")}</div>`
      : "";

  const confirmed = await showModal({
    title: `プリセット適用: ${name}`,
    bodyHtml: `<ul>${rowsHtml}</ul>${warningsHtml}`,
    actions: [
      { label: "キャンセル", value: false },
      { label: "確定", value: true, primary: true },
    ],
  });
  if (!confirmed) return;

  for (const rule of rules) {
    if (!isValidRuleOp(rule.op)) continue;
    if (rule.count === 0) continue;
    operationList.add({
      op: rule.op.op,
      targets: rule.gids,
      scope: rule.op.scope ?? "element",
      params: rule.op.params ?? {},
    });
  }
}

/**
 * 名前+説明を入力する保存用モーダル(showModalの汎用actionsでは入力値を
 * 読めないため専用実装)。キャンセルはnull、保存は{name, description}を返す
 * (nameが空ならnullとして扱う)。
 * @returns {Promise<{name:string, description:string}|null>}
 */
function showPresetSaveModal() {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const box = document.createElement("div");
    box.className = "modal-box";
    box.innerHTML = `
      <h3>プリセットとして保存</h3>
      <div class="modal-body">
        <label>名前<br><input id="preset-save-name-input" type="text" style="width:100%;"></label>
        <br><br>
        <label>説明<br><textarea id="preset-save-desc-input" style="width:100%;" rows="3"></textarea></label>
      </div>
    `;
    const actionsDiv = document.createElement("div");
    actionsDiv.className = "modal-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "キャンセル";
    cancelBtn.addEventListener("click", () => {
      overlay.remove();
      resolve(null);
    });

    const saveBtn = document.createElement("button");
    saveBtn.textContent = "保存";
    saveBtn.className = "modal-btn-primary";
    saveBtn.addEventListener("click", () => {
      const name = box.querySelector("#preset-save-name-input").value.trim();
      const description = box.querySelector("#preset-save-desc-input").value.trim();
      overlay.remove();
      resolve(name ? { name, description } : null);
    });

    actionsDiv.appendChild(cancelBtn);
    actionsDiv.appendChild(saveBtn);
    box.appendChild(actionsDiv);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  });
}

/**
 * 現在の操作リストからプリセットルールを導出する(§保存ルール、supervisor決定):
 * 操作のtargetsが「あるクラスの全gid」と完全一致する場合のみ{ifc_class}ルールに、
 * 「あるレイヤーの全gid」と完全一致する場合のみ{layer}ルールにする。どちらとも
 * 一致しない操作(部分選択/複数クラス混在等)はクラス/レイヤー単位でないため
 * プリセット化せずスキップする(誠実性: 条件だけでは再現できない選択を
 * ルール化しない)。
 * @param {Array<object>} operations
 * @param {Map<string, Set<string>>} classIndexMap
 * @param {Map<string, Set<string>>} layerIndexMap
 * @returns {{rules: Array<object>, skippedCount: number}}
 */
function deriveRulesFromOperations(operations, classIndexMap, layerIndexMap) {
  const rules = [];
  let skippedCount = 0;

  for (const operation of operations) {
    const targetSet = new Set(operation.targets);
    let match = null;

    for (const [cls, gidSet] of classIndexMap) {
      if (setsEqual(targetSet, gidSet)) {
        match = { ifc_class: cls };
        break;
      }
    }
    if (!match) {
      for (const [layer, gidSet] of layerIndexMap) {
        if (setsEqual(targetSet, gidSet)) {
          match = { layer };
          break;
        }
      }
    }

    if (!match) {
      skippedCount++;
      continue;
    }
    rules.push({
      match,
      op: { op: operation.op, scope: operation.scope, params: operation.params },
    });
  }

  return { rules, skippedCount };
}

/** @param {Set<*>} a @param {Set<*>} b */
function setsEqual(a, b) {
  if (!b || a.size !== b.size) return false;
  for (const v of a) {
    if (!b.has(v)) return false;
  }
  return true;
}

/**
 * 「現在の操作からプリセット保存」: 操作リストをクラス/レイヤー単位のルールに
 * 変換できるものだけプリセット化し、名前入力モーダルで確定後にサーバへ保存する。
 */
async function handleSavePresetFromCurrentClick() {
  const { rules, skippedCount } = deriveRulesFromOperations(
    operationList.operations,
    classIndex,
    layerIndex
  );
  if (rules.length === 0) {
    showError(
      "クラス/レイヤー単位でない操作はプリセット化できません(" +
        `${skippedCount}件)。保存可能な操作がありません。`
    );
    return;
  }

  const saved = await showPresetSaveModal();
  if (!saved) return;

  try {
    const existing = await fetchPresets();
    const updated = [
      ...existing.filter((p) => p.name !== saved.name),
      { name: saved.name, description: saved.description, rules },
    ];
    await postPresets(updated);
    await loadPresetsPanel();
    if (skippedCount > 0) {
      showSelectionNotice(`クラス/レイヤー単位でない操作はプリセット化できません(${skippedCount}件)`);
    }
  } catch (err) {
    showError(String(err.message || err));
  }
}

const CASCADE_DISPLAY_CAP = 20;

/**
 * 「削除」ボタン: preview-delete→モーダルで件数確認→確定でOperation(delete)追加。
 */
async function handleDeleteClick() {
  const targets = [...selectionModel.selected];
  if (targets.length === 0) return;

  opDeleteButton.disabled = true;
  try {
    let preview;
    try {
      preview = await previewDelete(targets);
    } catch (err) {
      showError(String(err.message || err));
      return;
    }

    const cascaded = preview.cascaded || [];
    const shownRows = cascaded.slice(0, CASCADE_DISPLAY_CAP);
    const overflow = cascaded.length - shownRows.length;
    const rowsHtml = shownRows
      .map(
        (c) =>
          `<li>${escapeHtml(c.ifc_class)} ${escapeHtml(c.name ?? "(なし)")} — ${escapeHtml(c.reason)}</li>`
      )
      .join("");
    const overflowHtml = overflow > 0 ? `<li>他${overflow}件</li>` : "";

    // Final Review Fix2: 連鎖削除はkeep指定に優先するが、黙って上書きしない。
    // keep_overridden(サーバが現在の操作リストから算出)を警告として明示する。
    const keepOverridden = preview.keep_overridden || [];
    let keepOverriddenHtml = "";
    if (keepOverridden.length > 0) {
      const shownKeep = keepOverridden.slice(0, 10);
      const keepOverflow = keepOverridden.length - shownKeep.length;
      const keepRowsHtml = shownKeep
        .map((c) => `<li>${escapeHtml(c.ifc_class)} ${escapeHtml(c.name ?? "(なし)")}</li>`)
        .join("");
      const keepOverflowHtml = keepOverflow > 0 ? `<li>他${keepOverflow}件</li>` : "";
      keepOverriddenHtml = `
        <div class="modal-warning">⚠ 残す指定を上書きして削除: ${keepOverridden.length}件</div>
        <ul>${keepRowsHtml}${keepOverflowHtml}</ul>
      `;
    }

    const bodyHtml = `
      <div>直接: ${preview.direct}件 / 連鎖: ${cascaded.length}件 / 合計: ${preview.total}件</div>
      <ul>${rowsHtml}${overflowHtml}</ul>
      ${keepOverriddenHtml}
    `;

    const confirmed = await showModal({
      title: "削除の確認",
      bodyHtml,
      actions: [
        { label: "キャンセル", value: false },
        { label: "確定", value: true, primary: true },
      ],
    });
    if (!confirmed) return;

    const operation = { op: "delete", targets, scope: "element", params: {} };
    // このプレビュー時点の連鎖gid(keep上書き分含む)を、対象要素自身を除いて記録する
    // (targets自身は通常のcurrentEffective経路で削除色が付くため重複させない)。
    cascadePreviewByOp.set(operation, cascaded.map((c) => c.global_id));
    operationList.add(operation);
    selectionModel.clear();
  } finally {
    opDeleteButton.disabled = false;
  }
}

/**
 * 「軽量化」ボタン: method/ratio選択→対象に共有要素があればscope確認→
 * Operation(simplify)追加。
 */
async function handleSimplifyClick() {
  const targets = [...selectionModel.selected];
  if (targets.length === 0) return;

  const method = opSimplifyMethod.value;
  const params = { method };
  if (method === "decimate") {
    const ratio = Number(opSimplifyRatio.value);
    if (!Number.isFinite(ratio) || ratio <= 0.05 - 1e-9 || ratio >= 0.95 + 1e-9) {
      showError("ratioは0.05〜0.95の範囲で入力してください。");
      return;
    }
    params.ratio = ratio;
  }

  opSimplifyButton.disabled = true;
  try {
    let scope = "element";
    let siblings = {};
    try {
      const result = await fetchSharingBatch(targets);
      const counts = result.counts || {};
      siblings = result.siblings || {};
      const maxShared = Object.values(counts).reduce((max, c) => Math.max(max, c), 0);
      if (maxShared > 1) {
        scope = await showModal({
          title: "共有形状の確認",
          bodyHtml: `<div>この形状は他要素と共有されています(共有${maxShared}要素)。波及範囲を選んでください。</div>`,
          actions: [
            { label: "この要素のみ", value: "element" },
            { label: "共有要素に波及", value: "shared", primary: true },
          ],
        });
      }
    } catch (err) {
      showError(String(err.message || err));
      return;
    }

    const operation = { op: "simplify", targets, scope, params };

    // Phase4 Task4 §3: scope="shared"確定時、export時に実際に一緒に変わる
    // 兄弟要素(自身targetsは除く)を着色対象として記録する。onChange
    // (applyOperationColors)より前に登録しておく必要がある。
    if (scope === "shared") {
      const targetSet = new Set(targets);
      const siblingSet = new Set();
      for (const gid of targets) {
        for (const sib of siblings[gid] || []) {
          if (!targetSet.has(sib)) siblingSet.add(sib);
        }
      }
      if (siblingSet.size > 0) sharedSiblingsByOp.set(operation, [...siblingSet]);
    }

    operationList.add(operation);
    selectionModel.clear();
  } finally {
    opSimplifyButton.disabled = false;
  }
}

/** 「残す」ボタン: 選択要素にOperation(keep)を追加する。 */
function handleKeepClick() {
  const targets = [...selectionModel.selected];
  if (targets.length === 0) return;
  opKeepButton.disabled = true;
  try {
    operationList.add({ op: "keep", targets, scope: "element", params: {} });
    selectionModel.clear();
  } finally {
    opKeepButton.disabled = false;
  }
}

/** 「出力」ボタン: POST /api/export→/api/statusをポーリングし結果を表示する。 */
async function handleExportClick() {
  const outputPath = exportPathInput.value.trim();
  if (!outputPath) {
    showError("出力パスを入力してください。");
    return;
  }
  clearError();
  exportButton.disabled = true;
  exportResult.innerHTML = "";

  try {
    await startExport(outputPath, exportConsolidateCheckbox.checked);
  } catch (err) {
    showError(String(err.message || err));
    exportButton.disabled = false;
    return;
  }

  const handle = setInterval(async () => {
    let status;
    try {
      status = await pollStatus();
    } catch (err) {
      clearInterval(handle);
      exportButton.disabled = false;
      showError(String(err.message || err));
      return;
    }

    if (status.state === "exporting") return;

    clearInterval(handle);
    exportButton.disabled = false;

    if (status.state === "error") {
      showError(status.message || "出力に失敗しました。");
      return;
    }
    if (status.export_result) {
      renderExportResult(status.export_result);
    }
    if (status.state === "ready") {
      resyncOpsIfPending();
    }
  }, 500);
}

function renderExportResult(result) {
  const warnings = result.warnings || [];
  const warningsHtml =
    warnings.length > 0
      ? `<div>警告: ${warnings.map((w) => escapeHtml(w)).join(" / ")}</div>`
      : "";
  const consolidatedHtml =
    result.consolidated_groups != null
      ? `<div>共有化: ${result.consolidated_groups}群 / ${result.consolidated_elements}要素</div>`
      : "";
  exportResult.innerHTML = `
    <div>削除: ${result.deleted}件 / 軽量化: ${result.simplified}件 / スキップ: ${result.skipped}件</div>
    ${consolidatedHtml}
    <div>出力先: ${escapeHtml(result.output_path)}</div>
    ${warningsHtml}
  `;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

loadButton.addEventListener("click", handleLoadClick);
clearButton.addEventListener("click", () => {
  clearSelectionNotice();
  selectionModel.clear();
});
selectionModel.onChange(renderSelection);

operationList.onChange((operations) => {
  currentEffective = resolveEffective(operations);
  applyOperationColors();
  renderOpsPanel(operations);
  scheduleOpsSync(operations);
});

opDeleteButton.addEventListener("click", handleDeleteClick);
opSimplifyButton.addEventListener("click", handleSimplifyClick);
opKeepButton.addEventListener("click", handleKeepClick);
opsClearButton.addEventListener("click", () => {
  cascadePreviewByOp.clear();
  sharedSiblingsByOp.clear();
  operationList.clear();
});
opSimplifyMethod.addEventListener("change", () => {
  opSimplifyRatio.style.display = opSimplifyMethod.value === "decimate" ? "inline-block" : "none";
});
exportButton.addEventListener("click", handleExportClick);
presetLoadSamplesButton.addEventListener("click", handleLoadSamplePresetsClick);
presetSaveButton.addEventListener("click", handleSavePresetFromCurrentClick);

loadPresetsPanel();

viewer.onTriangleClick((triIndex) => {
  if (!currentMeta) return;
  const el = triangleToElement(currentMeta.elements, triIndex);
  if (el) selectionModel.toggleElement(el.global_id);
});

if (new URLSearchParams(window.location.search).get("selftest") === "1") {
  runSelftest();
}

// デバッグ/検証用フック(ブラウザ内テストで内部状態を introspect するため)。
window.__debug = {
  viewer,
  selectionModel,
  operationList,
  getCurrentMeta: () => currentMeta,
  getGidToElement: () => gidToElement,
  getCurrentEffective: () => currentEffective,
  isOpsResyncPending: () => opsResyncPending,
};

/**
 * triangleToElement の境界ケースをconsole.assertで検証するブラウザ内self-test。
 * ?selftest=1 でのみ実行される。
 */
function runSelftest() {
  const elements = [
    { global_id: "A", tri_start: 0, tri_count: 10 },
    { global_id: "B", tri_start: 10, tri_count: 5 },
    { global_id: "C", tri_start: 15, tri_count: 20 },
  ];

  let passed = 0;
  let total = 0;

  function check(label, actualGid, expectedGid) {
    total++;
    const ok = actualGid === expectedGid;
    console.assert(ok, `[selftest] ${label}: expected ${expectedGid}, got ${actualGid}`);
    if (ok) passed++;
  }

  // 先頭要素の先頭三角形
  check("先頭要素の先頭", triangleToElement(elements, 0)?.global_id ?? null, "A");
  // 先頭要素の末尾三角形
  check("先頭要素の末尾", triangleToElement(elements, 9)?.global_id ?? null, "A");
  // 区間境界(Aの終わり/Bの始まり)
  check("区間境界(Bの先頭)", triangleToElement(elements, 10)?.global_id ?? null, "B");
  check("区間境界(Bの末尾)", triangleToElement(elements, 14)?.global_id ?? null, "B");
  check("区間境界(Cの先頭)", triangleToElement(elements, 15)?.global_id ?? null, "C");
  // 末尾要素の末尾三角形
  check("末尾要素の末尾", triangleToElement(elements, 34)?.global_id ?? null, "C");
  // 範囲外(負)
  check("範囲外(負)", triangleToElement(elements, -1)?.global_id ?? null, null);
  // 範囲外(超過)
  check("範囲外(超過)", triangleToElement(elements, 35)?.global_id ?? null, null);
  // 空配列
  check("空配列", triangleToElement([], 0)?.global_id ?? null, null);

  console.log(`[selftest] triangleToElement: ${passed}/${total} passed`);

  // --- SelectionModel.selectByGids の境界ケース ---
  // 未知gidは既知gid集合(elements)でフィルタして黙って除外する方式(ドキュメント済み)。
  const sm = new SelectionModel();

  function checkSelected(label, actualSet, expectedGids) {
    total++;
    const expected = new Set(expectedGids);
    const ok =
      actualSet.size === expected.size &&
      [...actualSet].every((g) => expected.has(g));
    console.assert(
      ok,
      `[selftest] ${label}: expected {${[...expected]}}, got {${[...actualSet]}}`
    );
    if (ok) passed++;
  }

  // 空リスト -> 選択は空になる
  sm.selectByGids([], elements);
  checkSelected("selectByGids(空リスト)", sm.selected, []);

  // 既知gidのみ -> そのまま選択される
  sm.selectByGids(["A", "C"], elements);
  checkSelected("selectByGids(既知gidのみ)", sm.selected, ["A", "C"]);

  // 既知+未知混在 -> 未知は除外され既知のみ残る
  sm.selectByGids(["A", "UNKNOWN", "B"], elements);
  checkSelected("selectByGids(既知+未知混在)", sm.selected, ["A", "B"]);

  // 未知gidのみ -> 選択は空になる
  sm.selectByGids(["UNKNOWN1", "UNKNOWN2"], elements);
  checkSelected("selectByGids(未知gidのみ)", sm.selected, []);

  console.log(`[selftest] selectByGids: 4件の境界ケースを含む合計 ${passed}/${total} passed`);
}
