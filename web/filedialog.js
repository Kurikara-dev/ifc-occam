// filedialog.js -- ファイル選択/保存ダイアログの生成と操作(GUI改修 Task4)。
//
// ブラウザの <input type="file"> は選んだファイルの絶対パスを返さない
// (セキュリティ制約)ため、サーバのファイル一覧API(/api/files)を使った
// 自前のファイルブラウザモーダルを組む。参照できるのはサーバ起動フォルダ
// (root)以下だけ(監督者裁定1)。返す文字列はroot相対パス(フォワード
// スラッシュ区切り)で、そのまま POST /api/load の path やexportの
// output_path に渡せる(サーバのrootはPath.cwd()なので、cwd相対パスとして
// も解釈される)。
//
// クラス/レイヤー/重複形状の一覧が共有するdatatable.jsのrenderDataTableは
// ここでも「ファイル」部分(名前/サイズ/更新日時、ヘッダクリックで列ソート)
// に再利用する。ただし「ディレクトリ」部分(太字の名前のみ・ダブルクリックで
// 掘る・列やソートを持たない)はrenderDataTableの型に合わない
// (renderDataTableは1クリックの選択とクイック操作ボタン列の2つを前提に
// しており、ダブルクリックや行ごとの太字スタイルのフックを持たない。
// gids/onQuickActionもこのユースケースには無関係)。ここを無理に共通化すると
// Task2で確定した公開契約(既にクラス/レイヤー/重複形状タブが依存している)
// を変える必要が生じるため、ディレクトリ部分は独自の軽量なDOM生成に留める。
//
// GUI改修Task5: opts.excludePathを追加(出力先ダイアログが読込中のファイルと
// 同一実体を選べないようにするため、既存呼び出し元は無変更で動く追加のみの
// 拡張)。同一性判定(isSameRootRelativePath)はここでexportし、app.jsの
// 手打ち欄チェックとも共有する(重複実装を避ける——filedialog.jsが
// root相対パスの意味論を最も詳しく知る場所であるため、この方向の依存
// (app.js → filedialog.js)は既存のopenFileDialogインポートと同じ向きで、
// 循環importにならない)。

import { renderDataTable } from "./datatable.js";
import { fetchFileList } from "./api.js";
import { formatBytes } from "./estimate.js";

const FILE_COLUMNS = [
  { key: "name", label: "名前", align: "left" },
  { key: "size", label: "サイズ", align: "right", format: (v) => (v == null ? "" : formatBytes(v)) },
  { key: "mtime", label: "更新日時", align: "right", format: (v) => formatMtime(v) },
];

/**
 * root相対パスのディレクトリ部分とファイル名部分に分ける。
 * @param {string} path
 * @returns {{dir: string, name: string}}
 */
function splitPath(path) {
  if (!path) return { dir: "", name: "" };
  const parts = path.split("/");
  const name = parts.pop();
  return { dir: parts.join("/"), name: name ?? "" };
}

/**
 * ディレクトリ部分とファイル名を結合してroot相対パスにする。
 * @param {string} dir
 * @param {string} name
 * @returns {string}
 */
function joinPath(dir, name) {
  return dir ? `${dir}/${name}` : name;
}

/**
 * epoch秒(list_directoryが返すmtime)を日本語のロケール表記にする
 * (表示専用のヘルパーであり推定ロジックではないため、estimate.jsのような
 * 純粋関数selftest対象にはしない——app.jsのescapeHtml/opLabelと同じ扱い)。
 * @param {number} epochSeconds
 * @returns {string}
 */
function formatMtime(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  return d.toLocaleString("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * root相対パス文字列を比較用に正規化する(GUI改修Task5 監督者裁定1)。
 * バックスラッシュ→フォワードスラッシュ、空/"."セグメント(連続スラッシュ・
 * 先頭の"./")の除去、大文字小文字の統一(Windowsのファイルシステムは大小文字を
 * 区別しない)を行う。".."の解決までは行わない——ダイアログ経由の値はroot
 * 閉じ込め(resolve_within_root)により実質的に意味を持たず、手打ちで".."を
 * 含む値が来てもサーバ側 core/paths.refers_to_same_file が resolve() で正しく
 * 判定する(この関数はUI側の事前チェック用であり最終防衛線ではない)。
 * @param {string} path
 * @returns {string}
 */
function normalizeForComparison(path) {
  return String(path ?? "")
    .replace(/\\/g, "/")
    .split("/")
    .filter((segment) => segment !== "" && segment !== ".")
    .join("/")
    .toLowerCase();
}

/**
 * 2つのroot相対パス文字列が同一ファイルを指す見た目かを判定する
 * (GUI改修Task5 監督者裁定1)。単純な文字列比較(===)は"./small.ifc"と
 * "small.ifc"、大文字小文字違いを別ファイルと誤判定するため、
 * normalizeForComparisonを介してから比較する。
 *
 * これはUI側の事前チェック(表記ゆれの吸収)であり、シンボリックリンクや
 * 8.3短縮名までは解決しない——それらはサーバ側 core/paths.refers_to_same_file
 * (os.path.samefile経由)が最終防衛線として拒否する。両方が効いている状態を
 * 意図的な設計としている(このUI側判定が万一取りこぼしても、サーバ側が
 * 原本上書きを防ぐ)。空文字列同士は「同じファイル」とはみなさない
 * (未選択どうしを衝突と誤判定しないため)。
 * @param {string} a
 * @param {string} b
 * @returns {boolean}
 */
export function isSameRootRelativePath(a, b) {
  if (!a || !b) return false;
  return normalizeForComparison(a) === normalizeForComparison(b);
}

/**
 * 出力先が読込中のファイルと衝突したときの文言(保存ダイアログの入力欄と
 * 手打ち欄の両方で出す)。**この定数を唯一の出典にすること**——同じ文言を
 * 各所にリテラルで置くと、片方だけ直されて表示が食い違う
 * (Task 5 レビュー Minor: app.js と filedialog.js に重複していた)。
 * @type {string}
 */
export const SAME_AS_SOURCE_MESSAGE =
  "出力先が読込中のファイルと同じです。別の名前にしてください。";

/**
 * サーバ起動フォルダ配下のファイル一覧モーダルダイアログを開く。
 * 選択されたファイルのroot相対パスをPromiseで返す。Esc・オーバーレイの
 * クリック・キャンセルボタンのいずれで閉じてもnullで解決する。
 *
 * @param {object} [opts]
 * @param {"open"|"save"} [opts.mode="open"]
 * @param {string} [opts.initialPath=""] 開始位置のroot相対パス。
 *   mode="save"のときは末尾をファイル名候補として入力欄に入れる。
 * @param {string[]} [opts.suffixes=[".ifc"]] 表示するファイルの拡張子
 *   (大小文字は無視)。空配列は無フィルタ。ディレクトリは常に表示する。
 * @param {string} [opts.excludePath] 選択不可にするroot相対パス
 *   (GUI改修Task5 監督者裁定2: 読込中のファイルと同一のファイルを出力先に
 *   選べないようにする)。一覧中でこのパスと同一実体を指すエントリ
 *   (isSameRootRelativePath判定)は行が無効化され、名前欄に
 *   `(読込中のファイル)` と注記される。mode="save"のときはファイル名入力欄で
 *   同じ実体を手打ちした場合も確定ボタンを無効化し赤字で理由を示す。
 *   省略時(既定undefined)は何も除外しない(Task4の呼び出し元は無変更で動く)。
 * @returns {Promise<string|null>}
 */
export function openFileDialog(opts = {}) {
  const mode = opts.mode ?? "open";
  const initialPath = opts.initialPath ?? "";
  const suffixes = opts.suffixes ?? [".ifc"];
  const excludePath = opts.excludePath ?? null;
  const isSave = mode === "save";

  const initial = isSave ? splitPath(initialPath) : { dir: initialPath, name: "" };

  return new Promise((resolve) => {
    let currentDir = "";
    let selectedFileName = null;
    let closed = false;

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";

    const box = document.createElement("div");
    box.className = "modal-box filedialog-box";

    const title = document.createElement("h3");
    title.textContent = isSave ? "保存先を選択" : "ファイルを選択";
    box.appendChild(title);

    const breadcrumb = document.createElement("div");
    breadcrumb.className = "filedialog-breadcrumb";
    box.appendChild(breadcrumb);

    const errorEl = document.createElement("div");
    errorEl.className = "filedialog-error";
    errorEl.style.display = "none";
    box.appendChild(errorEl);

    const listWrap = document.createElement("div");
    listWrap.className = "filedialog-list";
    const dirsEl = document.createElement("div");
    dirsEl.className = "filedialog-dirs";
    const filesEl = document.createElement("div");
    filesEl.className = "filedialog-files-wrap";
    listWrap.appendChild(dirsEl);
    listWrap.appendChild(filesEl);
    box.appendChild(listWrap);

    let filenameInput = null;
    let filenameCollisionEl = null;
    if (isSave) {
      const filenameRow = document.createElement("div");
      filenameRow.className = "filedialog-filename-row";
      const label = document.createElement("label");
      label.textContent = "ファイル名";
      filenameInput = document.createElement("input");
      filenameInput.type = "text";
      filenameInput.className = "filedialog-filename-input";
      filenameInput.value = initial.name;
      label.appendChild(filenameInput);
      filenameRow.appendChild(label);
      box.appendChild(filenameRow);

      // 監督者裁定2: 一覧の行を無効化するだけでは、同じファイル名をこの入力欄に
      // 直接手打ちして確定するループホールが残る。名前欄の値が変わるたびに
      // excludePathと突き合わせ、衝突時は確定ボタンごと止める。
      filenameCollisionEl = document.createElement("div");
      filenameCollisionEl.className = "filedialog-filename-error rule-error";
      filenameCollisionEl.style.display = "none";
      filenameRow.appendChild(filenameCollisionEl);

      filenameInput.addEventListener("input", updateConfirmEnabled);
    }

    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.textContent = "キャンセル";
    const confirmButton = document.createElement("button");
    confirmButton.type = "button";
    confirmButton.className = "modal-btn-primary";
    confirmButton.textContent = "選択";
    actions.appendChild(cancelButton);
    actions.appendChild(confirmButton);
    box.appendChild(actions);

    overlay.appendChild(box);

    function close(value) {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", onKeyDown);
      overlay.remove();
      resolve(value);
    }

    function onKeyDown(evt) {
      if (evt.key === "Escape") {
        evt.stopPropagation();
        close(null);
      }
    }

    overlay.addEventListener("click", (evt) => {
      if (evt.target === overlay) close(null);
    });
    document.addEventListener("keydown", onKeyDown);

    cancelButton.addEventListener("click", () => close(null));
    confirmButton.addEventListener("click", () => {
      if (isSave) {
        const name = filenameInput.value.trim();
        if (!name) return;
        close(joinPath(currentDir, name));
      } else {
        if (selectedFileName == null) return;
        close(joinPath(currentDir, selectedFileName));
      }
    });

    function updateConfirmEnabled() {
      if (isSave) {
        const name = filenameInput.value.trim();
        const collides =
          name !== "" &&
          excludePath != null &&
          isSameRootRelativePath(joinPath(currentDir, name), excludePath);
        if (filenameCollisionEl) {
          filenameCollisionEl.style.display = collides ? "block" : "none";
          filenameCollisionEl.textContent = collides ? SAME_AS_SOURCE_MESSAGE : "";
        }
        confirmButton.disabled = name === "" || collides;
      } else {
        confirmButton.disabled = selectedFileName == null;
      }
    }
    updateConfirmEnabled();

    function showDialogError(message) {
      errorEl.textContent = message;
      errorEl.style.display = "block";
    }
    function clearDialogError() {
      errorEl.style.display = "none";
      errorEl.textContent = "";
    }

    function renderBreadcrumb() {
      breadcrumb.innerHTML = "";
      const rootBtn = document.createElement("button");
      rootBtn.type = "button";
      rootBtn.className = "filedialog-breadcrumb-segment";
      rootBtn.textContent = "(ルート)";
      rootBtn.addEventListener("click", () => navigateTo(""));
      breadcrumb.appendChild(rootBtn);

      const parts = currentDir ? currentDir.split("/") : [];
      let accum = "";
      for (const part of parts) {
        accum = accum ? `${accum}/${part}` : part;
        const sep = document.createElement("span");
        sep.className = "filedialog-breadcrumb-sep";
        sep.textContent = "/";
        breadcrumb.appendChild(sep);

        const target = accum;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "filedialog-breadcrumb-segment";
        btn.textContent = part;
        btn.addEventListener("click", () => navigateTo(target));
        breadcrumb.appendChild(btn);
      }
    }

    function matchesSuffix(name) {
      if (!suffixes || suffixes.length === 0) return true;
      const lower = name.toLowerCase();
      return suffixes.some((s) => lower.endsWith(s.toLowerCase()));
    }

    function refreshFileSelectionHighlight() {
      for (const tr of filesEl.querySelectorAll("tbody tr")) {
        const row = tr._row;
        if (!row) continue;
        tr.classList.toggle("selected-row", row.cells.name === selectedFileName);
      }
    }

    function renderDirs(dirEntries) {
      dirsEl.innerHTML = "";
      for (const entry of dirEntries) {
        const row = document.createElement("div");
        row.className = "filedialog-dir-row";
        row.textContent = entry.name;
        row.tabIndex = 0;
        const target = joinPath(currentDir, entry.name);
        row.addEventListener("dblclick", () => navigateTo(target));
        row.addEventListener("keydown", (evt) => {
          if (evt.key === "Enter") navigateTo(target);
        });
        dirsEl.appendChild(row);
      }
    }

    /**
     * 現在のディレクトリ内のファイル名がexcludePath(読込中のファイル)と
     * 同一実体を指すかを判定する(監督者裁定2)。excludePath未指定なら常にfalse。
     * @param {string} name
     * @returns {boolean}
     */
    function isExcludedEntry(name) {
      if (excludePath == null) return false;
      return isSameRootRelativePath(joinPath(currentDir, name), excludePath);
    }

    function renderFiles(fileEntries) {
      const rows = fileEntries.map((e) => {
        const excluded = isExcludedEntry(e.name);
        return {
          key: e.name,
          cells: {
            name: excluded ? `${e.name} (読込中のファイル)` : e.name,
            size: e.size,
            mtime: e.mtime,
          },
          disabled: excluded,
          note: excluded ? "読込中のファイルのため選択できません" : undefined,
        };
      });
      renderDataTable(filesEl, {
        columns: FILE_COLUMNS,
        rows,
        defaultSort: { key: "name", dir: "asc" },
        onRowClick: (row) => {
          selectedFileName = row.cells.name;
          if (isSave) filenameInput.value = row.cells.name;
          refreshFileSelectionHighlight();
          updateConfirmEnabled();
        },
        emptyMessage: "対象のファイルがありません",
      });
      refreshFileSelectionHighlight();
    }

    function renderEntries(entries) {
      const dirs = entries.filter((e) => e.is_dir);
      const files = entries.filter((e) => !e.is_dir && matchesSuffix(e.name));
      renderDirs(dirs);
      renderFiles(files);
    }

    async function navigateTo(path) {
      let data;
      try {
        data = await fetchFileList(path);
      } catch (err) {
        // 監督者裁定9: 一覧取得の失敗はモーダル内にエラーとして出し、
        // 黙って閉じない。現在表示中の一覧・パンくずはそのまま保つ。
        showDialogError(`一覧の取得に失敗しました: ${err.detail || err.message || err}`);
        return;
      }
      currentDir = data.path;
      selectedFileName = null;
      clearDialogError();
      renderBreadcrumb();
      renderEntries(data.entries || []);
      updateConfirmEnabled();
    }

    document.body.appendChild(overlay);
    navigateTo(initial.dir);
  });
}
