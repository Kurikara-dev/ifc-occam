// viewer.js -- Three.jsによる単一マージジオメトリの表示・カメラ・raycastを担う。
// DOM(サイドバー等)には関与しない。

import * as THREE from "./vendor/three.module.js";
import { createCameraControls } from "./cameracontrols.js";
import { statusColor } from "./operations.js";
import { buildStage } from "./stage.js";

/**
 * 文字列を安定したハッシュ値からHSL色に変換する(クラス名→色)。
 * @param {string} str
 * @returns {[number, number, number]} r,g,b (0..1)
 */
export function classColor(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = (hash * 31 + str.charCodeAt(i)) | 0;
  }
  const hue = ((hash % 360) + 360) % 360;
  const color = new THREE.Color();
  color.setHSL(hue / 360, 0.55, 0.55);
  return [color.r, color.g, color.b];
}

// --- 配色モード(色task Task5)に関する定数・純粋関数 ---

export const COLOR_MODE_IFC = "ifc";
export const COLOR_MODE_CLASS = "class";
// IFCの色を持たない要素の代替色(無彩色。クラス色と混ざって誤読されないように
// 彩度を持たせない)。
export const NO_IFC_COLOR = [0.55, 0.55, 0.55];

const _srgbScratch = new THREE.Color();

/**
 * IfcColourRgb の値(sRGB表示色)を three.js の working color space(linear-sRGB)へ
 * 変換する。three.jsは頂点カラー属性を linear として解釈するため、sRGB値をそのまま
 * 入れると明るく転ぶ。
 * @param {[number,number,number]} rgb 0..1 のsRGB
 * @returns {[number,number,number]} 0..1 のlinear-sRGB
 */
export function ifcColorToLinear([r, g, b]) {
  _srgbScratch.setRGB(r, g, b, THREE.SRGBColorSpace);
  return [_srgbScratch.r, _srgbScratch.g, _srgbScratch.b];
}

/**
 * 要素の基本色(選択・減光を適用する前の色)を決める。
 * 優先順: 有効な操作があればステータス色 -> IFC配色ならIFCの色(無ければ無彩色)
 *         -> クラス色。
 * @param {object} element meta.elements の1要素
 * @param {{operation: object|null|undefined, colorMode: string}} params
 * @returns {[number,number,number]}
 */
export function resolveBaseColor(element, { operation, colorMode }) {
  if (operation) return statusColor(operation);
  if (colorMode === COLOR_MODE_IFC) {
    return element.color ? ifcColorToLinear(element.color) : NO_IFC_COLOR;
  }
  return classColor(element.ifc_class);
}

// --- 選択の可視化(GUI改修 Task8)に関する定数・純粋関数 ---
// DOM/Three.jsの状態に一切触れない(?selftest=1から直接呼べる)。

// 選択色(監督者裁定3): 既存の [1, 0.2, 0.2] から変更。
export const SELECTION_COLOR = [1.0, 0.35, 0.15];
// 非選択かつdim===trueの要素に掛ける係数(監督者裁定2)。
export const DIM_FACTOR = 0.25;
// 減光の下限(linear)。掛け算だけで落とすと、元が暗い色(IFCの実測値には
// linear 0.07 程度のものが普通にある)が壁の albedo 0.023 と同じ明るさになり、
// 背景に沈んで見えなくなる(レビュー実測: IFC配色×減光でモデル画素の47.8%が
// 背景との輝度差10未満)。下限を足して、減光しても背景からは必ず浮かせる
// (色task Task5 レビュー Important)。値は small.ifc の offscreen pixel-diff で
// 実測して決めた(背景との輝度差が10未満のモデル画素の割合):
//   0.03=20.2% / 0.035=16.4% / 0.04=13.55% / 0.05=3.9% / 0.06=3.4%
// 受け入れ基準(<15%)を満たす最小値は0.04だが、0.06を採る。0.04と0.06で
// 減光済み要素の最大輝度は 0.24 と 0.26 しか違わず(選択色は常に1.0なので
// 強調の落差はほぼ変わらない)、一方で背景に沈む画素は 13.6% から 3.4% へ
// 4分の1になる。コストがほぼ平坦で効果が急なので、最小値を選ぶ理由がない
// (「基準を満たす最小値を選べ」という当初の監督者指示を撤回した)。
export const DIM_FLOOR = 0.06;
// 縁取りの外観(監督者裁定4)。
export const OUTLINE_COLOR = 0xffd24a;
export const OUTLINE_THRESHOLD_DEGREES = 30;
// 縁取り生成の三角形数上限(監督者裁定5)。
export const MAX_OUTLINE_TRIANGLES = 200000;
// 背景クリック/ドラッグ判定の閾値(監督者裁定6)。
const CLICK_MAX_DISTANCE_PX = 5;
const CLICK_MAX_DURATION_MS = 400;

// --- 背景・ライト(GUI改修Task9)に関する定数・純粋関数 ---

// 背景(監督者裁定6): 単色0x1a1a1aから、上が明るく下が暗い縦グラデーションに
// 変更する。天地の手がかりを作るためだが、モデルの色より彩度を持たせると
// 部材のクラス色分けと競合するため無彩色〜わずかな青みに留める。screen-space
// 固定の2Dテクスチャとしてscene.backgroundへ設定するため、カメラを回しても
// グラデーションの上下は画面に対して常に一定になる。
const BACKGROUND_TOP_COLOR = "#3d434c";
const BACKGROUND_BOTTOM_COLOR = "#12131a";

/**
 * 上が明るく下が暗い縦グラデーションのCanvasTextureを作る(scene.background用)。
 * @returns {THREE.CanvasTexture}
 */
function _createBackgroundTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = 2;
  canvas.height = 256;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height);
  gradient.addColorStop(0, BACKGROUND_TOP_COLOR);
  gradient.addColorStop(1, BACKGROUND_BOTTOM_COLOR);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  return new THREE.CanvasTexture(canvas);
}

/**
 * 選択三角形数から縁取り(EdgesGeometry)を作るべきかを判定する。
 * 上限(200,000)を超えたら作らない(コストが選択サイズに比例するため)。
 * @param {number} triangleCount
 * @returns {boolean}
 */
export function shouldOutline(triangleCount) {
  return triangleCount <= MAX_OUTLINE_TRIANGLES;
}

/**
 * pointerdown〜pointerupの移動距離・経過時間から「クリック」か「ドラッグ
 * (視点操作)」かを判定する。5px未満かつ400ms未満のときだけクリックとみなす
 * (監督者裁定6)。dx/dyは符号を問わない(Math.hypotで距離化する)。
 * @param {{dx:number, dy:number, ms:number}} params
 * @returns {boolean}
 */
export function isClickNotDrag({ dx, dy, ms }) {
  return Math.hypot(dx, dy) < CLICK_MAX_DISTANCE_PX && ms < CLICK_MAX_DURATION_MS;
}

/**
 * 選択件数から減光すべきかを判定する。選択0件では減光しない(全要素が通常色)。
 * 1件以上選択されたときだけ非選択要素を減光する(監督者裁定2)。
 * @param {number} selectedCount
 * @returns {boolean}
 */
export function shouldDim(selectedCount) {
  return selectedCount > 0;
}

/**
 * 非選択要素の色を減光する(色task Task5 レビュー Important)。単純な乗算
 * (`* DIM_FACTOR`)だけだと、元の色が既に暗い場合(IFCの実測値には linear 0.07
 * 程度のものが普通にある)に結果が壁の albedo(0.023)と同程度まで沈み、背景と
 * 見分けが付かなくなる。乗算に `DIM_FLOOR` を加算することで、元の明るさに
 * 関わらず必ず一定以上の明るさを残す。選択色には適用しない(呼び出し側で
 * 選択中の要素には呼ばない)。
 * @param {[number,number,number]} rgb
 * @returns {[number,number,number]}
 */
export function dimColor([r, g, b]) {
  return [
    r * DIM_FACTOR + DIM_FLOOR,
    g * DIM_FACTOR + DIM_FLOOR,
    b * DIM_FACTOR + DIM_FLOOR,
  ];
}

/**
 * Three.jsシーンを初期化する。
 * @param {HTMLCanvasElement} canvas
 * @returns {object} viewer インターフェース
 */
export function initViewer(canvas) {
  const scene = new THREE.Scene();
  scene.background = _createBackgroundTexture();

  const camera = new THREE.PerspectiveCamera(
    50,
    canvas.clientWidth / canvas.clientHeight,
    0.01,
    100000
  );
  camera.position.set(10, 10, 10);
  // IFCは+Zが上(仕様)。three.jsの既定は+Y上のため、camera.upを明示する
  // (Z-up修正)。専用カメラコントローラ(cameracontrols.js)のorbitAroundPivotは
  // yawをワールド+Zまわりで回す前提で書かれており、camera.lookAt()もcamera.upを
  // 読んで正しい向き(ロール)を決めるため、この値が(0,0,1)であることが必要
  // (以前はOrbitControls生成前に設定する順序が重要だったが、3Dビュー操作入替で
  // OrbitControlsは撤去した。camera.up自体の必要性そのものは変わらない)。
  camera.up.set(0, 0, 1);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);

  // ステージ(床)の追加でシーンに反射的な明るさが増えるため、既存のAmbientは
  // 0.6->0.45に少し下げてHemisphereLightに明暗差の役割を譲る(監督者裁定7:
  // 「白飛びするなら強度を調整しろ」)。Directionalは既存のまま(0.8)。落ち影は
  // 付けない(コストと利点が釣り合わないという監督者裁定7の判断による)。
  const ambient = new THREE.AmbientLight(0xffffff, 0.45);
  const directional = new THREE.DirectionalLight(0xffffff, 0.8);
  // Z-up修正: この値はY-up前提で選ばれたが、3成分中Zが最大(3)のため、Z-upの
  // 「上」として読んでも仰角53.3°(atan2(3,√(1²+2²)))の斜め上から差す向きを保つ
  // (旧Y-up解釈での仰角32.3°より急だが、どちらも「上から」の範囲)。
  // _fitCameraToMeshの既定カメラ方向(1,1,0.6)を向く面法線との内積は実測0.835
  // (正で大きい=カメラに正対する面が明るく照らされる)。変更しない。
  directional.position.set(1, 2, 3);
  // 上方向(sky)を明るい無彩色、下方向(ground)を暗い無彩色にして、モデル上面と
  // 下面の明暗差を作る(監督者裁定7)。HemisphereLightの上下は「position」が指す
  // 方向で決まり、既定値はTHREE.Object3D.DEFAULT_UP(=+Y)がコンストラクタで
  // コピーされる。IFCはZ-upのためここで明示的に+Zへ差し替える(Z-up修正。忘れると
  // 上下の明暗が横向きになる)。
  const hemisphere = new THREE.HemisphereLight(0xffffff, 0x2b2f33, 0.5);
  hemisphere.position.set(0, 0, 1);
  scene.add(ambient, directional, hemisphere);

  // 専用カメラコントローラ(3Dビュー操作入替 Task2)。OrbitControlsは撤去した
  // (web/vendor/OrbitControls.js自体はthree.jsのベンダリング物として残すが、
  // このファイルからは使わない。理由はdocs/plans/2026-07-28-camera-controls.md
  // 参照)。pickPointには_pickPoint(このファイル内で後述)を渡す。関数宣言は
  // hoistされるため、この時点ではまだ本体を読んでいなくても参照として渡せる
  // (実際に呼ばれるのは右ドラッグのpointerdown時点であり、その頃にはmesh/
  // raycaster/pointerの宣言はすべて実行済み)。
  const controls = createCameraControls({
    camera,
    domElement: renderer.domElement,
    THREE,
    pickPoint: _pickPoint,
  });

  let mesh = null;
  let meta = null;
  let clickCallback = null;
  let backgroundClickCallback = null;
  let outlineObject = null; // 選択の縁取り(THREE.LineSegments)。無ければnull。
  let lastRepaintMs = 0; // repaintColorsの直近実行時間(性能測定用)。
  let stage = null; // buildStageの戻り値{group, dispose()}。モデル未読込ならnull。
  let stageVisible = true; // ステージ表示トグルの現在値(GUI改修Task9 監督者裁定8: 既定は表示)。

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();

  function _resize() {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (width === 0 || height === 0) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", _resize);
  // clientWidth/Height may still be 0 (or stale) at construction time
  // depending on layout timing, so also watch the canvas itself.
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(_resize).observe(canvas);
  }
  _resize();

  // 裁定D4: 新コントローラは減衰(damping)を持たない1:1の直接操作のため、
  // OrbitControls.update()相当の毎フレーム積分処理が不要になった。カメラの
  // 位置・向きはpointermove/wheelのイベントハンドラの中で直接書き換えている
  // ため、ここではrenderer.render()だけで描画は最新の状態を反映する
  // (controls.update()を削除してよい理由)。
  function _animate() {
    renderer.render(scene, camera);
    requestAnimationFrame(_animate);
  }
  requestAnimationFrame(_animate);

  /**
   * メッシュを設定する。既存メッシュがあれば破棄する。
   * @param {object} meshMeta /api/mesh の meta JSON
   * @param {Float32Array} positions
   * @param {Uint32Array} indices
   * @param {string} [colorMode] 初期塗りに使う配色モード(COLOR_MODE_IFC/COLOR_MODE_CLASS)。
   *   未指定ならCOLOR_MODE_IFC(既定は「IFCの色」)。
   */
  function setMesh(meshMeta, positions, indices, colorMode) {
    _disposeOutline(); // 前のモデルの縁取りは新しいモデルでは無意味なので必ず消す。
    _disposeStage(); // 前のモデルのステージ(床・グリッド・軸)も同様に作り直す(GUI改修Task9 監督者裁定9)。
    if (mesh) {
      scene.remove(mesh);
      mesh.geometry.dispose();
      mesh.material.dispose();
      mesh = null;
    }

    meta = meshMeta;

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setIndex(new THREE.BufferAttribute(indices, 1));
    geometry.computeBoundingBox(); // GUI改修Task9: ステージ(床)のサイズ算出に使うAABB。以前は外接球しか求めていなかった。

    const mode = colorMode ?? COLOR_MODE_IFC;
    const colors = new Float32Array(positions.length);
    for (const el of meta.elements) {
      const [r, g, b] = resolveBaseColor(el, { operation: null, colorMode: mode });
      // 要素内は溶接済み頂点のため vertex_start/vertex_count を使う
      // (要素間では頂点は共有されないが、要素内では溶接が普通)。
      const vertexStart = el.vertex_start;
      const vertexCount = el.vertex_count;
      for (let v = 0; v < vertexCount; v++) {
        const idx = (vertexStart + v) * 3;
        colors[idx] = r;
        colors[idx + 1] = g;
        colors[idx + 2] = b;
      }
    }
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));

    // flatShading: geometry に normal 属性を張っていないため必須。法線が無いまま
    // スムーズシェーディングを使うと normalize(vec3(0)) が NaN になり、モデル全体が
    // 真っ黒に潰れる(このフェーズで発見したバグ)。flatShading はフラグメント側で
    // 微分から面法線を作るので normal 属性が要らず、頂点あたり12バイトを節約でき、
    // かつ溶接済み頂点でも直角のエッジが丸まらない。
    const material = new THREE.MeshLambertMaterial({
      vertexColors: true,
      side: THREE.DoubleSide,
      flatShading: true,
    });

    mesh = new THREE.Mesh(geometry, material);
    scene.add(mesh);

    // GUI改修Task9: モデルのAABBから床+グリッド+軸を作る。stage.js側で
    // group及び全ての子にuserData.pickable=falseが立つ(床をレイキャストの対象に
    // しないための保険。現状のraycastはraycaster.intersectObject(mesh, false)で単一
    // メッシュのみを対象にしており、_performClickもmesh以外を一切見ないため、stageを
    // scene.addしてもレイキャストには当たらない。下のonBackgroundClick経由の動作
    // 確認結果はreportに記載)。
    stage = buildStage(geometry.boundingBox);
    stage.group.visible = stageVisible;
    scene.add(stage.group);

    _fitCameraToMesh();
  }

  /** 既存のステージをシーンから外してdisposeする(無ければ何もしない)。 */
  function _disposeStage() {
    if (!stage) return;
    scene.remove(stage.group);
    stage.dispose();
    stage = null;
  }

  /**
   * ステージ(床・グリッド・軸)の表示/非表示を切り替える(GUI改修Task9 監督者
   * 裁定8)。モデル読込前に呼ばれた場合は次回setMesh時に反映される状態だけを保持する。
   * @param {boolean} visible
   */
  function setStageVisible(visible) {
    stageVisible = visible;
    if (stage) stage.group.visible = visible;
  }

  /** ステージの表示状態を返す(検証/デバッグ用途)。 */
  function isStageVisible() {
    return stageVisible;
  }

  function _fitCameraToMesh() {
    if (!mesh) return;
    mesh.geometry.computeBoundingSphere();
    const sphere = mesh.geometry.boundingSphere;
    if (!sphere) return;

    const radius = Math.max(sphere.radius, 0.01);
    // Z-up修正: 水平成分(X・Y)を大きく、仰角成分(Z)を小さくした向き。旧来の
    // Y-up向け(1, 0.6, 1)(第2成分=高さ)から差し替えた。camera.upの(0,0,1)と
    // 平行にならない値を選んでいる(真上/真下から見下ろす向きだと、直後の
    // camera.lookAt(sphere.center)の向き(ロール)が不定になる。3Dビュー操作入替
    // 以前はOrbitControlsの内部球面座標変換が破綻する、という同趣旨の理由だった)。
    const direction = new THREE.Vector3(1, 1, 0.6).normalize();
    const distance = radius / Math.sin((camera.fov * Math.PI) / 360);

    camera.position.copy(sphere.center).addScaledVector(direction, distance * 1.2);
    camera.near = Math.max(distance / 1000, 0.001);
    camera.far = distance * 1000;
    camera.updateProjectionMatrix();

    // OrbitControls.update()が毎フレームやっていた「target を向く」処理を、
    // ここで明示的に行う(新コントローラはlookAtを強制しないため、この
    // 呼び出し側の責務になった)。
    camera.lookAt(sphere.center);
    controls.setFocus(sphere.center, camera.position.distanceTo(sphere.center));
  }

  /**
   * 選択状態を反映して全要素の色を1パスで塗り直す(GUI改修 Task8)。増分更新は
   * しない(前回選択との差分だけ塗る旧方式は減光と両立しないため廃止した)。
   * 決定順: 選択中 → 選択色(SELECTION_COLOR)。そうでなければ resolveBaseColor
   * (有効操作があればステータス色、無ければ colorMode に応じてIFCの色/クラス色)。
   * dim===trueのときは非選択の要素のみdimColor()で減光する(選択色は常に
   * フル輝度のまま)。color属性へのneedsUpdate=trueは1回だけ立てる。
   * @param {{selectedGids: Set<string>, effectiveOps: Map<string, object>, dim: boolean, colorMode: string}} params
   */
  function repaintColors({ selectedGids, effectiveOps, dim, colorMode } = {}) {
    if (!mesh || !meta) return;
    const t0 = performance.now();

    const selected = selectedGids ?? new Set();
    const ops = effectiveOps ?? new Map();
    const mode = colorMode ?? COLOR_MODE_IFC;
    const colorAttr = mesh.geometry.getAttribute("color");
    const array = colorAttr.array;

    for (const el of meta.elements) {
      let r, g, b;
      if (selected.has(el.global_id)) {
        [r, g, b] = SELECTION_COLOR;
      } else {
        [r, g, b] = resolveBaseColor(el, { operation: ops.get(el.global_id), colorMode: mode });
        if (dim) {
          [r, g, b] = dimColor([r, g, b]);
        }
      }
      const vertexStart = el.vertex_start;
      const vertexCount = el.vertex_count;
      for (let v = 0; v < vertexCount; v++) {
        const idx = (vertexStart + v) * 3;
        array[idx] = r;
        array[idx + 1] = g;
        array[idx + 2] = b;
      }
    }
    colorAttr.needsUpdate = true;
    lastRepaintMs = performance.now() - t0;
  }

  /** repaintColorsの直近実行時間をms単位で返す(性能実測用途)。 */
  function getLastRepaintMs() {
    return lastRepaintMs;
  }

  /** 既存の縁取りをシーンから外してdisposeする(無ければ何もしない)。 */
  function _disposeOutline() {
    if (!outlineObject) return;
    scene.remove(outlineObject);
    outlineObject.geometry.dispose();
    outlineObject.material.dispose();
    outlineObject = null;
  }

  /**
   * 選択要素のみの縁取り(EdgesGeometry→LineSegments)を作り直す(GUI改修 Task8)。
   * 前回のものは必ずdisposeしてから差し替える。elementsがnull/空配列なら
   * 縁取りを消すだけでtrueを返す。選択三角形数がMAX_OUTLINE_TRIANGLESを
   * 超える場合は作らずfalseを返す(呼び出し側が省略表示を出す)。
   * @param {Array<object>|null} elements meta.elements のうち選択中のもの
   * @returns {boolean} 縁取りを作った(または作る必要が無かった)ならtrue、
   *   サイズ上限超過のため省略したならfalse。
   */
  function setSelectionOutline(elements) {
    _disposeOutline();
    if (!mesh || !elements || elements.length === 0) return true;

    let totalTriangles = 0;
    for (const el of elements) totalTriangles += el.tri_count;
    if (!shouldOutline(totalTriangles)) return false;

    const srcIndex = mesh.geometry.getIndex();
    if (!srcIndex) return true; // setMeshは常にindexed geometryを作るため通常は到達しない。

    let indexLength = 0;
    for (const el of elements) indexLength += el.tri_count * 3;
    const selectionIndices = new Uint32Array(indexLength);
    let offset = 0;
    for (const el of elements) {
      const start = el.tri_start * 3;
      const count = el.tri_count * 3;
      selectionIndices.set(srcIndex.array.subarray(start, start + count), offset);
      offset += count;
    }

    // positionは元メッシュの配列をそのまま共有する(コピーしない)。EdgesGeometryは
    // 与えたindexが参照する頂点だけを読むため、他要素の頂点が混在していても
    // 無害(参照されないだけ)。mesh.geometry自体は変更しないため、
    // 描画中のメインメッシュに影響しない。
    const sourceGeometry = new THREE.BufferGeometry();
    sourceGeometry.setAttribute(
      "position",
      new THREE.BufferAttribute(mesh.geometry.getAttribute("position").array, 3)
    );
    sourceGeometry.setIndex(new THREE.BufferAttribute(selectionIndices, 1));

    const edgesGeometry = new THREE.EdgesGeometry(sourceGeometry, OUTLINE_THRESHOLD_DEGREES);
    const material = new THREE.LineBasicMaterial({ color: OUTLINE_COLOR, depthTest: true });
    outlineObject = new THREE.LineSegments(edgesGeometry, material);
    scene.add(outlineObject);
    return true;
  }

  /** 縁取りが現在存在するかを返す(検証/デバッグ用途)。 */
  function hasOutline() {
    return outlineObject !== null;
  }

  /** シーン自体を返す(検証/デバッグ用途。scene.childrenの確認等に使う)。 */
  function getScene() {
    return scene;
  }

  /**
   * クリックされた三角形のindexを受け取るコールバックを登録する。
   * raycastはviewer内部で行う。
   * @param {(triIndex: number) => void} callback
   */
  function onTriangleClick(callback) {
    clickCallback = callback;
  }

  /**
   * 何にも当たらないクリック(ドラッグでない)で発火するコールバックを登録する
   * (GUI改修 Task8: 選択解除の経路2「背景クリック」)。
   * @param {() => void} callback
   */
  function onBackgroundClick(callback) {
    backgroundClickCallback = callback;
  }

  /**
   * client座標(clientX/clientY)からメッシュへのraycastを行う共通処理。
   * _performClick(クリック判定)とpickPoint(cameracontrols.jsへ渡す、右ドラッグの
   * 回転軸決定に使う)の両方がここを通る(raycast処理そのものを重複させない)。
   * @param {number} clientX
   * @param {number} clientY
   * @returns {THREE.Intersection|null} 最も近い交点。当たらなければnull。
   */
  function _raycastAtClient(clientX, clientY) {
    if (!mesh) return null;
    // raycaster.setFromCamera は camera.matrixWorld を直接読むだけで、更新はしない
    // (web/vendor/three.module.js の Raycaster.setFromCamera 実装を確認済み)。
    // matrixWorld は通常 renderer.render() 内で毎フレーム更新されるが、タブが
    // バックグラウンド扱いになる環境では requestAnimationFrame が止まる/間引かれ、
    // _fitCameraToMesh 等でcamera.position/quaternionを直接書き換えた直後に
    // まだ古いmatrixWorldのままraycastされることがある(実測で確認したレビュー
    // Important。3Dビュー操作入替Task2)。描画ループの実行タイミングに依存させず
    // ここで明示的に更新することで、いつ呼ばれても正しい結果を返すようにする。
    camera.updateMatrixWorld();
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;

    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObject(mesh, false);
    return hits.length > 0 ? hits[0] : null;
  }

  /**
   * pointerdown〜pointerupがクリックと判定された場合のみ呼ばれる。raycastして
   * 要素に当たればclickCallback、当たらなければbackgroundClickCallbackを呼ぶ。
   * @param {PointerEvent} event
   */
  function _performClick(event) {
    if (!mesh) return;
    const hit = _raycastAtClient(event.clientX, event.clientY);
    if (hit && hit.faceIndex !== undefined) {
      if (clickCallback) clickCallback(hit.faceIndex);
    } else if (backgroundClickCallback) {
      backgroundClickCallback();
    }
  }

  /**
   * カーソル直下のモデル表面点を返す(cameracontrols.jsのpickPoint。右ドラッグの
   * 回転軸決定(裁定D1)に使う)。_performClickと同じ_raycastAtClientを使うため、
   * raycast処理そのものは重複しない。
   * @param {number} clientX
   * @param {number} clientY
   * @returns {THREE.Vector3|null}
   */
  function _pickPoint(clientX, clientY) {
    const hit = _raycastAtClient(clientX, clientY);
    return hit ? hit.point.clone() : null;
  }

  // ドラッグ(視点操作)とクリックを区別する(GUI改修 Task8 監督者裁定6)。
  // 従来のclickイベント無条件購読は、視点を回してマウスを離しただけでも
  // クリック扱いになってしまうため置き換える。pointerdownは対象要素の
  // ボタン0(左クリック)のみ追跡し、pointerupはwindowで拾う(キャンバス外で
  // 離される高速ドラッグでも取り逃さないため)。
  let pointerDownInfo = null; // {x, y, t, pointerId} | null

  renderer.domElement.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    pointerDownInfo = {
      x: event.clientX,
      y: event.clientY,
      t: performance.now(),
      pointerId: event.pointerId,
    };
  });

  window.addEventListener("pointerup", (event) => {
    if (!pointerDownInfo || event.pointerId !== pointerDownInfo.pointerId) return;
    const dx = event.clientX - pointerDownInfo.x;
    const dy = event.clientY - pointerDownInfo.y;
    const ms = performance.now() - pointerDownInfo.t;
    pointerDownInfo = null;
    if (!isClickNotDrag({ dx, dy, ms })) return;
    _performClick(event);
  });

  // 中断された操作の記録を残さない。pointercancel を無視すると
  // pointerDownInfo が残り、同じ pointerId の pointerup が遅れて届いたときに
  // 「キャンセル済みの押下」の座標と時刻でクリック判定が走る——実測では
  // それだけで選択が全解除された(Task 8 レビュー Important)。ブラウザは
  // タッチのパンやペンの拒否、OSのジェスチャ横取りで pointercancel を出す。
  window.addEventListener("pointercancel", (event) => {
    if (pointerDownInfo && event.pointerId === pointerDownInfo.pointerId) {
      pointerDownInfo = null;
    }
  });

  /**
   * 現在の頂点色配列のコピーを返す(検証/デバッグ用途)。
   * @returns {Float32Array|null}
   */
  function getColorArray() {
    if (!mesh) return null;
    return mesh.geometry.getAttribute("color").array.slice();
  }

  /**
   * 現在のcamera.upを返す(検証/デバッグ用途。Z-up修正の番人アサーション
   * `?selftest=1` から、うっかりY-upに戻す変更が入ったら赤くなるようにするために
   * 追加した最小限のgetter)。
   * @returns {THREE.Vector3}
   */
  function getCameraUp() {
    return camera.up.clone();
  }

  /**
   * カメラの現在状態を返す(検証/デバッグ用途。3Dビュー操作入替Task2 Step3の
   * 実機確認で使う)。
   * @returns {{position: THREE.Vector3, quaternion: THREE.Quaternion,
   *   focus: THREE.Vector3, focusDistance: number}}
   */
  function getCameraState() {
    const { point, distance } = controls.getFocus();
    return {
      position: camera.position.clone(),
      quaternion: camera.quaternion.clone(),
      focus: point,
      focusDistance: distance,
    };
  }

  return {
    setMesh,
    repaintColors,
    setSelectionOutline,
    onTriangleClick,
    onBackgroundClick,
    getColorArray,
    getLastRepaintMs,
    hasOutline,
    getScene,
    setStageVisible,
    isStageVisible,
    getCameraUp,
    getCameraState,
  };
}
