// stage.js -- ステージ(床 + 背面2枚の壁 + グリッド + 軸)の生成。GUI改修 Task9。
//
// three.jsのimportはこのファイル内に閉じ込める。viewer.jsはbuildStage()を呼んで
// 返り値のgroupをscene.add/removeするだけで、stageの内部構造(床・壁・グリッド・軸
// それぞれのジオメトリ/マテリアル)には関与しない。
//
// 生成した全オブジェクト(group自体および各子オブジェクト)にuserData.pickable=falseを
// 立てる。現在のraycastはraycaster.intersectObject(mesh, false)で単一メッシュのみを
// 対象にしているため実質的にはまだ効果を持たないが、将来raycastの対象がscene全体の
// 走査に広がった場合の保険として立てておく(viewer.js側での現状確認は別途report済み)。

import * as THREE from "./vendor/three.module.js";

const DEFAULT_MARGIN = 0.15;

// 床・壁の色(監督者裁定7: 無彩色〜わずかな青みまでに留め、部材のクラス色分けを
// 邪魔しない彩度ゼロ付近の色)。床は背景よりわずかに明るく、壁は床よりわずかに
// 暗く奥へ退くように選んでいる。
const FLOOR_COLOR = 0x35383d;
const WALL_COLOR = 0x2a2d31;
const GRID_COLOR_CENTER = 0x53575c;
const GRID_COLOR_LINES = 0x3d4045;

const AXES_LENGTH_RATIO = 1 / 10; // 軸の長さ = モデル最大辺の1/10
const GRID_STEP_DIVISOR = 20; // グリッド分割幅の目安 = モデル最大辺の1/20

// 分割幅の丸め先(1,2,5,10の系列。10^n倍して使う)。roundToNiceStepが返す値は
// 常にこの4つのいずれかに10の整数乗を掛けた値になる。
const NICE_FRACTION_1 = 1;
const NICE_FRACTION_2 = 2;
const NICE_FRACTION_5 = 5;
const NICE_FRACTION_10 = 10;

/**
 * 正の数値を「切りのいい値」(1,2,5,10,20,50,100...の系列。10進の桁ごとに
 * 1/2/5/10のいずれかへスナップ)に丸める。10進の指数(桁)を先に切り出すため、
 * 値が1未満(m単位の小さいモデル)でも数万〜数十万(mm単位のモデル)でも同じ規則
 * で破綻なく動く(GUI改修Task9 監督者裁定5)。0以下・非有限値は1にフォール
 * バックする。
 * @param {number} value
 * @returns {number}
 */
export function roundToNiceStep(value) {
  if (!(value > 0) || !Number.isFinite(value)) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = Math.pow(10, exponent);
  const fraction = value / base;
  let niceFraction;
  if (fraction < 1.5) niceFraction = NICE_FRACTION_1;
  else if (fraction < 3.5) niceFraction = NICE_FRACTION_2;
  else if (fraction < 7.5) niceFraction = NICE_FRACTION_5;
  else niceFraction = NICE_FRACTION_10;
  return niceFraction * base;
}

/**
 * モデルのAABBから、床+グリッド+背面2枚の壁+軸のGroupを作る(GUI改修Task9)。
 *
 * 壁は最小X面・最小Z面の2枚(「背面2枚」)。viewer.jsの_fitCameraToMeshが既定で
 * カメラを中心から見て+X/+Y/+Zの方向へ置くため、その対角にあたる最小X/最小Z側を
 * 「モデルの奥」とみなし、そこに壁を立てる。壁の法線はモデル側(+X/+Z方向)へ
 * 向け、material.side=THREE.FrontSideにする。これにより:
 *   - カメラがモデル側(+X/+Zの外側、既定のカメラ位置を含む)にあるときは壁の
 *     前面が描画され、モデルの背景として機能する。
 *   - カメラが壁の外側(-X/-Z側)まで回り込むと、法線が逆側を向く壁の裏側になり
 *     FrontSideは描画しない(=透過してモデルが見える)。
 *
 * @param {THREE.Box3} boundingBox モデルのAABB(ワールド座標)
 * @param {{margin?: number}} [options] marginはAABBの各辺に対する比率(既定0.15)。
 *   床・壁・グリッドの水平方向の広がり、および壁の高さの上端に適用する。
 *   床のY座標そのものにはmarginを適用しない(裁定4: モデルの下端に正確に一致させ、
 *   浮いたりめり込んだりしないようにする)。
 * @returns {{group: THREE.Group, dispose(): void}}
 */
export function buildStage(boundingBox, { margin = DEFAULT_MARGIN } = {}) {
  const size = new THREE.Vector3();
  boundingBox.getSize(size);

  // 平面/点しかない退化モデル(いずれかの辺のサイズが0)でも0除算等で破綻しない
  // よう下駄を履かせる。あわせて**非有限値を必ず潰す**: 壊れたIFCの頂点に
  // Infinity/NaN が混じると AABB もそうなり、そのまま GridHelper へ渡すと
  // `RangeError: Invalid array length` を投げてモデル読込全体が止まる
  // (setMesh は buildStage を try/catch 無しで同期呼び出しする)。
  // ステージは飾りなので、壊れた寸法のときは無難な大きさで描いて
  // モデル表示を生かす方を選ぶ(Task 9 レビュー Important)。
  const EPS = 1e-6;
  const FALLBACK_DIM = 1;
  const finiteOr = (v, fallback) => (Number.isFinite(v) ? v : fallback);
  const safeSize = new THREE.Vector3(
    Math.max(finiteOr(size.x, FALLBACK_DIM), EPS),
    Math.max(finiteOr(size.y, FALLBACK_DIM), EPS),
    Math.max(finiteOr(size.z, FALLBACK_DIM), EPS)
  );
  const maxDim = Math.max(safeSize.x, safeSize.y, safeSize.z);
  // margin も呼び出し側の値なので同じく信用しない(負値は床が反転し、
  // Infinity/NaN はグリッドの分割数を壊す)。
  const safeMargin = Number.isFinite(margin) && margin >= 0 ? margin : DEFAULT_MARGIN;

  const padX = safeSize.x * safeMargin;
  const padY = safeSize.y * safeMargin;
  const padZ = safeSize.z * safeMargin;

  // AABB の座標そのものが非有限のときは原点に寄せる(上の safeSize と同じ理由)。
  const originX = finiteOr(boundingBox.min.x, 0);
  const originY = finiteOr(boundingBox.min.y, 0);
  const originZ = finiteOr(boundingBox.min.z, 0);
  const farX = finiteOr(boundingBox.max.x, originX + safeSize.x);
  const farZ = finiteOr(boundingBox.max.z, originZ + safeSize.z);

  const floorY = originY; // 裁定4: marginを適用せず、AABBの最小Yに正確に一致させる。
  const minX = originX - padX;
  const maxX = farX + padX;
  const minZ = originZ - padZ;
  const maxZ = farZ + padZ;
  const topY = finiteOr(boundingBox.max.y, originY + safeSize.y) + padY;

  const footprintWidthX = maxX - minX; // 床のX方向の幅(壁Zの幅もこれに揃える)
  const footprintDepthZ = maxZ - minZ; // 床のZ方向の幅(壁Xの幅もこれに揃える)
  const wallHeight = Math.max(topY - floorY, EPS);
  const centerX = (minX + maxX) / 2;
  const centerZ = (minZ + maxZ) / 2;

  const group = new THREE.Group();
  group.name = "stage";
  group.userData.pickable = false;

  // --- 床 ---
  const floorGeometry = new THREE.PlaneGeometry(footprintWidthX, footprintDepthZ);
  const floorMaterial = new THREE.MeshLambertMaterial({
    color: FLOOR_COLOR,
    side: THREE.DoubleSide, // カメラが床下に回り込んでも消えないようにする(壁とは要件が異なる)。
  });
  const floor = new THREE.Mesh(floorGeometry, floorMaterial);
  floor.name = "stage-floor";
  floor.rotation.x = -Math.PI / 2; // 既定はXY平面(法線+Z) -> XZ平面(法線+Y、上向き)に直す。
  floor.position.set(centerX, floorY, centerZ);
  floor.userData.pickable = false;
  group.add(floor);

  // --- グリッド ---
  const gridStep = roundToNiceStep(maxDim / GRID_STEP_DIVISOR);
  const gridExtent = Math.max(footprintWidthX, footprintDepthZ);
  const gridDivisions = Math.max(1, Math.round(gridExtent / gridStep));
  const gridSize = gridDivisions * gridStep;
  const grid = new THREE.GridHelper(gridSize, gridDivisions, GRID_COLOR_CENTER, GRID_COLOR_LINES);
  grid.name = "stage-grid";
  // 床と厳密に同一平面だとz-fightingが起きるため、モデル規模に対する相対値で
  // ごく僅かに浮かせる(絶対値だとmm単位モデルで消え、m単位モデルで浮きすぎる)。
  grid.position.set(centerX, floorY + maxDim * 0.0005, centerZ);
  grid.userData.pickable = false;
  group.add(grid);

  // --- 壁2枚(背面。side:FrontSide、法線をモデル側へ) ---
  const wallMaterial = new THREE.MeshLambertMaterial({
    color: WALL_COLOR,
    side: THREE.FrontSide,
  });

  // 壁Z: 最小Z面。PlaneGeometryの既定の法線(+Z)がそのままモデル側を向くため回転不要。
  const wallZGeometry = new THREE.PlaneGeometry(footprintWidthX, wallHeight);
  const wallZ = new THREE.Mesh(wallZGeometry, wallMaterial);
  wallZ.name = "stage-wall-z";
  wallZ.position.set(centerX, floorY + wallHeight / 2, minZ);
  wallZ.userData.pickable = false;
  group.add(wallZ);

  // 壁X: 最小X面。Y軸周りに+90度回すと法線が+Z->+Xになり、モデル側を向く。
  const wallXGeometry = new THREE.PlaneGeometry(footprintDepthZ, wallHeight);
  const wallX = new THREE.Mesh(wallXGeometry, wallMaterial); // wallZと同一マテリアルを共有(描画コスト削減)。
  wallX.name = "stage-wall-x";
  wallX.rotation.y = Math.PI / 2;
  wallX.position.set(minX, floorY + wallHeight / 2, centerZ);
  wallX.userData.pickable = false;
  group.add(wallX);

  // --- 軸(ワールド原点。モデル中心ではない) ---
  const axes = new THREE.AxesHelper(maxDim * AXES_LENGTH_RATIO);
  axes.name = "stage-axes";
  axes.position.set(0, 0, 0);
  axes.userData.pickable = false;
  group.add(axes);

  function dispose() {
    floorGeometry.dispose();
    floorMaterial.dispose();
    grid.geometry.dispose();
    grid.material.dispose();
    wallZGeometry.dispose();
    wallXGeometry.dispose();
    wallMaterial.dispose(); // wallZ/wallXで共有しているため一度だけdisposeする。
    axes.geometry.dispose();
    axes.material.dispose();
  }

  return { group, dispose };
}
