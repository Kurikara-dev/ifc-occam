// datatable.js -- クラス/レイヤー/重複形状の3つの一覧が共有するテーブル部品。
// 列定義+行+クイックボタン+ソートのみを扱う(選択の確定や操作の実行はしない、
// onRowClick/onQuickActionで呼び出し側(app.js)に委ねる)。DOM生成のみで
// three.js/APIには関与しない(純粋なUI部品)。
//
// ソート状態はcontainer要素(呼び出し側が渡すDOMノード)をキーにモジュール内の
// WeakMapへ保持する。クラス/レイヤー/重複形状タブはそれぞれ別のcontainerを
// 使うため、これだけで「タブごとに独立したソート状態」が成立する
// (呼び出し側がソート状態を別途管理する必要はない)。

const sortStateByContainer = new WeakMap();

/**
 * 行クリック時の修飾キーからクリックモードを判定する。
 * Shift = 選択に追加(additive)、Ctrl または Cmd(Macのmeta) = 選択から除外
 * (subtractive)、それ以外(修飾なし) = 置換(replace)。
 * @param {{shiftKey?: boolean, ctrlKey?: boolean, metaKey?: boolean}} evt
 * @returns {"additive"|"subtractive"|"replace"}
 */
export function readClickMode(evt) {
  if (evt && evt.shiftKey) return "additive";
  if (evt && (evt.ctrlKey || evt.metaKey)) return "subtractive";
  return "replace";
}

/**
 * 数値/文字列いずれの列でも比較できる汎用コンパレータ(昇順)。
 * @param {*} a
 * @param {*} b
 */
function compareValues(a, b) {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a ?? "").localeCompare(String(b ?? ""));
}

/**
 * containerに紐づく現在のソート状態で rows を並べ替える(元の配列は変更しない)。
 * ソート状態が無い(まだヘッダをクリックしておらず、defaultSortも無い)場合は
 * 呼び出し側が渡した順序のまま返す。
 * @param {HTMLElement} container
 * @param {Array<object>} rows
 */
function sortedRows(container, rows) {
  const sortState = sortStateByContainer.get(container);
  if (!sortState) return rows;
  const sorted = [...rows].sort((a, b) => {
    const cmp = compareValues(a.cells[sortState.key], b.cells[sortState.key]);
    return sortState.dir === "desc" ? -cmp : cmp;
  });
  return sorted;
}

/**
 * クイック操作ボタン(削除/軽量化/残す)を1個作る。opacity:0で隠し、
 * 行hover・行内フォーカス・ボタン自身のフォーカスのいずれでも見えるようにする
 * (styles.css側の :hover / :focus-within / :focus-visible。ホバー専用にすると
 * キーボード操作で永久に押せなくなるため)。
 * @param {string} label
 * @param {string|undefined} title
 * @param {() => void} onClick
 */
function makeQuickButton(label, title, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "row-quick-button";
  btn.textContent = label;
  if (title) btn.title = title;
  btn.addEventListener("click", (evt) => {
    evt.stopPropagation();
    onClick();
  });
  return btn;
}

const QUICK_ACTIONS = [
  { action: "delete", label: "削除" },
  { action: "simplify", label: "軽量化" },
  { action: "keep", label: "残す" },
];

/**
 * 列定義+行データから一覧テーブルを描画する。クラス別ランキング/レイヤー/
 * 重複形状グループの3パネルが共有する(GUI改修 Task2)。
 *
 * @param {HTMLElement} container 描画先(この要素の中身を丸ごと入れ替える)
 * @param {object} opts
 * @param {Array<{key:string, label:string, align?:"left"|"right", format?:(v:*)=>string}>} opts.columns
 * @param {Array<{key:string, cells:object, gids:string[], disabled?:boolean, note?:string}>} opts.rows
 * @param {(row:object, mode:{additive:boolean, subtractive:boolean}) => void} [opts.onRowClick]
 * @param {(row:object, action:"delete"|"simplify"|"keep") => void} [opts.onQuickAction]
 * @param {string} [opts.emptyMessage] rowsが空の時に表示する文言
 * @param {(row:object, action:"delete"|"simplify"|"keep") => string|undefined} [opts.actionTitle]
 *   クイックボタンのtitle属性(行の種類に応じて呼び出し側が文言を決める。brief手順1)
 * @param {{key:string, dir:"asc"|"desc"}} [opts.defaultSort]
 *   このcontainerにまだソート状態が無い時(=初回描画時)だけ使う既定ソート。
 *   一度ヘッダがクリックされた後はユーザーの選択が優先される。
 */
export function renderDataTable(container, opts) {
  const { columns, rows, onRowClick, onQuickAction, emptyMessage, actionTitle, defaultSort } = opts;

  if (!sortStateByContainer.has(container) && defaultSort) {
    sortStateByContainer.set(container, { ...defaultSort });
  }

  draw();

  function draw() {
    container.innerHTML = "";

    if (!rows || rows.length === 0) {
      if (emptyMessage) {
        const empty = document.createElement("div");
        empty.className = "datatable-empty";
        empty.textContent = emptyMessage;
        container.appendChild(empty);
      }
      return;
    }

    const hasActions = typeof onQuickAction === "function";
    const table = document.createElement("table");
    table.className = "data-table";

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    const sortState = sortStateByContainer.get(container);
    for (const col of columns) {
      const th = document.createElement("th");
      th.textContent = col.label;
      if (col.align === "right") th.classList.add("col-right");
      if (sortState && sortState.key === col.key) {
        th.classList.add("sorted");
        th.dataset.sortDir = sortState.dir;
      }
      th.addEventListener("click", () => {
        const prev = sortStateByContainer.get(container);
        const nextDir = prev && prev.key === col.key && prev.dir === "desc" ? "asc" : "desc";
        sortStateByContainer.set(container, { key: col.key, dir: nextDir });
        draw();
      });
      headRow.appendChild(th);
    }
    if (hasActions) headRow.appendChild(document.createElement("th"));
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const row of sortedRows(container, rows)) {
      const tr = document.createElement("tr");
      // renderSelectionの行ハイライト(app.js)がソート後の見た目の並びに関わらず
      // 「このtrはどのgids群か」を引けるよう、行データそのものを持たせておく。
      tr._row = row;
      if (row.disabled) tr.classList.add("row-disabled");
      if (row.note) tr.title = row.note;

      for (const col of columns) {
        const td = document.createElement("td");
        if (col.align === "right") td.classList.add("col-right");
        const raw = row.cells[col.key];
        td.textContent = col.format ? col.format(raw) : raw;
        tr.appendChild(td);
      }

      if (hasActions) {
        const actionsTd = document.createElement("td");
        actionsTd.className = "row-actions";
        if (!row.disabled) {
          for (const { action, label } of QUICK_ACTIONS) {
            const title = actionTitle ? actionTitle(row, action) : undefined;
            actionsTd.appendChild(
              makeQuickButton(label, title, () => onQuickAction(row, action))
            );
          }
        }
        tr.appendChild(actionsTd);
      }

      if (onRowClick && !row.disabled) {
        tr.addEventListener("click", (evt) => {
          const mode = readClickMode(evt);
          onRowClick(row, {
            additive: mode === "additive",
            subtractive: mode === "subtractive",
          });
        });
      }

      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    container.appendChild(table);
  }
}
