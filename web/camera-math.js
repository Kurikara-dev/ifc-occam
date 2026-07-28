// camera-math.js -- カメラ操作の数式だけを集めた純粋関数モジュール。
// DOM にも three.js の import にも依存しない(THREE は引数で受け取る)。
// ?selftest=1 から直接呼んで数値で固定できるようにするための分離。

// マウスボタン -> 操作の割り当て(ユーザー指定, 2026-07-28)。
//   0(左)   : 視点移動(カメラ位置は動かさない)
//   1(中/ホイール): パン
//   2(右)   : 回転(カーソル直下の点が軸)
export const DRAG_ACTION_BY_BUTTON = { 0: "look", 1: "pan", 2: "orbit" };

// 極角(ワールド+Zからの角度)の可動範囲。真上・真下でカメラの向きが
// 不定になるのを防ぐため、両端をわずかに残す。
export const MIN_POLAR = 0.001;
export const MAX_POLAR = Math.PI - 0.001;

/**
 * マウスボタン番号から操作を決める。割り当ての無いボタンは null。
 * @param {number} button PointerEvent.button
 * @returns {"look"|"orbit"|"pan"|null}
 */
export function resolveDragAction(button) {
  return DRAG_ACTION_BY_BUTTON[button] ?? null;
}

/**
 * 極角の変化量を可動範囲に収める。範囲外へ出る分は切り詰める
 * (打ち切るのではなく、境界にぴたりと着地させる)。
 * @param {number} currentPolar 現在の極角(rad, ワールド+Zからの角度)
 * @param {number} delta 加えたい変化量(rad)
 * @param {number} [minPolar]
 * @param {number} [maxPolar]
 * @returns {number} 実際に加えてよい変化量
 */
export function clampPolarDelta(currentPolar, delta, minPolar = MIN_POLAR, maxPolar = MAX_POLAR) {
  const next = Math.min(Math.max(currentPolar + delta, minPolar), maxPolar);
  return next - currentPolar;
}

/**
 * 任意の点 pivot を軸にカメラを回す。カメラの位置と向きの**両方**に同じ回転を
 * 掛けるため、pivot が視軸から外れていても見え方が飛ばない
 * (OrbitControls は位置を target から導出し lookAt(target) を強制するため
 * これができない。それがこのモジュールを書いた理由)。
 *
 * yaw はワールド+Z(IFCの上方向)まわり、pitch はカメラの右軸まわり。
 *
 * @param {{position, quaternion, pivot, yaw, pitch, THREE}} params
 *   position/quaternion/pivot は変更しない(新しい値を返す)。
 * @returns {{position: THREE.Vector3, quaternion: THREE.Quaternion}}
 */
export function orbitAroundPivot({ position, quaternion, pivot, yaw, pitch, THREE }) {
  const right = new THREE.Vector3(1, 0, 0).applyQuaternion(quaternion).normalize();
  const qYaw = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), -yaw);
  const qPitch = new THREE.Quaternion().setFromAxisAngle(right, -pitch);
  const q = qYaw.multiply(qPitch); // pitch を先に、次に yaw

  const offset = new THREE.Vector3().subVectors(position, pivot).applyQuaternion(q);
  return {
    position: new THREE.Vector3().addVectors(pivot, offset),
    quaternion: q.clone().multiply(quaternion),
  };
}

/**
 * 注視距離の面において、画面1ピクセルが何ワールド単位に相当するかを返す。
 * パン量の換算に使う(ドラッグした分だけ、その奥行きの物が指に付いてくる)。
 * @param {number} focusDistance カメラから注視点までの距離
 * @param {number} fovDegrees PerspectiveCamera.fov(垂直画角, degree)
 * @param {number} viewportHeightPx
 * @returns {number}
 */
export function worldUnitsPerPixel(focusDistance, fovDegrees, viewportHeightPx) {
  if (!(viewportHeightPx > 0)) return 0;
  const halfHeight = focusDistance * Math.tan((fovDegrees * Math.PI) / 360);
  return (2 * halfHeight) / viewportHeightPx;
}
