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
  deletePreset,
  resolvePreset,
  fetchFileList,
  fetchConfig,
} from "./api.js";
import {
  initViewer,
  shouldOutline,
  isClickNotDrag,
  shouldDim,
  resolveBaseColor,
  ifcColorToLinear,
  dimColor,
  COLOR_MODE_IFC,
  COLOR_MODE_CLASS,
} from "./viewer.js";
import { SelectionModel, triangleToElement } from "./selection.js";
import { OperationList, resolveEffective } from "./operations.js";
import { renderDataTable, readClickMode } from "./datatable.js";
import {
  openFileDialog,
  isSameRootRelativePath,
  SAME_AS_SOURCE_MESSAGE,
} from "./filedialog.js";
import { estimateLoad, formatDuration, formatBytes } from "./estimate.js";
import { roundToNiceStep, buildStage } from "./stage.js";
import * as THREE from "./vendor/three.module.js"; // ?selftest=1でbuildStageにTHREE.Box3を渡すためだけに使う。
import { measureRenderedColors } from "./rendercheck.js";
import {
  resolveDragAction,
  clampPolarDelta,
  orbitAroundPivot,
  worldUnitsPerPixel,
} from "./camera-math.js";

const canvas = document.getElementById("canvas");
const filePickButton = document.getElementById("file-pick-button");
const selectedPathDisplay = document.getElementById("selected-path");
const manualPathToggle = document.getElementById("manual-path-toggle");
const manualPathRow = document.getElementById("manual-path-row");
const pathInput = document.getElementById("path-input");
const loadButton = document.getElementById("load-button");
const statusLine = document.getElementById("status-line");
const loadEstimateEl = document.getElementById("load-estimate");
const errorBanner = document.getElementById("error-banner");
const diagnosticsSummary = document.getElementById("diagnostics-summary");
const classTableWrap = document.getElementById("class-table-wrap");
const layerList = document.getElementById("layer-list");
const layerlessNote = document.getElementById("layerless-note");
const selectionChipLabel = document.getElementById("selection-chip-label");
const selectionChipClearButton = document.getElementById("selection-chip-clear-button");
const selectionChipOutlineNote = document.getElementById("selection-chip-outline-note");
const infoTitle = document.getElementById("info-title");
const infoPanel = document.getElementById("info-panel");
const selectionNotice = document.getElementById("selection-notice");
const duplicatesList = document.getElementById("duplicates-list");
const duplicatesNote = document.getElementById("duplicates-note");
const opsControlsTitle = document.getElementById("ops-controls-title");
const opsControls = document.getElementById("ops-controls");
const opDeleteButton = document.getElementById("op-delete-button");
const opSimplifyMethod = document.getElementById("op-simplify-method");
const opSimplifyRatio = document.getElementById("op-simplify-ratio");
const opSimplifyButton = document.getElementById("op-simplify-button");
const simplifyDesc = document.getElementById("simplify-desc");
const simplifyRatioDesc = document.getElementById("simplify-ratio-desc");
const simplifyEstimate = document.getElementById("simplify-estimate");
const opKeepButton = document.getElementById("op-keep-button");
const opsPanelTitle = document.getElementById("ops-panel-title");
const opsPanelList = document.getElementById("ops-panel-list");
const opsClearButton = document.getElementById("ops-clear-button");
const exportPathPickButton = document.getElementById("export-path-pick-button");
const exportSelectedPathDisplay = document.getElementById("export-selected-path");
const exportManualPathToggle = document.getElementById("export-manual-path-toggle");
const exportManualPathRow = document.getElementById("export-manual-path-row");
const exportPathInput = document.getElementById("export-path-input");
const exportPathError = document.getElementById("export-path-error");
const exportConsolidateCheckbox = document.getElementById("export-consolidate-checkbox");
const exportButton = document.getElementById("export-button");
const exportStatusLine = document.getElementById("export-status-line");
const exportResult = document.getElementById("export-result");
const presetList = document.getElementById("preset-list");
const presetEmptyNotice = document.getElementById("preset-empty-notice");
const presetLoadSamplesButton = document.getElementById("preset-load-samples-button");
const presetSaveButton = document.getElementById("preset-save-button");
const sidebarEl = document.getElementById("sidebar");
const sidebarResizer = document.getElementById("sidebar-resizer");
const sidebarToggle = document.getElementById("sidebar-toggle");
const stageToggleButton = document.getElementById("stage-toggle-button");
const colorModeSelect = document.getElementById("color-mode-select");
const listTabButtons = {
  class: document.getElementById("tab-class"),
  layer: document.getElementById("tab-layer"),
  duplicate: document.getElementById("tab-duplicate"),
};
const listPanels = {
  class: document.getElementById("panel-class"),
  layer: document.getElementById("panel-layer"),
  duplicate: document.getElementById("panel-duplicate"),
};

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
// 配色モード(色task Task5): COLOR_MODE_IFC(既定)/COLOR_MODE_CLASS。値そのものは
// COLOR_MODE_IFCで初期化しておき、起動シーケンス終盤のrestoreColorMode()
// (サイドバー幅と同じ「宣言はここ、localStorageからの復元呼び出しは起動
// シーケンス末尾」の構成)がlocalStorageの値で上書きする。
let colorModeValue = COLOR_MODE_IFC;

/** 現在の配色モードを返す。viewer.repaintColors/viewer.setMeshの呼び出し全箇所が
 *  これを渡す(1箇所でも渡し忘れると、その経路だけモード切替が効かなくなる)。
 * @returns {string}
 */
function currentColorMode() {
  return colorModeValue;
}
let selectionNoticeTimer = null;
// クラス/レイヤーの行ハイライトをO(全要素)全走査せずに判定するための前計算索引。
// onModelReadyで構築し、renderSelectionでは各クラス/レイヤー自身の要素数分だけを見る。
let classIndex = new Map(); // ifc_class -> Set(global_id)
let layerIndex = new Map(); // layer -> Set(global_id)
// 操作リストの有効操作(resolve_effective相当)。GUI改修Task8:
// repaintViewerColorsが選択変更・操作変更のどちらからも常にこの最新値を読み、
// viewer.repaintColorsで全要素を1パスで塗り直す(差分反映方式は廃止した)。
let currentEffective = new Map(); // global_id -> operation
// Final Review Fix2 (frontend): 確定した削除操作(operation)ごとに、そのプレビュー
// 時点で連鎖していたgid集合(keep上書き分含む)を保持する。closureの子(開口・充填要素)
// はcurrentEffectiveに自分自身のOperationを持たない(operationのtargetsは直接対象の
// gidのみ)ため、currentEffective単体をそのままrepaintViewerColorsに渡しても
// 削除色にならない。これを補うための追加着色専用マップ(buildEffectiveOpsForPaint
// が読む)。key=operation object(参照)、value=string[](gid配列)。
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
// 出力実行中かどうか(GUI改修Task5)。setExportInFlightのみが変更し、出力系の
// 入力欄・ボタン群のdisabled状態と出力ボタンの最終ゲートを一箇所にまとめる。
let exportInFlight = false;

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
  filePickButton.disabled = isLoading;
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
  setTabCount("class", 0);
  statusLine.textContent = "読込を開始しています...";
  hideLoadEstimate();
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

// ---------------------------------------------------------------------------
// ファイル選択ダイアログと読込前の推定 (GUI改修 Task4)
// ---------------------------------------------------------------------------

let cachedConfig = null;

/** GET /api/config を初回のみ取得してキャッシュする(セッション中に値は変わらない)。 */
async function getConfig() {
  if (!cachedConfig) cachedConfig = await fetchConfig();
  return cachedConfig;
}

/** #selected-path を pathInput.value の現在値に同期する(未選択なら"(未選択)")。 */
function updateSelectedPathDisplay() {
  const value = pathInput.value.trim();
  selectedPathDisplay.textContent = value || "(未選択)";
}

function hideLoadEstimate() {
  loadEstimateEl.style.display = "none";
  loadEstimateEl.innerHTML = "";
}

/**
 * 読込前の推定(読込時間レンジ・推定メモリ)を表示する。文言はbrief verbatim。
 * warn===trueのときはCUI案内を警告色で追加表示する(監督者裁定4: 係数は
 * 実測2点からの目安であり、遅い実行環境で外れて見えても係数は調整しない)。
 * @param {number} bytes
 * @param {object} config GET /api/config の戻り値
 * @param {string} path 選択されたパス(CUI案内コマンドの引用に使う)
 */
function renderLoadEstimate(bytes, config, path) {
  const est = estimateLoad(bytes, config);
  const rangeText = `${formatDuration(est.secLow)}〜${formatDuration(est.secHigh)}`;
  const memText = formatBytes(est.memBytes);
  let html = `<div>推定読込時間 ${rangeText} / 推定メモリ 約${memText}(実測2点からの目安です)</div>`;
  if (est.warn) {
    const cuiCommand = `python -m ifc_occam cui "${path}" --scan-only`;
    const exeCommand = `ifc_occam.exe cui "${path}" --scan-only`;
    html += `<div class="load-estimate-warning">${escapeHtml(
      `このサイズはフルオープンに失敗する可能性があります。CUI なら開かずに診断できます: ${cuiCommand}(exe版: ${exeCommand})`
    )}</div>`;
  }
  loadEstimateEl.innerHTML = html;
  loadEstimateEl.style.display = "block";
}

/**
 * ファイル選択ダイアログで選ばれたパスの推定を表示する。サイズは
 * openFileDialogの戻り値(パス文字列のみ)に含まれないため、選択されたパスの
 * 親ディレクトリを再取得してentry.sizeを引く(/api/filesの再利用。新規
 * エンドポイントは増やさない)。推定は付加情報のため、取得に失敗しても
 * 読込導線自体は塞がない(エラーバナーは出さず、単に推定非表示のままにする)。
 * @param {string} path
 */
async function updateLoadEstimateForPath(path) {
  hideLoadEstimate();
  const idx = path.lastIndexOf("/");
  const dir = idx >= 0 ? path.slice(0, idx) : "";
  const name = idx >= 0 ? path.slice(idx + 1) : path;

  let listing;
  let config;
  try {
    [listing, config] = await Promise.all([fetchFileList(dir), getConfig()]);
  } catch (_err) {
    return;
  }
  const entry = (listing.entries || []).find((e) => e.name === name && !e.is_dir);
  if (!entry || entry.size == null) return;
  renderLoadEstimate(entry.size, config, path);
}

/** 「ファイル指定」ボタン: ダイアログを開き、選択されたら手打ち欄に反映して推定を出す。 */
async function handleFilePickClick() {
  const selected = await openFileDialog({ mode: "open", initialPath: "", suffixes: [".ifc"] });
  if (selected == null) return;
  pathInput.value = selected;
  updateSelectedPathDisplay();
  await updateLoadEstimateForPath(selected);
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
    updateExportSelectedPathDisplay();
    refreshExportPathValidity();
    gidToElement = new Map(currentMeta.elements.map((el) => [el.global_id, el]));
    buildSelectionIndexes(currentMeta.elements);
    selectionModel.clear();
    renderDiagnostics(diagnostics);
    renderLayers(diagnostics.layer_stats || [], diagnostics.layerless_element_count || 0);
    renderDuplicates(diagnostics.duplicate_groups || []);
    activateTab("class");
    viewer.setMesh(meshData.meta, meshData.positions, meshData.indices, currentColorMode());
    // 新規読込ではサーバ側の操作リストも空(state.set_readyでリセット済み)なので
    // フロントも追従させる(古いgidの色戻し試行はel未検出で黙って無視される)。
    exportResult.innerHTML = "";
    exportStatusLine.textContent = "";
    // cascadePreviewByOpはoperation object参照をキーにした追加着色マップ。
    // operationList.clear()のonChange->repaintViewerColors再描画で古い連鎖gidが
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
 * gids(Array/Set)の全要素がselectedに含まれるかを判定する(行のハイライト
 * 判定用)。行の要素数分だけ見れば済み、selectedやelements全体を走査しない。
 * @param {Iterable<string>} gids
 * @param {Set<string>} selected
 */
function isFullySelected(gids, selected) {
  let any = false;
  for (const gid of gids) {
    any = true;
    if (!selected.has(gid)) return false;
  }
  return any;
}

/**
 * container内の各行(datatable.jsがtr._rowに行データを持たせている)のハイライトを
 * 選択状態に合わせて更新する。クラス/レイヤー/重複形状の3パネルで共通に使う
 * (GUI改修Task2: 旧classIndex/layerIndex/duplicateRowsを別々に見ていた3つの
 * ループの統合)。並べ替え後の見た目の行順にも自然に追従する(tr._rowを見るため)。
 * @param {HTMLElement} container
 * @param {Set<string>} selected
 */
function refreshRowHighlight(container, selected) {
  for (const tr of container.querySelectorAll("tbody tr")) {
    const row = tr._row;
    if (!row) continue;
    tr.classList.toggle("selected-row", isFullySelected(row.gids, selected));
  }
}

// クイックボタンのtitle文言(GUI改修Task2 brief手順1: 行の種類に応じて呼び出し側が渡す)。
const QUICK_ACTION_TITLES = {
  class: {
    delete: "このクラスの全要素を削除対象にする",
    simplify: "このクラスの全要素を軽量化対象にする",
    keep: "このクラスの全要素を残す対象にする",
  },
  layer: {
    delete: "このレイヤーの全要素を削除対象にする",
    simplify: "このレイヤーの全要素を軽量化対象にする",
    keep: "このレイヤーの全要素を残す対象にする",
  },
  duplicate: {
    delete: "この重複形状グループの全要素を削除対象にする",
    simplify: "この重複形状グループの全要素を軽量化対象にする",
    keep: "この重複形状グループの全要素を残す対象にする",
  },
};

/**
 * @param {"class"|"layer"|"duplicate"} kind
 * @param {"delete"|"simplify"|"keep"} action
 * @returns {string|undefined}
 */
function quickActionTitle(kind, action) {
  return QUICK_ACTION_TITLES[kind]?.[action];
}

// 列定義(GUI改修Task2/3): クラス表/重複形状/レイヤーは列を固定する(brief要件7,6/Task3)。
const CLASS_COLUMNS = [
  { key: "ifc_class", label: "クラス", align: "left" },
  { key: "element_count", label: "要素数", align: "right" },
  { key: "unique_shape_count", label: "形状数", align: "right" },
  { key: "total_triangles", label: "三角形数", align: "right" },
  { key: "mapped_count", label: "共有経由", align: "right" },
  { key: "max_single_shape_triangles", label: "最大単体", align: "right" },
];

// GUI改修Task3: レイヤーもクラス別ランキングと同様にボリュームを見せる。
// ただしLayerStatsにはmapped_count/max_single_shape_triangles相当が無いため
// (共有形状の分析はレイヤー軸では意味が薄いという監督者裁定4)、4列止まり。
const LAYER_COLUMNS = [
  { key: "layer", label: "レイヤー", align: "left" },
  { key: "element_count", label: "要素数", align: "right" },
  { key: "unique_shape_count", label: "形状数", align: "right" },
  { key: "total_triangles", label: "三角形数", align: "right" },
];

const DUPLICATE_COLUMNS = [
  { key: "shape_count", label: "形状数", align: "right" },
  { key: "element_count", label: "要素数", align: "right" },
  { key: "triangle_count", label: "三角形数", align: "right" },
  { key: "savable_triangles", label: "節約可能", align: "right" },
];

/**
 * 一覧行クリックの共通処理(クラス/レイヤー/重複形状で共通)。修飾キーで
 * 置換/追加/除外を振り分ける(監督者裁定1: 置換=selectByGids、追加=addGids、
 * 除外=removeGids。行クリックの経路はgidsベースに統一されている。旧
 * selectByClass/selectByLayer(クラス名/レイヤー名で直接置き換える版)は
 * この統一により呼び出し元が無くなったため、Task 10で削除した)。
 * @param {{gids:string[]}} row
 * @param {{additive:boolean, subtractive:boolean}} mode
 */
function handleListRowClick(row, mode) {
  if (!currentMeta) return;
  if (mode.subtractive) {
    // 除外は「選択が0件になる」ことも正常な結果なので、空選択の通知は出さない。
    selectionModel.removeGids(row.gids);
    clearSelectionNotice();
    return;
  }
  if (mode.additive) {
    selectionModel.addGids(row.gids, currentMeta.elements);
  } else {
    selectionModel.selectByGids(row.gids, currentMeta.elements);
  }
  if (selectionModel.selected.size === 0) {
    showSelectionNotice("この分類に描画対象の要素はありません");
  } else {
    clearSelectionNotice();
  }
}

/**
 * 一覧行のクイック操作ボタン(削除/軽量化/残す)の共通処理。監督者裁定2,3:
 * 確認モーダルを飛ばさず既存のhandleDeleteClick/handleSimplifyClick/
 * handleKeepClickをそのまま経由させる。実行前にその行のgidsを選択状態にする
 * (置換。何に対する操作か3Dで見えるようにするため)。
 * @param {{gids:string[]}} row
 * @param {"delete"|"simplify"|"keep"} action
 */
async function handleRowQuickAction(row, action) {
  if (!currentMeta) return;
  selectionModel.selectByGids(row.gids, currentMeta.elements);
  if (selectionModel.selected.size === 0) {
    showSelectionNotice("この分類に描画対象の要素はありません");
    return;
  }
  clearSelectionNotice();
  if (action === "delete") {
    await handleDeleteClick();
  } else if (action === "simplify") {
    await handleSimplifyClick();
  } else if (action === "keep") {
    handleKeepClick();
  }
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

  // 色task Task5 手順4: IFC配色時にグレー表示になる(色情報を持たない)要素数を
  // 知らせる。0件なら何も表示しない(黙って効いているだけで十分)。
  const noColorCount = currentMeta ? currentMeta.elements.filter((el) => !el.color).length : 0;
  const noColorLine =
    noColorCount > 0
      ? `<div id="no-color-line">色情報なし: ${noColorCount}要素(IFCの色ではグレーで表示します)</div>`
      : "";

  diagnosticsSummary.innerHTML = `
    <div>スキーマ: ${escapeHtml(diagnostics.schema)}</div>
    <div>要素数: ${diagnostics.element_count}</div>
    <div>総三角形数: ${diagnostics.total_triangles}</div>
    <div>警告数: ${diagnostics.warnings.length}</div>
    ${excludedLine}
    ${noColorLine}
  `;

  const stats = diagnostics.class_stats || [];
  const rows = stats.map((s) => ({
    key: s.ifc_class,
    cells: {
      ifc_class: s.ifc_class,
      element_count: s.element_count,
      unique_shape_count: s.unique_shape_count,
      total_triangles: s.total_triangles,
      mapped_count: s.mapped_count,
      max_single_shape_triangles: s.max_single_shape_triangles,
    },
    gids: [...(classIndex.get(s.ifc_class) ?? [])],
  }));

  renderDataTable(classTableWrap, {
    columns: CLASS_COLUMNS,
    rows,
    defaultSort: { key: "total_triangles", dir: "desc" },
    onRowClick: handleListRowClick,
    onQuickAction: handleRowQuickAction,
    actionTitle: (row, action) => quickActionTitle("class", action),
    emptyMessage: "クラスがありません",
  });
  setTabCount("class", rows.length);
}

/**
 * レイヤー一覧を描画する(GUI改修Task3: クラス別ランキングと同様に要素数/形状数/
 * 三角形数のボリュームを見せる)。三角形数の降順で表示する(サーバの
 * aggregate_by_layerが既に降順で返すが、defaultSortでヘッダソートの初期状態も
 * renderDiagnosticsのクラス版と揃える)。
 * @param {Array<{layer:string, element_count:number, unique_shape_count:number, total_triangles:number}>} layerStats
 * @param {number} layerlessCount layerが未設定(null)の要素数。aggregate_by_layerは
 *   これらを結果から除外するため、レイヤー別集計の合計が全要素数に一致しない
 *   ことを「集計が壊れている」と誤解されないよう別途表示する(監督者裁定2)。
 *   0のときは注記を出さない。
 */
function renderLayers(layerStats, layerlessCount) {
  const stats = layerStats || [];
  const rows = stats.map((s) => ({
    key: s.layer,
    cells: {
      layer: s.layer,
      element_count: s.element_count,
      unique_shape_count: s.unique_shape_count,
      total_triangles: s.total_triangles,
    },
    gids: [...(layerIndex.get(s.layer) ?? [])],
  }));

  renderDataTable(layerList, {
    columns: LAYER_COLUMNS,
    rows,
    defaultSort: { key: "total_triangles", dir: "desc" },
    onRowClick: handleListRowClick,
    onQuickAction: handleRowQuickAction,
    actionTitle: (row, action) => quickActionTitle("layer", action),
    emptyMessage: "レイヤーがありません",
  });
  setTabCount("layer", rows.length);

  const count = layerlessCount || 0;
  layerlessNote.style.display = count > 0 ? "block" : "none";
  layerlessNote.textContent = count > 0 ? `レイヤー未設定: ${count}要素` : "";
}

const DUPLICATE_DISPLAY_CAP = 50;

/**
 * 重複形状グループ(diagnostics.duplicate_groups の1件)を一覧行データに変換する。
 * 形状数=shape_ids.length、要素数=element_gids(shape_id毎の要素gid配列のlist)を
 * 平坦化した件数(GUI改修Task2 要件R6: 旧「件数=N」表記は形状数/要素数の区別が
 * 付かないため廃止)。三角形数/節約可能はサーバ値をそのまま使う。
 * @param {{shape_ids:string[], triangle_count:number, savable_triangles:number, element_gids:string[][]}} group
 */
function duplicateRow(group) {
  const flatGids = group.element_gids.flat();
  return {
    key: group.shape_ids.join(","),
    cells: {
      shape_count: group.shape_ids.length,
      element_count: flatGids.length,
      triangle_count: group.triangle_count,
      savable_triangles: group.savable_triangles,
    },
    gids: flatGids,
  };
}

/**
 * 重複形状グループ一覧を描画する。/api/diagnostics の duplicate_groups。
 * 節約可能三角形数降順で上位50件までのみ表示する(この打ち切り基準自体は
 * ヘッダクリックで表示順を変えても変わらない。表示中の50件の並びだけが変わる)。
 * 件数が超える場合は注記を出す。
 * @param {Array<object>} groups diagnostics.duplicate_groups
 */
function renderDuplicates(groups) {
  const list = groups || [];
  setTabCount("duplicate", list.length);

  if (list.length === 0) {
    duplicatesNote.style.display = "none";
    renderDataTable(duplicatesList, {
      columns: DUPLICATE_COLUMNS,
      rows: [],
      emptyMessage: "重複形状がありません",
    });
    return;
  }

  const sortedBySavable = [...list].sort((a, b) => b.savable_triangles - a.savable_triangles);
  const capped = sortedBySavable.slice(0, DUPLICATE_DISPLAY_CAP);

  const truncated = list.length > DUPLICATE_DISPLAY_CAP;
  duplicatesNote.style.display = truncated ? "block" : "none";
  if (truncated) {
    duplicatesNote.textContent =
      `全 ${list.length} 件のうち、節約可能三角形数の多い上位50件を表示しています`;
  }

  renderDataTable(duplicatesList, {
    columns: DUPLICATE_COLUMNS,
    rows: capped.map(duplicateRow),
    defaultSort: { key: "savable_triangles", dir: "desc" },
    onRowClick: handleListRowClick,
    onQuickAction: handleRowQuickAction,
    actionTitle: (row, action) => quickActionTitle("duplicate", action),
    emptyMessage: "重複形状がありません",
  });
}

// 縁取り省略の通知文言(verbatim。監督者裁定5: 黙って効かないのは禁止)。
const OUTLINE_OMITTED_MESSAGE = "(縁取りは選択が大きいため省略)";

// cascadePreviewByOp/sharedSiblingsByOpをviewer.repaintColorsのeffectiveOpsへ
// 合成するためのプレースホルダop。statusColor(operations.js)はop種別しか
// 見ないため、これで十分(GUI改修Task8: 色の決定はviewer.repaintColors内に
// 一本化したため、ここではもうstatusColor自体を呼ばない)。
const DELETE_GHOST_OP = { op: "delete" };
const SIMPLIFY_GHOST_OP = { op: "simplify" };

/**
 * currentEffectiveへcascadePreviewByOp(連鎖削除プレビュー)とsharedSiblingsByOp
 * (共有波及プレビュー)のghost opを合成する。cascadeは無条件に上書きする
 * (このgidが自分自身のOperationを持つことは無い設計のため)。sharedSiblingsは
 * 自身の操作色を優先し、既にエントリがあれば上書きしない(旧applyOperationColors
 * の優先順位をそのまま維持——Final Review Fix2 / Phase4 Task4 §3)。
 * @returns {Map<string, object>}
 */
function buildEffectiveOpsForPaint() {
  const merged = new Map(currentEffective);
  for (const gids of cascadePreviewByOp.values()) {
    for (const gid of gids) merged.set(gid, DELETE_GHOST_OP);
  }
  for (const gids of sharedSiblingsByOp.values()) {
    for (const gid of gids) {
      if (!merged.has(gid)) merged.set(gid, SIMPLIFY_GHOST_OP);
    }
  }
  return merged;
}

/**
 * 選択・有効操作の現在値からviewer.repaintColorsを1回呼ぶ(GUI改修Task8)。
 * 「前回選択との差分だけ塗る」旧方式は廃止した(減光は選択0件/1件以上で
 * 全要素の明るさが変わるため、差分方式では成立しない)。選択が変わった時
 * (renderSelection)・操作リストが変わった時(operationList.onChange)・
 * 配色モードが変わった時(colorModeSelectのchange)のどの経路からも必ず
 * この関数を呼び、単一の描画ロジックに揃える(色task Task5: colorModeを
 * 渡し忘れるとモード切替がその経路だけ効かなくなるため、呼び出しはここ
 * 1箇所に集約している)。
 */
function repaintViewerColors() {
  const selected = selectionModel.selected;
  viewer.repaintColors({
    selectedGids: selected,
    effectiveOps: buildEffectiveOpsForPaint(),
    dim: shouldDim(selected.size),
    colorMode: currentColorMode(),
  });
}

/**
 * 選択要素の縁取りを更新する(GUI改修Task8)。選択0件ならviewer側で消すだけ。
 * 選択三角形数が上限(viewer.jsのMAX_OUTLINE_TRIANGLES)を超えて省略された
 * 場合は選択チップの横にverbatim文言を出す(監督者裁定5: 黙って効かないのは
 * 禁止)。
 * @param {Set<string>} selected
 */
function updateSelectionOutline(selected) {
  const elements = [...selected].map((gid) => gidToElement.get(gid)).filter(Boolean);
  const created = viewer.setSelectionOutline(elements);
  const omitted = elements.length > 0 && !created;
  selectionChipOutlineNote.style.display = omitted ? "inline" : "none";
  selectionChipOutlineNote.textContent = omitted ? OUTLINE_OMITTED_MESSAGE : "";
}

/**
 * 操作バー(#ops-dock)左端の選択チップを更新する(GUI改修Task8: 選択解除の
 * 経路4)。`選択中 {N}件`(×は選択1件以上でのみ表示)/ 0件は`選択なし`と
 * 薄く表示し×は出さない。既存の「選択をクリア」ボタン(#clear-button)は
 * このチップの×に統合して撤去した(index.html)。
 * @param {Set<string>} selected
 */
function updateSelectionChip(selected) {
  const n = selected.size;
  selectionChipLabel.textContent = n > 0 ? `選択中 ${n}件` : "選択なし";
  selectionChipLabel.classList.toggle("selection-chip-empty", n === 0);
  selectionChipClearButton.style.display = n > 0 ? "inline-block" : "none";
}

/**
 * 選択状態を3Dビューと行のハイライト・情報パネルに反映する。
 * @param {Set<string>} selected
 */
function renderSelection(selected) {
  repaintViewerColors();
  updateSelectionOutline(selected);
  updateSelectionChip(selected);

  opsControlsTitle.style.display = selected.size > 0 ? "block" : "none";
  opsControls.style.display = selected.size > 0 ? "block" : "none";

  refreshRowHighlight(classTableWrap, selected);
  refreshRowHighlight(layerList, selected);
  refreshRowHighlight(duplicatesList, selected);

  renderInfoPanel(selected);
  updateSimplifyExplanation();
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

    // Esc で閉じる。app.js の選択解除用 Esc ハンドラは `.modal-overlay` が
    // あると「モーダル側の Esc が優先」として自分を抑止するが、**その
    // モーダル側に Esc が無かった**ため、確認モーダルが出ている間は Esc が
    // 完全な無反応になっていた(フェーズ最終レビュー I-3)。閉じ方は
    // キャンセル相当(=非 primary の action があればその値、無ければ null)。
    const cancelAction = actions.find((a) => !a.primary);
    const closeValue = cancelAction ? cancelAction.value : null;
    const onKeyDown = (event) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      cleanup();
      resolve(closeValue);
    };
    function cleanup() {
      document.removeEventListener("keydown", onKeyDown, true);
      overlay.remove();
    }
    // capture フェーズで拾う。document 側の選択解除ハンドラより先に走らせ、
    // stopPropagation でそちらへ届かないようにするため(モーダルを閉じた
    // 拍子に選択まで消えるのを防ぐ)。
    document.addEventListener("keydown", onKeyDown, true);

    for (const action of actions) {
      const btn = document.createElement("button");
      btn.textContent = action.label;
      if (action.primary) btn.className = "modal-btn-primary";
      btn.addEventListener("click", () => {
        cleanup();
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
 * 操作パターン一覧を描画する。空なら「操作パターンがまだありません。」
 * を表示する(GUI改修Task6: 「サンプルを追加」ボタンはこの通知の外に出し、
 * 常時表示にした——空でなくてもサンプルを追加できるようにするため)。
 * 各行に「適用」と削除(×)の2ボタンを置く。
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
    const actionsDiv = document.createElement("div");
    actionsDiv.className = "preset-actions";

    const applyButton = document.createElement("button");
    applyButton.type = "button";
    applyButton.className = "preset-apply-button";
    applyButton.textContent = "適用";
    applyButton.addEventListener("click", () => handlePresetApplyClick(preset.name));

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "preset-delete-button";
    deleteButton.textContent = "×";
    deleteButton.title = `操作パターン「${preset.name}」を削除`;
    deleteButton.addEventListener("click", () => handlePresetDeleteClick(preset.name));

    actionsDiv.appendChild(applyButton);
    actionsDiv.appendChild(deleteButton);
    li.appendChild(infoDiv);
    li.appendChild(actionsDiv);
    presetList.appendChild(li);
  }
}

/**
 * プリセット行の削除(×): 確認モーダル(verbatim文言、監督者裁定4)→確定で
 * DELETE /api/presets(クエリパラメータ方式)→一覧再取得。GUI改修Task6。
 * @param {string} name
 */
async function handlePresetDeleteClick(name) {
  const confirmed = await showModal({
    title: "操作パターンの削除確認",
    bodyHtml: `<div>操作パターン「${escapeHtml(name)}」を削除しますか?</div>`,
    actions: [
      { label: "キャンセル", value: false },
      { label: "削除する", value: true, primary: true },
    ],
  });
  if (!confirmed) return;

  try {
    await deletePreset(name);
    await loadPresetsPanel();
  } catch (err) {
    showError(String(err.message || err));
  }
}

/**
 * 「サンプルを追加」: web/preset-samples.json を取得し、既存のプリセットへ
 * 追加する(GUI改修Task6監督者裁定6: 全置換ではない)。サンプルと同名の
 * プリセットが既にあれば上書き、無ければ追加する(他の既存プリセットは
 * 保持される)。ボタンは常時表示(一覧が空でなくても押せる)。
 */
async function handleLoadSamplePresetsClick() {
  presetLoadSamplesButton.disabled = true;
  try {
    const res = await fetch("./preset-samples.json");
    if (!res.ok) throw new Error(`preset-samples.json取得失敗 (${res.status})`);
    const samples = await res.json();
    const existing = await fetchPresets();
    const sampleNames = new Set(samples.map((s) => s.name));
    const merged = [...existing.filter((p) => !sampleNames.has(p.name)), ...samples];
    await postPresets(merged);
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
    title: `操作パターンを適用: ${name}`,
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
 *
 * GUI改修Task6監督者裁定5: 保存対象/保存できない操作の件数をverbatim文言で
 * 表示し、M>0(部分選択で対象外になった操作がある)でも保存自体は続行できる
 * ようにする(ここで止めない。ユーザーが承知の上で保存したい場合がある)。
 * M>0のときは対象外になった操作の要約(op/対象件数)も一覧で出す。
 * @param {number} savedCount 保存対象になるルール数(N)
 * @param {number} skippedCount 保存できない操作数(M)
 * @param {Array<{op:string, count:number}>} skippedSummaries 対象外になった操作の要約
 * @returns {Promise<{name:string, description:string}|null>}
 */
function showPresetSaveModal(savedCount, skippedCount, skippedSummaries) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const box = document.createElement("div");
    box.className = "modal-box";
    const skippedListHtml =
      skippedCount > 0
        ? `<ul>${skippedSummaries
            .map((s) => `<li>${escapeHtml(opLabel(s.op))} / ${s.count}件</li>`)
            .join("")}</ul>`
        : "";
    box.innerHTML = `
      <h3>操作パターンとして保存</h3>
      <div class="modal-body">
        <div id="preset-save-summary">保存対象: ${savedCount}件 / 保存できない操作: ${skippedCount}件(部分選択のため)</div>
        ${skippedListHtml}
        <label>名前<br><input id="preset-save-name-input" type="text" style="width:100%;"></label>
        <br><br>
        <label>説明<br><textarea id="preset-save-desc-input" style="width:100%;" rows="3"></textarea></label>
      </div>
    `;
    const actionsDiv = document.createElement("div");
    actionsDiv.className = "modal-actions";

    // Esc で閉じる(showModal と同じ理由。フェーズ最終レビュー I-3)。
    // 名前欄にフォーカスがあっても閉じてよい——このモーダルは「キャンセル」で
    // 失うものが入力途中の名前だけなので、Esc の一般的な期待どおりに振る舞う。
    const onKeyDown = (event) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      cleanup();
      resolve(null);
    };
    function cleanup() {
      document.removeEventListener("keydown", onKeyDown, true);
      overlay.remove();
    }
    document.addEventListener("keydown", onKeyDown, true);

    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "キャンセル";
    cancelBtn.addEventListener("click", () => {
      cleanup();
      resolve(null);
    });

    const saveBtn = document.createElement("button");
    saveBtn.textContent = "保存";
    saveBtn.className = "modal-btn-primary";
    saveBtn.addEventListener("click", () => {
      const name = box.querySelector("#preset-save-name-input").value.trim();
      const description = box.querySelector("#preset-save-desc-input").value.trim();
      cleanup();
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
 * ルール化しない)。GUI改修Task6監督者裁定5: スキップした操作はop/対象件数を
 * skippedSummariesに積み、保存モーダルで明示する(黙ってスキップしない)。
 * @param {Array<object>} operations
 * @param {Map<string, Set<string>>} classIndexMap
 * @param {Map<string, Set<string>>} layerIndexMap
 * @returns {{rules: Array<object>, skippedCount: number, skippedSummaries: Array<{op:string, count:number}>}}
 */
function deriveRulesFromOperations(operations, classIndexMap, layerIndexMap) {
  const rules = [];
  const skippedSummaries = [];

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
      skippedSummaries.push({ op: operation.op, count: operation.targets.length });
      continue;
    }
    rules.push({
      match,
      op: { op: operation.op, scope: operation.scope, params: operation.params },
    });
  }

  return { rules, skippedCount: skippedSummaries.length, skippedSummaries };
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
 * 変換できるものだけプリセット化する。保存モーダルに保存対象/保存できない
 * 操作の件数を出し(GUI改修Task6監督者裁定5)、対象外の操作があっても
 * (M>0)保存自体は続行できるようにする(ここでは止めない)。操作が1件も
 * 無い場合のみ「保存できる操作がありません」で止める(M=0かつN=0で
 * そもそも保存するものが無いケース。裁定5が対象とする「M>0でも止めない」
 * とは別の話)。
 */
async function handleSavePresetFromCurrentClick() {
  if (operationList.operations.length === 0) {
    showError("操作がありません。操作パターンにできる操作がありません。");
    return;
  }

  const { rules, skippedCount, skippedSummaries } = deriveRulesFromOperations(
    operationList.operations,
    classIndex,
    layerIndex
  );

  // 保存できるルールが1件も無いなら保存させない(Task 6 レビュー Minor の裁定)。
  // 裁定5の「M>0 でも止めるな」は「一部が対象外でも保存を続行させろ」の意味で
  // あって、ルール0件の空プリセットを作らせる意味ではない。空で保存すると
  // 適用しても何も起きない操作パターンが一覧に並び、ユーザーは理由が分からない。
  if (rules.length === 0) {
    showError(
      "保存できる操作がありません(すべて部分選択のため)。" +
        "クラスまたはレイヤー全体への操作を含めてください。"
    );
    return;
  }

  const saved = await showPresetSaveModal(rules.length, skippedCount, skippedSummaries);
  if (!saved) return;

  try {
    const existing = await fetchPresets();
    const updated = [
      ...existing.filter((p) => p.name !== saved.name),
      { name: saved.name, description: saved.description, rules },
    ];
    await postPresets(updated);
    await loadPresetsPanel();
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

// ---------------------------------------------------------------------------
// 簡略化手法の説明と効果の目安 — GUI改修 Task7
// ---------------------------------------------------------------------------

// 手法ごとの説明文(verbatim。brief/監督者裁定1により1文字も変えないこと)。
const SIMPLIFY_METHOD_DESCRIPTIONS = {
  bbox: "形を、それがすっぽり入る直方体1個に置き換えます。いちばん軽くなり、いちばん形が失われます。",
  convex_hull:
    "形の外側を輪ゴムで包んだような形に置き換えます。くぼみは埋まりますが、おおよその姿は残ります。",
  decimate:
    "三角形の数を減らして形を近似します。0.3 なら元の約30%まで減らします。数字が小さいほど軽く、粗くなります。",
};

// 凸包は事前に正確な三角形数を出せない(推測値を出さない — 監督者裁定2)。verbatim。
const CONVEX_HULL_ESTIMATE_UNAVAILABLE =
  "凸包後の三角形数は形状によって変わるため、事前には出せません。";

/**
 * 簡略化手法の効果の目安を計算する(副作用なし。DOMに触らない — 監督者裁定5)。
 * bbox: 要素あたり12三角形で確定している(ifc_occam/core/simplify.pyの実装。
 * 直方体=6面×2三角形)ため、afterは推測ではなく正確な数値として返せる
 * (監督者裁定2)。
 * decimate: round(triangles*ratio)。実際の削減率は形状によりズレるため、
 * "(目安)"の付記は呼び出し側の責務とする(この関数自体は数値のみ返す)。
 * convex_hull・未知の手法: 事前に正確な数を出せないため常にnullを返す
 * (推測値を出さない — 監督者裁定2)。
 * elementCountが0以下のときも常にnullを返す(監督者裁定3: 選択0件で
 * "約0→0三角形"のような無意味な表示を作らせないための判定をここに持つ)。
 * @param {{method:string, elementCount:number, triangles:number, ratio?:number}} params
 * @returns {{before:number, after:number}|null} 出せないときはnull。
 */
function estimateSimplified({ method, elementCount, triangles, ratio }) {
  if (!(elementCount > 0)) return null;
  if (method === "bbox") {
    return { before: triangles, after: elementCount * 12 };
  }
  if (method === "decimate") {
    // ratio欄が空文字の一時的な入力中はNumber("")===0(NaNではない)になる。
    // 0以下を素通しすると"約T→0三角形(目安)"という無意味な数字が一瞬出る
    // ため、有限かつ正の値のときだけ計算する(厳密な0.05〜0.95の範囲検証は
    // 確定操作であるhandleSimplifyClick側の責務のまま変えない)。
    if (!Number.isFinite(ratio) || ratio <= 0) return null;
    return { before: triangles, after: Math.round(triangles * ratio) };
  }
  return null; // convex_hull および未知の手法
}

/**
 * #simplify-desc(手法説明)・#simplify-ratio-desc(ratio欄の説明)・
 * #simplify-estimate(効果の目安)を、現在の選択・手法・ratioに合わせて
 * 更新する。選択・手法・ratioのいずれかが変わったら必ず呼ぶこと
 * (監督者裁定7)。DOMを読み書きする側であり、estimateSimplified自体は
 * 呼ぶだけで計算ロジックは持たない。
 */
function updateSimplifyExplanation() {
  const method = opSimplifyMethod.value;
  simplifyDesc.textContent = SIMPLIFY_METHOD_DESCRIPTIONS[method] ?? "";
  simplifyRatioDesc.style.display = method === "decimate" ? "block" : "none";

  const selected = selectionModel.selected;
  if (selected.size === 0) {
    // 監督者裁定3: 選択0件のときは効果の目安を出さない(説明文だけ出す)。
    simplifyEstimate.textContent = "";
    return;
  }

  if (method === "convex_hull") {
    // 監督者裁定2: 凸包は数字を出さない(推測値をでっち上げない)。
    simplifyEstimate.textContent = CONVEX_HULL_ESTIMATE_UNAVAILABLE;
    return;
  }

  let triangles = 0;
  for (const gid of selected) {
    const el = gidToElement.get(gid);
    if (el) triangles += el.tri_count;
  }
  const ratio = Number(opSimplifyRatio.value);
  const result = estimateSimplified({ method, elementCount: selected.size, triangles, ratio });
  if (!result) {
    simplifyEstimate.textContent = "";
    return;
  }
  const suffix = method === "decimate" ? "(目安)" : "";
  simplifyEstimate.textContent = `約 ${result.before} → ${result.after} 三角形${suffix}`;
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
    // (repaintViewerColors)より前に登録しておく必要がある。
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

// ---------------------------------------------------------------------------
// 出力先の指定と検証 (GUI改修 Task5)
// ---------------------------------------------------------------------------

// 文言の出典は filedialog.js の1箇所に集約する(保存ダイアログの入力欄と
// この手打ち欄の両方で同じ文字列を出すため。リテラルを各所に置くと片方だけ
// 直されて食い違う——Task 5 レビュー Minor の引き取り)。
const EXPORT_SAME_FILE_MESSAGE = SAME_AS_SOURCE_MESSAGE;

/** #export-selected-path を exportPathInput.value の現在値に同期する。 */
function updateExportSelectedPathDisplay() {
  const value = exportPathInput.value.trim();
  exportSelectedPathDisplay.textContent = value || "(未選択)";
}

/**
 * 出力先が読込中のファイルと同一実体を指しているかを判定する
 * (監督者裁定1・2)。判定の実体はfiledialog.jsのisSameRootRelativePath
 * (表記ゆれ・大文字小文字違いを吸収する正規化比較)——ダイアログの一覧行の
 * 無効化(filedialog.js)と手打ち欄のこの事前チェックが同じ関数を共有する
 * ことで、経路によって判定基準がずれることを防ぐ。あくまでUI側の事前チェック
 * であり、サーバ側 core/paths.refers_to_same_file(apply_operations内、
 * os.path.samefile経由)が最終防衛線として別途効く。
 * @returns {boolean}
 */
function isExportPathInvalid() {
  return isSameRootRelativePath(lastLoadedPath, exportPathInput.value.trim());
}

/** exportInFlight/パス検証の結果に応じて出力ボタンの有効/無効を一箇所で決める。 */
function updateExportButtonDisabled() {
  exportButton.disabled = exportInFlight || isExportPathInvalid();
}

/**
 * 出力先の同一ファイル判定を再評価し、赤字表示とボタンの有効/無効を更新する
 * (監督者裁定2: 出力ボタンの押下前に読込元と突き合わせる)。手打ち欄の
 * inputイベントと保存ダイアログでの選択後、モデル読込完了時に呼ぶ。
 * @returns {boolean} 判定結果(trueなら出力先が読込中のファイルと同一)
 */
function refreshExportPathValidity() {
  const invalid = isExportPathInvalid();
  exportPathError.style.display = invalid ? "block" : "none";
  exportPathError.textContent = invalid ? EXPORT_SAME_FILE_MESSAGE : "";
  updateExportButtonDisabled();
  return invalid;
}

/**
 * 出力実行中の入出力制御を一箇所にまとめる(読込側のsetLoadingUIと同じ流儀)。
 * @param {boolean} inFlight
 */
function setExportInFlight(inFlight) {
  exportInFlight = inFlight;
  exportPathPickButton.disabled = inFlight;
  exportManualPathToggle.disabled = inFlight;
  exportPathInput.disabled = inFlight;
  exportConsolidateCheckbox.disabled = inFlight;
  updateExportButtonDisabled();
}

/** 「保存先を指定」ボタン: 保存ダイアログを開き、選択されたら手打ち欄に反映する。 */
async function handleExportPathPickClick() {
  const selected = await openFileDialog({
    mode: "save",
    initialPath: exportPathInput.value.trim(),
    suffixes: [".ifc"],
    excludePath: lastLoadedPath,
  });
  if (selected == null) return;
  exportPathInput.value = selected;
  updateExportSelectedPathDisplay();
  refreshExportPathValidity();
}

/**
 * 出力先パス(root相対、ディレクトリ部分を含んでよい)の実体が既に存在するかを
 * /api/filesの一覧で調べる(監督者裁定3: 新しいAPIは増やさない)。ディレクトリの
 * 一覧取得に失敗した場合(root外・権限エラー等、通常は起こらない)は
 * 「存在しない」側として扱う——上書き確認を出せないだけで、実際の書き込みの
 * 安全性(原本非破壊)は/api/export側のサーバ判定に委ねられており損なわれない。
 * ファイル名の比較は大文字小文字を区別しない(Windowsのファイルシステムに合わせる)。
 * @param {string} path
 * @returns {Promise<boolean>}
 */
async function exportTargetExists(path) {
  const idx = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  const dir = idx >= 0 ? path.slice(0, idx) : "";
  const name = idx >= 0 ? path.slice(idx + 1) : path;
  let listing;
  try {
    listing = await fetchFileList(dir);
  } catch (_err) {
    return false;
  }
  const lowerName = name.toLowerCase();
  return (listing.entries || []).some((e) => !e.is_dir && e.name.toLowerCase() === lowerName);
}

// ifc_occam/cui/repl.py の _STAGE_SECONDS_LABELS と同じ日本語(監督者裁定5)。
// キーはexport.apply_operationsが実際に設定する7種で固定
// (open/deletes/simplify/reextract_duplicates/consolidate/write/gc。
// フェーズ最終レビューI3: "gc" ステージ追加時にここへの追記が漏れていた)。
// UI文言(語彙)の共有であり、estimate.jsの数値係数のような二重管理禁止の
// 対象ではない(opLabelがcore/ops.pyの操作名と同じ語彙を独自に持つのと同じ扱い)。
const STAGE_SECONDS_LABELS = {
  open: "開く",
  deletes: "削除",
  simplify: "簡略化",
  reextract_duplicates: "重複再抽出",
  consolidate: "重複統合",
  write: "書き込み",
  gc: "ゴミ回収",
};

/**
 * export_result.stage_seconds から表示用の行データ配列を作る(監督者裁定5)。
 * 0秒以下のステージ(consolidate=falseのときexport.apply_operationsが
 * reextract_duplicates/consolidateに明示的に0.0を入れる、等)は実行されていない
 * ため意味の無い行になり、除外する。未知キーが来てもラベルが無いだけで
 * 落ちない(そのままキー文字列を表示する)。
 * @param {Record<string, number>|undefined} stageSeconds
 * @returns {Array<{key:string, label:string, seconds:number}>}
 */
function formatStageSeconds(stageSeconds) {
  if (!stageSeconds) return [];
  return Object.entries(stageSeconds)
    .filter(([, seconds]) => seconds > 0)
    .map(([key, seconds]) => ({ key, label: STAGE_SECONDS_LABELS[key] ?? key, seconds }));
}

/**
 * export_result から表示すべき先頭行の文言を選ぶ(GUI改修Task5、既存バグ修正の
 * 核)。result.errorがあれば失敗文言(サーバの拒否理由をそのまま含む、verbatim:
 * 「出力に失敗しました: {error}」)を返す。無ければ既存の件数表示文言を返す。
 * renderExportResultがこれを先頭行としてそのまま使う(brief手順6の
 * ?selftest=1アサーション対象——サーバ契約は変えないため、この分岐自体を
 * フロントのテストで固定する)。
 * @param {{error?:string, deleted?:*, simplified?:*, skipped?:*}} result
 * @returns {string}
 */
function pickExportMessage(result) {
  if (result.error) return `出力に失敗しました: ${result.error}`;
  return `削除: ${result.deleted}件 / 軽量化: ${result.simplified}件 / スキップ: ${result.skipped}件`;
}

/**
 * 「出力」ボタン: 同名/既存チェック→POST /api/export→/api/statusをポーリングし
 * 結果を表示する。
 */
async function handleExportClick() {
  const outputPath = exportPathInput.value.trim();
  if (!outputPath) {
    showError("出力パスを入力してください。");
    return;
  }
  // 監督者裁定2: 手打ち欄はinputイベントで反応的に赤字/ボタン無効化されている
  // はずだが、押下時にも同じ判定でもう一度止める(防御的二重チェック。
  // 最終防衛線はサーバ側 core/export.apply_operations の refers_to_same_file)。
  if (refreshExportPathValidity()) return;
  clearError();

  // 監督者裁定3: 既存ファイルへの出力は上書き確認を挟む。新しいAPIは増やさず
  // /api/filesの一覧で存在を判定する(手打ち・ダイアログ選択のどちらでも同じ
  // ここ1箇所で判定する)。
  const exists = await exportTargetExists(outputPath);
  if (exists) {
    const fileName = outputPath.split(/[/\\]/).pop() || outputPath;
    const confirmed = await showModal({
      title: "上書きの確認",
      bodyHtml: `<div>${escapeHtml(fileName)} は既に存在します。上書きしますか?</div>`,
      actions: [
        { label: "キャンセル", value: false },
        { label: "上書きする", value: true, primary: true },
      ],
    });
    if (!confirmed) return;
  }

  setExportInFlight(true);
  exportResult.innerHTML = "";
  const startedAt = performance.now();
  exportStatusLine.textContent = "出力中... (0.0秒)";

  try {
    await startExport(outputPath, exportConsolidateCheckbox.checked);
  } catch (err) {
    showError(String(err.message || err));
    setExportInFlight(false);
    exportStatusLine.textContent = "";
    return;
  }

  const handle = setInterval(async () => {
    let status;
    try {
      status = await pollStatus();
    } catch (err) {
      clearInterval(handle);
      setExportInFlight(false);
      exportStatusLine.textContent = "";
      showError(String(err.message || err));
      return;
    }

    if (status.state === "exporting") {
      // /api/statusのelapsed_secはload専用のタイマー(_load_started_at)なので
      // export進捗には使えない(exporting中もload時点からの経過秒を返し続ける)。
      // ここはクライアント側のperformance.nowで経過秒を測る(読込時と同じ
      // 「実行中...(X.X秒)」の流儀を、サーバを変更せずに実現する)。
      const elapsed = (performance.now() - startedAt) / 1000;
      exportStatusLine.textContent = `出力中... (${elapsed.toFixed(1)}秒)`;
      return;
    }

    clearInterval(handle);
    setExportInFlight(false);

    if (status.state === "error") {
      exportStatusLine.textContent = "";
      showError(status.message || "出力に失敗しました。");
      return;
    }
    if (status.export_result) {
      const elapsed = (performance.now() - startedAt) / 1000;
      exportStatusLine.textContent = status.export_result.error
        ? ""
        : `出力完了 (${elapsed.toFixed(1)}秒)`;
      renderExportResult(status.export_result);
    } else {
      exportStatusLine.textContent = "";
    }
    if (status.state === "ready") {
      resyncOpsIfPending();
    }
  }, 500);
}

/**
 * 出力結果パネルを描画する。result.errorがあれば失敗表示のみを行い、それ以外は
 * 既存の件数/共有化/出力先/ステージ別秒数/警告を表示する(GUI改修Task5、既存
 * バグ修正: 従来はresult.errorを見ずにresult.deleted等を読んでいたため、失敗時
 * に「削除: undefined件」等しか表示されず理由が伝わらなかった)。
 * @param {object} result status.export_result
 */
function renderExportResult(result) {
  if (result.error) {
    const message = pickExportMessage(result);
    exportResult.innerHTML = `<div class="rule-error">${escapeHtml(message)}</div>`;
    showError(message);
    return;
  }

  const warnings = result.warnings || [];
  const warningsHtml =
    warnings.length > 0
      ? `<div>警告: ${warnings.map((w) => escapeHtml(w)).join(" / ")}</div>`
      : "";
  const consolidatedHtml =
    result.consolidated_groups != null
      ? `<div>共有化: ${result.consolidated_groups}群 / ${result.consolidated_elements}要素</div>`
      : "";
  const stageRows = formatStageSeconds(result.stage_seconds);
  const stageHtml =
    stageRows.length > 0
      ? `<div class="export-stage-seconds">${stageRows
          .map((row) => `${escapeHtml(row.label)}: ${row.seconds.toFixed(1)}秒`)
          .join(" / ")}</div>`
      : "";
  exportResult.innerHTML = `
    <div>${escapeHtml(pickExportMessage(result))}</div>
    ${consolidatedHtml}
    <div>出力先: ${escapeHtml(result.output_path)}</div>
    ${stageHtml}
    ${warningsHtml}
  `;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// サイドバー骨格(一覧タブ・幅ドラッグ・折りたたみ) — GUI改修 Task1
// ---------------------------------------------------------------------------

const TAB_LABELS = { class: "クラス", layer: "レイヤー", duplicate: "重複形状" };

/**
 * 一覧タブを切り替える(タブの選択状態とパネル表示を同期する)。disabled
 * かどうかはここでは見ない(disabledなタブはクリックイベント自体が来ない。
 * 初期表示やモデル読込後の既定タブ選択など、プログラムからの呼び出しは許可する)。
 * @param {"class"|"layer"|"duplicate"} name
 */
export function activateTab(name) {
  for (const key of Object.keys(listTabButtons)) {
    const isActive = key === name;
    listTabButtons[key].classList.toggle("active-tab", isActive);
    // 見た目(class)と支援技術への通知(aria-selected)を必ず一緒に更新する。
    // 片方だけ更新するとスクリーンリーダーに嘘の状態が伝わる。
    listTabButtons[key].setAttribute("aria-selected", isActive ? "true" : "false");
    listPanels[key].classList.toggle("active-panel", isActive);
  }
}

/**
 * 一覧タブのラベルに件数を反映し、件数0ならdisabledにする
 * (旧: renderDiagnostics/renderLayers/renderDuplicatesがh2ごと隠していた
 * 挙動の置き換え。パネル内部の描画そのものは変えない)。
 * @param {"class"|"layer"|"duplicate"} name
 * @param {number} count
 */
function setTabCount(name, count) {
  const button = listTabButtons[name];
  button.textContent = `${TAB_LABELS[name]} ${count}`;
  button.disabled = count === 0;
}

for (const key of Object.keys(listTabButtons)) {
  listTabButtons[key].addEventListener("click", () => activateTab(key));
}

// --- サイドバー幅のドラッグリサイズ ---
const SIDEBAR_WIDTH_KEY = "ifcOccam.sidebarWidth";
const SIDEBAR_COLLAPSED_KEY = "ifcOccam.sidebarCollapsed";
const SIDEBAR_MIN_WIDTH = 280;

/**
 * ウィンドウ幅から決まるサイドバー幅の上限を返す(min(60vw, 900px))。
 * ビューポート幅を引数で受けるのはテストのため——`window.innerWidth` を
 * 内部で読むと、期待値も同じ関数から作るしかなくなり、係数 0.6 を書き換える
 * バグをテストが素通ししてしまう(Task 1 レビュー Important-2)。
 * @param {number} [viewportWidth=window.innerWidth]
 * @returns {number}
 */
function sidebarMaxWidth(viewportWidth = window.innerWidth) {
  return Math.min(viewportWidth * 0.6, 900);
}

/**
 * サイドバー幅を [280, min(60vw,900)] にクランプする。
 *
 * 上限が下限を下回る極端に狭いビューポート(概ね467px未満)では**下限を優先**
 * する。計画は下限280pxと上限min(60vw,900px)を無条件に両立させる書き方に
 * なっていて両者が矛盾する領域を定義していないが、サイドバーが280px未満に
 * 潰れて中身が読めなくなるより、はみ出してでも読める方がましと判断した
 * (Task 1 レビュー Minor-4 の引き取り)。
 * @param {number} px
 * @param {number} [viewportWidth=window.innerWidth]
 * @returns {number}
 */
function clampSidebarWidth(px, viewportWidth = window.innerWidth) {
  const max = Math.max(sidebarMaxWidth(viewportWidth), SIDEBAR_MIN_WIDTH);
  return Math.min(Math.max(px, SIDEBAR_MIN_WIDTH), max);
}

function applySidebarWidth(px) {
  document.documentElement.style.setProperty("--sidebar-width", `${px}px`);
}

/** 起動時にlocalStorageから幅を復元する(無ければCSS既定の340pxのまま)。
 *
 * localStorage が例外を投げる環境(プライベートモードやストレージ無効設定)でも
 * 落ちないようにする。ここはモジュールのトップレベルから呼ばれるため、例外が
 * 漏れると以降の初期化(ボタンの結線・window.__debug・selftest)が全部止まり、
 * ページが操作不能になる。restoreColorMode 側だけ守っても、先に走るこちらで
 * 死ぬので意味がなかった(フェーズ最終レビュー M-3)。
 */
function restoreSidebarWidth() {
  let saved;
  try {
    saved = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY));
  } catch (_err) {
    return; // 読めなければCSS既定のまま
  }
  if (Number.isFinite(saved) && saved > 0) {
    applySidebarWidth(clampSidebarWidth(saved));
  }
}

let sidebarResizing = false;

sidebarResizer.addEventListener("pointerdown", (evt) => {
  sidebarResizing = true;
  sidebarResizer.setPointerCapture(evt.pointerId);
  document.body.classList.add("sidebar-resizing");
  evt.preventDefault();
});

sidebarResizer.addEventListener("pointermove", (evt) => {
  if (!sidebarResizing) return;
  const rect = sidebarEl.getBoundingClientRect();
  applySidebarWidth(clampSidebarWidth(evt.clientX - rect.left));
});

function endSidebarResize() {
  if (!sidebarResizing) return;
  sidebarResizing = false;
  document.body.classList.remove("sidebar-resizing");
  const currentPx = parseFloat(document.documentElement.style.getPropertyValue("--sidebar-width"));
  if (Number.isFinite(currentPx)) {
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(Math.round(currentPx)));
  }
}
sidebarResizer.addEventListener("pointerup", endSidebarResize);
sidebarResizer.addEventListener("pointercancel", endSidebarResize);

// --- サイドバー折りたたみ ---
function applySidebarCollapsed(collapsed) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  sidebarToggle.textContent = collapsed ? "»" : "«";
}

function restoreSidebarCollapsed() {
  applySidebarCollapsed(localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1");
}

sidebarToggle.addEventListener("click", () => {
  const collapsed = !document.body.classList.contains("sidebar-collapsed");
  applySidebarCollapsed(collapsed);
  localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
});

// --- ステージ(床・グリッド・軸)表示トグル(GUI改修Task9 監督者裁定8) ---
// 既定は表示(index.htmlのaria-pressed="true"とviewer.js内のstageVisible既定値
// が一致しているため、初期化時に明示的な同期呼び出しは不要)。
stageToggleButton.addEventListener("click", () => {
  const nextVisible = stageToggleButton.getAttribute("aria-pressed") !== "true";
  viewer.setStageVisible(nextVisible);
  stageToggleButton.setAttribute("aria-pressed", String(nextVisible));
});

// --- 配色モード切替(色task Task5): 「IFCの色」/「クラス別」 ---
const COLOR_MODE_STORAGE_KEY = "ifc-occam.colorMode";

/**
 * 起動時にlocalStorageから配色モードを復元する(不正値/未設定はCOLOR_MODE_IFCのまま)。
 * localStorageへのアクセスが例外を投げる環境(プライベートブラウジング等でストレージが
 * 無効化されている場合等)でも、この関数の失敗でそれ以降の初期化(ボタン結線・
 * window.__debug・selftest)まで止まってはならないため、try/catchで包み既定値
 * (COLOR_MODE_IFC)のままフォールバックする(色task Task5 レビュー Minor)。
 */
function restoreColorMode() {
  try {
    const saved = localStorage.getItem(COLOR_MODE_STORAGE_KEY);
    if (saved === COLOR_MODE_IFC || saved === COLOR_MODE_CLASS) {
      colorModeValue = saved;
    }
  } catch (_err) {
    colorModeValue = COLOR_MODE_IFC;
  }
}

restoreColorMode();
colorModeSelect.value = colorModeValue; // 復元した値をセレクトの表示にも反映する。
colorModeSelect.addEventListener("change", () => {
  colorModeValue = colorModeSelect.value;
  localStorage.setItem(COLOR_MODE_STORAGE_KEY, colorModeValue);
  repaintViewerColors(); // 現在の選択・操作状態を保ったまま塗り直す(setMeshは呼ばない)。
});

restoreSidebarWidth();
restoreSidebarCollapsed();

loadButton.addEventListener("click", handleLoadClick);
filePickButton.addEventListener("click", handleFilePickClick);
manualPathToggle.addEventListener("click", () => {
  const expanded = manualPathRow.style.display !== "none";
  manualPathRow.style.display = expanded ? "none" : "block";
  manualPathToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
});
pathInput.addEventListener("input", () => {
  // 手打ちで直接編集された場合、ダイアログ選択に基づく推定はもはや根拠が無い
  // (サイズを取り直していない)ため、いったん隠す。
  updateSelectedPathDisplay();
  hideLoadEstimate();
});
updateSelectedPathDisplay();

exportPathPickButton.addEventListener("click", handleExportPathPickClick);
exportManualPathToggle.addEventListener("click", () => {
  const expanded = exportManualPathRow.style.display !== "none";
  exportManualPathRow.style.display = expanded ? "none" : "block";
  exportManualPathToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
});
exportPathInput.addEventListener("input", () => {
  updateExportSelectedPathDisplay();
  refreshExportPathValidity();
});
updateExportSelectedPathDisplay();
refreshExportPathValidity();

selectionChipClearButton.addEventListener("click", () => {
  clearSelectionNotice();
  selectionModel.clear();
});
selectionModel.onChange(renderSelection);
renderSelection(selectionModel.selected); // 初期表示(チップ/縁取りの既定状態を確定させる)

// 選択解除の経路(GUI改修Task8監督者裁定、全4経路): (1)Escキー (2)3D背景クリック
// (3)選択済み要素の再クリック(既存のonTriangleClickのトグル挙動そのまま)
// (4)選択チップの×(上のselectionChipClearButton)。(1)(2)をここで結線する。
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  // モーダル側のEscが優先(モーダルを閉じる動作を横取りしない)。
  if (document.querySelector(".modal-overlay")) return;
  // 入力欄フォーカス時は無効(Escでのテキスト編集キャンセル等を横取りしない)。
  const activeTag = document.activeElement?.tagName;
  if (activeTag === "INPUT" || activeTag === "TEXTAREA") return;
  clearSelectionNotice();
  selectionModel.clear();
});

operationList.onChange((operations) => {
  currentEffective = resolveEffective(operations);
  repaintViewerColors();
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
  updateSimplifyExplanation();
});
opSimplifyRatio.addEventListener("input", updateSimplifyExplanation);
updateSimplifyExplanation(); // 初期表示(選択前でも手法説明欄を埋めておく)
exportButton.addEventListener("click", handleExportClick);
presetLoadSamplesButton.addEventListener("click", handleLoadSamplePresetsClick);
presetSaveButton.addEventListener("click", handleSavePresetFromCurrentClick);

loadPresetsPanel();

viewer.onTriangleClick((triIndex) => {
  if (!currentMeta) return;
  const el = triangleToElement(currentMeta.elements, triIndex);
  if (el) selectionModel.toggleElement(el.global_id);
});

// 選択解除の経路2「背景クリック」(GUI改修Task8): 何にも当たらないクリックで
// 全解除する。ドラッグ判定はviewer.js側(pointerdown/pointerup)で完了済みで、
// ここに来るのは常に「クリックと判定されたが要素には当たらなかった」場合のみ。
viewer.onBackgroundClick(() => {
  clearSelectionNotice();
  selectionModel.clear();
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
  // ブラウザタブが非表示扱いになりsetIntervalがスロットルされる環境(ヘッドレス
  // 自動操作等)向けに、pollOnceを手動で1回発火させる。本番の操作導線では
  // 使わない(startPollingの1秒間隔が唯一の呼び出し元)。
  forcePoll: () => pollOnce(),
  // 描画結果の実測(この環境ではスクリーンショットが撮れないための代替手段。
  // docs/testing-guide.md「オフスクリーン描画で色を確認する」を参照)。
  measureRender: (options) => measureRenderedColors(viewer.getScene(), THREE, options),
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

  // --- clampSidebarWidth の境界ケース(GUI改修 Task1 手順7) ---
  // ビューポート幅をリテラルで渡し、期待値もリテラルで書く。実装内部と同じ
  // 関数から期待値を作るとトートロジーになり、上限式の係数を書き換えるバグを
  // 素通しする(Task 1 レビュー Important-2 の引き取り)。
  check("clampSidebarWidth(下限未満)", clampSidebarWidth(100, 1600), 280);
  check("clampSidebarWidth(範囲内)", clampSidebarWidth(400, 1600), 400);
  check("clampSidebarWidth(上限=60vw側)", clampSidebarWidth(5000, 1000), 600);
  check("clampSidebarWidth(上限=900px定数側)", clampSidebarWidth(5000, 2000), 900);
  check("clampSidebarWidth(上限が下限を下回る狭幅は下限優先)", clampSidebarWidth(100, 400), 280);
  check("sidebarMaxWidth(60vw側)", sidebarMaxWidth(1000), 600);
  check("sidebarMaxWidth(900px定数側)", sidebarMaxWidth(2000), 900);

  console.log(`[selftest] clampSidebarWidth: 7件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- readClickMode の修飾キー解釈(GUI改修 Task2 手順7) ---
  check(
    "readClickMode(Shift=追加)",
    readClickMode({ shiftKey: true, ctrlKey: false, metaKey: false }),
    "additive"
  );
  check(
    "readClickMode(Ctrl=除外)",
    readClickMode({ shiftKey: false, ctrlKey: true, metaKey: false }),
    "subtractive"
  );
  check(
    "readClickMode(Cmd=除外)",
    readClickMode({ shiftKey: false, ctrlKey: false, metaKey: true }),
    "subtractive"
  );
  check("readClickMode(修飾なし=置換)", readClickMode({}), "replace");

  console.log(`[selftest] readClickMode: 4件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- SelectionModel.addGids/removeGids の境界ケース(GUI改修 Task2 裁定1) ---
  const sm2 = new SelectionModel();

  // 空集合への追加 -> 何も変わらない
  sm2.addGids([], elements);
  checkSelected("addGids(空集合への追加)", sm2.selected, []);

  sm2.selectByGids(["A"], elements);
  // 既に入っているgidの追加 -> 重複しない(Setなので個数は増えない)
  sm2.addGids(["A"], elements);
  checkSelected("addGids(既に入っているgidの追加)", sm2.selected, ["A"]);

  // 未知gid(elementsに存在しない)の追加は無視される
  sm2.addGids(["B", "UNKNOWN"], elements);
  checkSelected("addGids(未知gidの追加は無視される)", sm2.selected, ["A", "B"]);

  // 入っていないgidの除外 -> 壊れない(既存選択はそのまま)
  sm2.removeGids(["ZZZ"]);
  checkSelected("removeGids(入っていないgidの除外)", sm2.selected, ["A", "B"]);

  // 入っているgidの除外 -> そのgidだけ外れる
  sm2.removeGids(["A"]);
  checkSelected("removeGids(入っているgidの除外)", sm2.selected, ["B"]);

  console.log(`[selftest] addGids/removeGids: 5件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- duplicateRow の行データ導出(GUI改修 Task2 必須アサーション) ---
  const dupGroup = {
    shape_ids: ["s1", "s2"],
    triangle_count: 40,
    savable_triangles: 40,
    element_gids: [["a", "b"], ["c"]],
  };
  const dupRow = duplicateRow(dupGroup);
  check("duplicateRow(要素数=element_gidsの平坦化件数)", dupRow.cells.element_count, 3);
  check("duplicateRow(形状数=shape_ids.length)", dupRow.cells.shape_count, 2);
  check("duplicateRow(三角形数はそのまま)", dupRow.cells.triangle_count, 40);
  check("duplicateRow(節約可能はそのまま)", dupRow.cells.savable_triangles, 40);

  console.log(`[selftest] duplicateRow: 4件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- estimate.js の境界値(GUI改修 Task4 手順5) ---
  // 実際のサーバ定数(ifc_occam/scan/aggregate.FULLOPEN_BYTES_MULTIPLIER=14、
  // ifc_occam/cui/repl._FULLOPEN_WARN_BYTES=2*1024**3*14)と一次式の係数
  // (sec_per_mb=0.72/base_sec=30.0/band_low=0.5/band_high=2.0)をここに
  // ハードコードするのは「期待値」であって「実装への写経」ではない
  // (estimate.js自体はconfigを引数で受け取るだけで、これらの値を持たない)。
  const selftestConfig = {
    fullopen_bytes_multiplier: 14,
    fullopen_warn_bytes: 2 * 1024 ** 3 * 14,
    load_estimate: { sec_per_mb: 0.72, base_sec: 30.0, band_low: 0.5, band_high: 2.0 },
  };

  // small.ifc実測: 21.5MB -> 45秒
  const est21 = estimateLoad(21.5 * 1024 * 1024, selftestConfig);
  check(
    "estimateLoad(21.5MB): 実測45秒がsecLow〜secHighの範囲に入る",
    est21.secLow <= 45 && 45 <= est21.secHigh,
    true
  );
  // large.ifc実測: 102MB -> 103秒
  const est102 = estimateLoad(102 * 1024 * 1024, selftestConfig);
  check(
    "estimateLoad(102MB): 実測103秒がsecLow〜secHighの範囲に入る",
    est102.secLow <= 103 && 103 <= est102.secHigh,
    true
  );

  check("formatDuration(90) === '1分30秒'", formatDuration(90), "1分30秒");
  check("formatDuration(45) === '45秒'", formatDuration(45), "45秒");
  check("formatDuration(4000) === '1時間7分'", formatDuration(4000), "1時間7分");
  check("formatBytes(1536) === '1.5 KB'", formatBytes(1536), "1.5 KB");

  console.log(`[selftest] estimate.js: 6件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- isSameRootRelativePath の境界ケース(GUI改修 Task5 監督者裁定1) ---
  // 単純な文字列比較(===)では"./small.ifc"と"small.ifc"、大文字小文字違いを
  // 別ファイルと誤判定する、というのがこのタスクで直す前提の不具合だった。
  check(
    "isSameRootRelativePath(先頭の./の表記ゆれ)",
    isSameRootRelativePath("small.ifc", "./small.ifc"),
    true
  );
  check(
    "isSameRootRelativePath(大文字小文字違い)",
    isSameRootRelativePath("small.ifc", "SMALL.IFC"),
    true
  );
  check(
    "isSameRootRelativePath(バックスラッシュ表記の違い)",
    isSameRootRelativePath("sub/small.ifc", "sub\\small.ifc"),
    true
  );
  check(
    "isSameRootRelativePath(別ファイルはfalse)",
    isSameRootRelativePath("small.ifc", "large.ifc"),
    false
  );
  check(
    "isSameRootRelativePath(片方が空文字は同一とみなさない)",
    isSameRootRelativePath("", "small.ifc"),
    false
  );
  check(
    "isSameRootRelativePath(両方空文字も同一とみなさない)",
    isSameRootRelativePath("", ""),
    false
  );

  console.log(`[selftest] isSameRootRelativePath: 6件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- pickExportMessage(GUI改修 Task5 brief手順6 必須アサーション) ---
  // 既存バグ(出力失敗時にresult.errorを見ずにresult.deleted等を読んでいたため
  // 「削除: undefined件」等しか出なかった)の修正をフロント側で固定する。
  check(
    "pickExportMessage({error:'x'})は理由を含む失敗文言を返す",
    pickExportMessage({ error: "x" }),
    "出力に失敗しました: x"
  );
  check(
    "pickExportMessage(成功形)は失敗文言を返さない",
    pickExportMessage({ deleted: [], simplified: [], skipped: [] }).startsWith("出力に失敗しました"),
    false
  );

  console.log(`[selftest] pickExportMessage: 2件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- formatStageSeconds: 0秒のステージを除外する(GUI改修 Task5 監督者裁定5) ---
  const stageRows = formatStageSeconds({
    open: 1.2,
    deletes: 0,
    simplify: 0.5,
    reextract_duplicates: 0,
    consolidate: 0,
    write: 0.3,
  });
  check("formatStageSeconds(0秒のステージを除外した件数)", stageRows.length, 3);
  check("formatStageSeconds(1番目は開く)", stageRows[0].label, "開く");
  check("formatStageSeconds(2番目は簡略化)", stageRows[1].label, "簡略化");
  check("formatStageSeconds(3番目は書き込み)", stageRows[2].label, "書き込み");
  check("formatStageSeconds(stage_seconds未指定は空配列)", formatStageSeconds(undefined).length, 0);

  console.log(`[selftest] formatStageSeconds: 5件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- estimateSimplified(GUI改修 Task7 brief手順4・監督者裁定必須アサーション) ---
  check(
    "estimateSimplified(bbox): 要素数10→120三角形(要素あたり12三角形で確定)",
    estimateSimplified({ method: "bbox", elementCount: 10, triangles: 5000 })?.after,
    120
  );
  check(
    "estimateSimplified(decimate): round(5000*0.3)=1500",
    estimateSimplified({ method: "decimate", elementCount: 10, triangles: 5000, ratio: 0.3 })?.after,
    1500
  );
  check(
    "estimateSimplified(convex_hull): 事前に数値を出せないためnull(推測値を出さない)",
    estimateSimplified({ method: "convex_hull", elementCount: 10, triangles: 5000 }),
    null
  );
  check(
    "estimateSimplified(選択0件): elementCount=0はnull('約0→0三角形'を作らせない)",
    estimateSimplified({ method: "bbox", elementCount: 0, triangles: 0 }),
    null
  );

  console.log(`[selftest] estimateSimplified: 4件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- isClickNotDrag(GUI改修Task8 brief手順6・監督者裁定の必須アサーション) ---
  // 視点操作(ドラッグ)と選択/背景クリックを区別する閾値: 5px未満かつ400ms未満。
  check(
    "isClickNotDrag(小さい移動・短時間=クリック)",
    isClickNotDrag({ dx: 2, dy: 2, ms: 100 }),
    true
  );
  check(
    "isClickNotDrag(大きい移動=視点操作のドラッグ)",
    isClickNotDrag({ dx: 20, dy: 0, ms: 100 }),
    false
  );
  check(
    "isClickNotDrag(移動は無いが長時間の押下=ドラッグ扱い)",
    isClickNotDrag({ dx: 0, dy: 0, ms: 900 }),
    false
  );

  console.log(`[selftest] isClickNotDrag: 3件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- shouldOutline(GUI改修Task8 brief手順6・監督者裁定の必須アサーション) ---
  // 縁取り生成コストは選択三角形数に比例するため、200,000三角形を上限とする。
  check("shouldOutline(上限未満なら縁取りを作る)", shouldOutline(150000), true);
  check("shouldOutline(上限超過なら縁取りを作らない)", shouldOutline(250000), false);

  console.log(`[selftest] shouldOutline: 2件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- shouldDim(GUI改修Task8 監督者裁定2: 減光の規約) ---
  // 選択0件では減光しない(全要素が通常色)。1件以上選択されたときだけ減光する。
  check("shouldDim(選択0件では減光しない)", shouldDim(0), false);
  check("shouldDim(選択1件以上では減光する)", shouldDim(1), true);

  console.log(`[selftest] shouldDim: 2件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- roundToNiceStep(GUI改修Task9 必須アサーション: グリッド分割幅の丸め) ---
  // 期待値はroundToNiceStepを呼ばずに手計算したリテラル(1,2,5,10の系列を10進の
  // 桁ごとに適用する規則そのものを目で追って検算した値):
  //   1234 -> 指数3(base=1000)、fraction=1.234<1.5 -> 1倍 -> 1000
  //   37   -> 指数1(base=10)、  fraction=3.7 (3.5以上7.5未満) -> 5倍 -> 50
  //   60000-> 指数4(base=10000)、fraction=6   (3.5以上7.5未満) -> 5倍 -> 50000
  //          (small.ifcのようなmm単位・座標が数万〜数十万のモデルでも破綻しないことの確認)
  //   8    -> 指数0(base=1)、   fraction=8   (7.5以上)       -> 10倍 -> 10
  check("roundToNiceStep(1234) === 1000", roundToNiceStep(1234), 1000);
  check("roundToNiceStep(37) === 50", roundToNiceStep(37), 50);
  check(
    "roundToNiceStep(60000) === 50000(mm単位で座標が大きいモデルでも破綻しない)",
    roundToNiceStep(60000),
    50000
  );
  check("roundToNiceStep(8) === 10(次の桁への繰り上げ)", roundToNiceStep(8), 10);

  console.log(`[selftest] roundToNiceStep: 4件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- buildStage(GUI改修Task9→Z-up修正 必須アサーション: margin適用寸法と床Zの一致) ---
  // Z-up修正: IFCは+Zが上のため、床の垂直座標はY→Z、marginを適用する水平2方向は
  // (X,Z)の組→(X,Y)の組に読み替えた。期待値はbuildStageを呼ばずに手計算した
  // literal(size=(10,4,6), margin=0.15 -> padX=1.5, padY=0.6 -> 床X幅=10+2*1.5=13、
  // 床Y幅=4+2*0.6=5.2)。
  const stageTestBox = new THREE.Box3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(10, 4, 6));
  const stageTest = buildStage(stageTestBox, { margin: 0.15 });
  const stageFloor = stageTest.group.children.find((c) => c.name === "stage-floor");
  check(
    "buildStage: 床のZ座標がAABBの最小Zと一致する(裁定4、IFCはZ-up)",
    stageFloor.position.z,
    0
  );
  check(
    "buildStage: margin適用後の床のX幅(13)がAABBのX幅(10)より大きい",
    stageFloor.geometry.parameters.width > stageTestBox.max.x - stageTestBox.min.x,
    true
  );
  check(
    "buildStage: margin適用後の床のY幅(5.2)がAABBのY幅(4)より大きい",
    stageFloor.geometry.parameters.height > stageTestBox.max.y - stageTestBox.min.y,
    true
  );
  // 壁はユーザーの実機確認により削除した(グリッド付きの床だけで広がりと天地が
  // 読み取れ、壁は黒く重くモデルの視認性を下げていたため)。うっかり復活させない
  // ための番人。
  check(
    "buildStage: ステージの子は床・グリッド・軸だけ(壁を作らない)",
    stageTest.group.children.map((c) => c.name).sort().join(","),
    "stage-axes,stage-floor,stage-grid"
  );
  stageTest.dispose();

  console.log(`[selftest] buildStage: 4件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- camera.up(Z-up修正 必須アサーション: うっかりY-upに戻したら赤くなる番人) ---
  // IFCは+Zが上(仕様)。three.jsの既定+Y上のままだとモデルが90度倒れて表示される
  // (このタスクで直したバグそのもの)。initViewerの戻り値からcamera.up自体は
  // 取れないため、検証専用の最小限のgetter`getCameraUp()`をviewer.jsに追加した。
  const cameraUp = viewer.getCameraUp();
  check(
    "camera.upが(0,0,1)である(Z-up修正: IFCの+Zをthree.jsの上方向として扱う)",
    `${cameraUp.x},${cameraUp.y},${cameraUp.z}`,
    "0,0,1"
  );

  console.log(`[selftest] camera.up: 1件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- ifcColorToLinear(色task Task5 必須アサーション: sRGB→linear変換) ---
  // sRGB 0.5 -> linear ((0.5+0.055)/1.055)^2.4 = 0.2140(手計算)。
  // sRGB 0.0 -> 0.0、sRGB 1.0 -> 1.0 は境界。
  const lin = ifcColorToLinear([0.5, 0.0, 1.0]);
  check("ifcColorToLinear(sRGB 0.5 -> linear 0.2140付近)", Math.abs(lin[0] - 0.214) < 0.001, true);
  check("ifcColorToLinear(sRGB 0.0 -> linear 0.0)", lin[1], 0);
  check("ifcColorToLinear(sRGB 1.0 -> linear 1.0付近)", Math.abs(lin[2] - 1.0) < 0.001, true);

  console.log(`[selftest] ifcColorToLinear: 3件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- resolveBaseColor(色task Task5 必須アサーション: 真理値表4通り) ---
  // 色(3要素配列)を許容誤差付きで比較するローカルヘルパ。ifcColorToLinearの
  // ガンマ変換で生じる浮動小数の誤差を吸収するため、完全一致ではなく各成分の
  // 差が0.001未満かで判定する(passed/totalの集計はcheckと共有する)。
  function checkColor(label, actual, expected) {
    total++;
    const ok = actual.every((v, i) => Math.abs(v - expected[i]) < 0.001);
    console.assert(ok, `[selftest] ${label}: expected [${expected}], got [${actual}]`);
    if (ok) passed++;
  }

  // (a) 操作あり -> ステータス色(simplifyは[0.3,0.5,1.0])。colorMode/colorの
  //     値に関わらず最優先で勝つことを、両方に「該当しない値」を与えて確認する。
  const rbcOp = resolveBaseColor(
    { color: null, ifc_class: "IfcWall" },
    { operation: { op: "simplify" }, colorMode: COLOR_MODE_IFC }
  );
  checkColor("resolveBaseColor(a: 操作ありはcolorModeより優先してステータス色を返す)", rbcOp, [0.3, 0.5, 1.0]);

  // (b) IFC配色 + 色あり -> その色を線形化したもの。ifcColorToLinearと同じ入力・
  //     同じ手計算リテラルを流用する(「同じ色を線形化した値を返す」ことの確認)。
  const rbcIfcWithColor = resolveBaseColor(
    { color: [0.5, 0.0, 1.0], ifc_class: "IfcWall" },
    { operation: null, colorMode: COLOR_MODE_IFC }
  );
  checkColor(
    "resolveBaseColor(b: IFC配色+色ありはifcColorToLinearした値を返す)",
    rbcIfcWithColor,
    [0.214, 0.0, 1.0]
  );

  // (c) IFC配色 + 色なし -> NO_IFC_COLOR。[0.55,0.55,0.55]はviewer.jsの定義値を
  //     ここでもliteralで書く(importした定数同士の比較にすると、定数の値を
  //     書き換えるバグまで素通しするため)。
  const rbcIfcNoColor = resolveBaseColor(
    { color: null, ifc_class: "IfcWall" },
    { operation: null, colorMode: COLOR_MODE_IFC }
  );
  checkColor("resolveBaseColor(c: IFC配色+色なしはNO_IFC_COLORを返す)", rbcIfcNoColor, [0.55, 0.55, 0.55]);

  // (d) クラス配色 -> classColor(ifc_class)。ハッシュ値そのものをliteralで書くと
  //     実装追従になるため(ブリーフの指示どおり)、「同じクラス名なら常に同じ値」
  //     「違うクラス名なら違う値」という性質だけを検証する。
  const rbcClassWall1 = resolveBaseColor(
    { color: null, ifc_class: "IfcWall" },
    { operation: null, colorMode: COLOR_MODE_CLASS }
  );
  const rbcClassWall2 = resolveBaseColor(
    { color: [0.9, 0.1, 0.1], ifc_class: "IfcWall" },
    { operation: null, colorMode: COLOR_MODE_CLASS }
  );
  checkColor(
    "resolveBaseColor(d: クラス配色は同じクラス名なら常に同じ値。colorフィールドは無視される)",
    rbcClassWall2,
    rbcClassWall1
  );
  const rbcClassDoor = resolveBaseColor(
    { color: null, ifc_class: "IfcDoor" },
    { operation: null, colorMode: COLOR_MODE_CLASS }
  );
  const rbcClassDiffers = rbcClassWall1.some((v, i) => v !== rbcClassDoor[i]);
  check("resolveBaseColor(d: クラス配色は違うクラス名なら違う値)", rbcClassDiffers, true);

  console.log(`[selftest] resolveBaseColor: 5件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- dimColor(色task Task5 レビュー Important 必須アサーション: 減光の下限) ---
  // DIM_FACTOR=0.25, DIM_FLOOR=0.06での手計算(実装から逆算していない。0.06は
  // offscreen pixel-diffの実測から選んだ値——詳細は報告書「減光の下限」節)。
  //   [0.8, 0.0, 0.4] -> [0.8*0.25+0.06, 0.0*0.25+0.06, 0.4*0.25+0.06]
  //                    = [0.2+0.06, 0+0.06, 0.1+0.06] = [0.26, 0.06, 0.16]
  checkColor("dimColor([0.8, 0.0, 0.4]) === [0.26, 0.06, 0.16](手計算)", dimColor([0.8, 0.0, 0.4]), [
    0.26, 0.06, 0.16,
  ]);
  // 下限の効果そのものの確認: 元が真っ黒(0)でも0(=DIM_FLOOR)未満には落ちず、
  // レビューで問題になった「暗い色が壁のalbedo(0.023)未満まで沈む」ことがない
  // (0.06 > 0.023)。
  checkColor("dimColor([0, 0, 0]) === [DIM_FLOOR, DIM_FLOOR, DIM_FLOOR] = [0.06, 0.06, 0.06]", dimColor([0, 0, 0]), [
    0.06, 0.06, 0.06,
  ]);
  // 元が最大輝度(1)でも1.0を超えない([0.25+0.06=0.31] < 1.0)ことの確認
  // (色の飽和・クランプが必要ないことの裏付け)。
  checkColor("dimColor([1, 1, 1]) === [0.31, 0.31, 0.31](手計算)", dimColor([1, 1, 1]), [0.31, 0.31, 0.31]);

  console.log(`[selftest] dimColor: 3件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- resolveDragAction(3Dビュー操作入替Cam Task1 必須アサーション: ボタン→操作割り当て) ---
  check("resolveDragAction(0=左は視点移動)", resolveDragAction(0), "look");
  check("resolveDragAction(1=中/ホイールはパン)", resolveDragAction(1), "pan");
  check("resolveDragAction(2=右は回転)", resolveDragAction(2), "orbit");
  check("resolveDragAction(割り当てのないボタンはnull)", resolveDragAction(3), null);

  console.log(`[selftest] resolveDragAction: 4件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- clampPolarDelta(Cam Task1 必須アサーション: 極角の可動範囲クランプ) ---
  // 期待値はclampPolarDeltaを呼ばずに手計算したliteral。加算後に減算する実装のため
  // (例: 1.0+0.2を計算してから-1.0する)、JSの浮動小数点表現では厳密等価(===)が
  // 保証されない(実測: 1.2-1.0が0.19999999999999996になるなど)。ifcColorToLinear等
  // 既存の浮動小数点比較と同じ「Math.abs(差)<閾値をcheckに通す」パターンに合わせる。
  check(
    "clampPolarDelta(範囲の真ん中では変化量がそのまま通る)",
    Math.abs(clampPolarDelta(1.0, 0.2) - 0.2) < 1e-9,
    true
  );
  check(
    "clampPolarDelta(上限を越える分は切り詰められる。手計算: 3.0+1.0=4.0は上限3.1を越えるので3.1-3.0=0.1)",
    Math.abs(clampPolarDelta(3.0, 1.0, 0.001, 3.1) - 0.1) < 1e-9,
    true
  );
  check(
    "clampPolarDelta(下限を越える分も同様に切り詰められる。手計算: 0.1-1.0=-0.9は下限0.05未満なので0.05-0.1=-0.05)",
    Math.abs(clampPolarDelta(0.1, -1.0, 0.05, 3.1) - -0.05) < 1e-9,
    true
  );

  console.log(`[selftest] clampPolarDelta: 3件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- orbitAroundPivot(Cam Task1 必須アサーション: 原点を軸に+X側10の位置からyaw90度) ---
  // 入力: position(10,0,0)、quaternionは単位、pivot(0,0,0)、yaw=Math.PI/2、pitch=0。
  // 期待値はorbitAroundPivotを呼ばずに手計算したliteral(手計算の手順・符号の判断根拠は
  // 報告書「回転の符号をどう決めたか」節を参照。Task 2 Step 3の実機確認で符号を確定させた)。
  const orbitResult = orbitAroundPivot({
    position: new THREE.Vector3(10, 0, 0),
    quaternion: new THREE.Quaternion(),
    pivot: new THREE.Vector3(0, 0, 0),
    yaw: Math.PI / 2,
    pitch: 0,
    THREE,
  });
  check(
    "orbitAroundPivot(yaw90度: position.xが0付近、誤差1e-6以内)",
    Math.abs(orbitResult.position.x - 0) < 1e-6,
    true
  );
  check(
    "orbitAroundPivot(yaw90度: position.yが-10付近、誤差1e-6以内)",
    Math.abs(orbitResult.position.y - -10) < 1e-6,
    true
  );
  check(
    "orbitAroundPivot(yaw90度: position.zが0付近、誤差1e-6以内)",
    Math.abs(orbitResult.position.z - 0) < 1e-6,
    true
  );
  // 符号に依存しない不変量: 回転なのでpivotからの距離(10)は保存される。
  check(
    "orbitAroundPivot(pivotからの距離は回転で保存される。手計算: 10)",
    Math.abs(orbitResult.position.distanceTo(new THREE.Vector3(0, 0, 0)) - 10) < 1e-6,
    true
  );

  // ここまでのアサーションは position しか見ていなかった。レビューで、
  // orbitAroundPivot が「向きを一切変えない」実装に改竄しても全て通ってしまう
  // (= 左ドラッグの視点移動が完全な no-op になっても気付けない)ことが実証された。
  // このモジュールの存在理由そのものである「位置と向きに同じ回転を掛ける」を
  // 以下3件で固定する。
  //
  // (1) yaw90度のquaternion。手計算: qYaw = axis(0,0,1)を角度 -π/2 で回す
  //     -> (x,y,z,w) = (0, 0, sin(-π/4), cos(-π/4)) = (0, 0, -0.70710678, 0.70710678)。
  //     入力quaternionが単位なので、結果はこれそのもの。
  check(
    "orbitAroundPivot(yaw90度: quaternionが(0,0,-0.70710678,0.70710678)付近)",
    Math.abs(orbitResult.quaternion.x - 0) < 1e-6 &&
      Math.abs(orbitResult.quaternion.y - 0) < 1e-6 &&
      Math.abs(orbitResult.quaternion.z - -0.70710678) < 1e-6 &&
      Math.abs(orbitResult.quaternion.w - 0.70710678) < 1e-6,
    true
  );

  // (2) pivot === position のときは位置が動かず、向きだけが変わる。
  //     これが左ドラッグ(視点移動)が成立する条件そのもの。
  const lookStart = new THREE.Vector3(3, 4, 5);
  const lookQuat = new THREE.Quaternion();
  const lookResult = orbitAroundPivot({
    position: lookStart.clone(),
    quaternion: lookQuat.clone(),
    pivot: lookStart.clone(),
    yaw: 0.3,
    pitch: 0.2,
    THREE,
  });
  check(
    "orbitAroundPivot(pivot=positionなら位置は不動。手計算: (3,4,5)のまま)",
    lookResult.position.distanceTo(lookStart) < 1e-12,
    true
  );
  check(
    "orbitAroundPivot(pivot=positionでも向きは変わる。回転角>0.1rad)",
    lookResult.quaternion.angleTo(lookQuat) > 0.1,
    true
  );

  // (3) pitch を掛けた分だけ極角(ワールド+Zから見た視線の角度)がちょうど増える。
  //     コントローラ側の極角追跡(currentPolar += pitch)が成り立つ前提であり、
  //     ここが崩れると可動範囲の制限が逆の極に効く。
  //     注意: 単位quaternionは「真下を向く」退化姿勢(極角π)なので使わない。
  //     極角2.0の現実的な姿勢を作ってから0.1だけpitchする -> 2.1(手計算)。
  const polarOf = (q) => {
    const f = new THREE.Vector3(0, 0, -1).applyQuaternion(q).normalize();
    return Math.acos(Math.min(1, Math.max(-1, f.z)));
  };
  const pitchCam = new THREE.PerspectiveCamera(50, 1, 0.1, 100);
  pitchCam.up.set(0, 0, 1);
  pitchCam.position.set(0, 0, 0);
  pitchCam.lookAt(new THREE.Vector3(Math.sin(2.0), 0, Math.cos(2.0)));
  pitchCam.updateMatrixWorld(true);
  const pitchResult = orbitAroundPivot({
    position: pitchCam.position.clone(),
    quaternion: pitchCam.quaternion.clone(),
    pivot: pitchCam.position.clone(),
    yaw: 0,
    pitch: 0.1,
    THREE,
  });
  check(
    "orbitAroundPivot(極角2.0からpitch0.1で極角2.1になる。手計算)",
    Math.abs(polarOf(pitchResult.quaternion) - 2.1) < 1e-6,
    true
  );

  console.log(`[selftest] orbitAroundPivot: 8件の境界ケースを含む合計 ${passed}/${total} passed`);

  // --- worldUnitsPerPixel(Cam Task1 必須アサーション: 画面1pxが何ワールド単位か) ---
  // 手計算: worldUnitsPerPixel(10, 50, 600) = 2*10*tan(25°)/600。
  // tan(25°) = 0.4663076582。よって 2*10*0.4663076582 = 9.326153164 -> /600 = 0.01554359(手計算)。
  check(
    "worldUnitsPerPixel(10,50,600)は手計算値0.01554359付近(誤差1e-6)",
    Math.abs(worldUnitsPerPixel(10, 50, 600) - 0.01554359) < 1e-6,
    true
  );
  check(
    "worldUnitsPerPixel(viewportHeightPx=0は0除算を防いで0を返す)",
    worldUnitsPerPixel(10, 50, 0),
    0
  );

  console.log(`[selftest] worldUnitsPerPixel: 2件の境界ケースを含む合計 ${passed}/${total} passed`);
}
