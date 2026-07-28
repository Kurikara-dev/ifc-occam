// cameracontrols.js -- 専用カメラコントローラ。OrbitControlsの置き換え(3Dビュー操作入替)。
// マウス操作(左ドラッグ=視点移動/中ドラッグ=パン/右ドラッグ=カーソル位置を軸にした回転/
// ホイール=拡大縮小)をcamera-math.jsの純粋関数で計算し、three.jsのカメラへ直接反映する。
// OrbitControlsと異なり毎フレームのupdate()は無い(裁定D4: 減衰なしの1:1直接操作。
// 理由はweb/viewer.jsの_animate直上のコメントを参照)。

import {
  resolveDragAction,
  clampPolarDelta,
  orbitAroundPivot,
  worldUnitsPerPixel,
} from "./camera-math.js";

// 感度: 画面の高さいっぱい(clientHeight px)のドラッグでおよそ180度(πラジアン)回る
// 程度を目安にした。three.js本体のOrbitControls(web/vendor/OrbitControls.js:689
// `rotateLeft(2*Math.PI*rotateDelta.x/element.clientHeight)`)は「高さいっぱいで360度」
// だったが、軸がカーソル直下(画面中央でないことが多い)になり同じ角度でも振れて
// 見えやすくなるため、半分の感度にした。横方向(yaw)・縦方向(pitch)ともに
// clientHeightで正規化する(OrbitControls同様、アスペクト比で感度がぶれないように
// するため。同ファイルの"yes, height"というコメントと同じ考え方)。
const RADIANS_PER_FULL_HEIGHT_DRAG = Math.PI;

// 右ドラッグ(orbit)のyaw符号。監督者が実機で測って確定した(small.ifc、30px右ドラッグ):
//   ピボット自身        : 画面上 0.0000px 移動(=視界が飛ばない)
//   ピボットより手前の点: +50.2px(右) ... ドラッグに付いてくる
//   ピボットより奥の点  : -24.2px(左) ... 逆に流れる
// ターンテーブルを手前から掴んで回すのと同じで、手前側が指に付いてくるのが正しい。
//
// 注意: 観測点の選び方を誤ると符号を読み違える。ピボットと同じ奥行きの点は
// ほとんど横に動かない(回転の主成分が奥行き方向になる)ため、そこを測ると
// 符号がノイズに埋もれる。実際、当初の検証はモデルの重心を観測点にしていたが、
// この重心はピボットとほぼ同じ奥行き(45.35 対 45.46)にあったため結果が
// 安定せず、「大角度で逆転する」という誤った説明に至っていた。
// 符号を測り直すときは必ず「ピボットより明確に手前の点」を観測すること。
const ORBIT_YAW_SIGN = 1;
// 左ドラッグ(look=視点移動)のyaw符号。位置を動かさないぶんorbitとは向きの感覚が
// 異なるため別の定数として持つ。標準的なマウスルック(FPS等)と同じ
// 「右へドラッグしたら右を向く」を採用した(実機確認・判断根拠は報告書参照)。
const LOOK_YAW_SIGN = 1;

// ホイール1ノッチ(=deltaY 100相当)あたりの注視距離の変化率。OrbitControls既定の
// getZoomScale (0.95^(|delta|/100)、web/vendor/OrbitControls.js:495-503)と同じ式を
// 踏襲し、既存の拡大縮小の手触りをできるだけ保つ(裁定D5: 拡大縮小は現行と同じ
// 視軸方向)。trackpad等の小さいdeltaYが連続する入力でも急に感じられないための
// 配慮でもある(deltaYの絶対値に比例した指数にする=1ノッチが大きいほど大きく動く)。
const WHEEL_ZOOM_BASE = 0.95;
// 注視距離の下限。0や負になるとworldUnitsPerPixel(パン換算)が0除算/符号反転する
// ため、視線方向へドリーする際はここで頭打ちにする。
const MIN_FOCUS_DISTANCE = 0.01;

/**
 * 専用カメラコントローラを作る。
 * @param {{camera: THREE.PerspectiveCamera, domElement: HTMLElement, THREE: object,
 *   pickPoint: (clientX:number, clientY:number) => (THREE.Vector3|null)}} params
 * @returns {{setFocus: (point, distance) => void, getFocus: () => {point, distance}, dispose: () => void}}
 */
export function createCameraControls({ camera, domElement, THREE, pickPoint }) {
  // focus/focusDistance: 「今見ている対象」の位置と距離(裁定D7)。パン・ドリーの
  // 刻み幅の基準になり、右ドラッグで軸を取り直すたびに更新される。読込直後は
  // viewer.js側の_fitCameraToMeshがsetFocusで外接球の中心に置き換える。
  const focus = new THREE.Vector3(0, 0, 0);
  let focusDistance = Math.max(camera.position.distanceTo(focus), MIN_FOCUS_DISTANCE);

  // 極角(ワールド+Zから見た視線方向の角度)。camera-math.jsのclampPolarDeltaで
  // 可動範囲を制限する対象そのもの。three.jsのQuaternionにはこの角度を直接問い合わせる
  // 手段が無いため、OrbitControlsのspherical.phiと同じ考え方で「コントローラ側が
  // 追跡する状態」として持つ(pitchを適用するたびに同じ量だけ加算/減算する。
  // 詳細は_applyRotationのコメントを参照)。
  let currentPolar = _polarAngleFromQuaternion(camera.quaternion, THREE);

  let activeAction = null; // "look" | "orbit" | "pan" | null(ドラッグしていない)
  let activePointerId = null;
  let lastClientX = 0;
  let lastClientY = 0;
  let orbitPivot = null; // アクティブなorbitドラッグ中だけ非null(裁定D3: pointerdown時に1回だけ決める)

  function _forward() {
    return new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion).normalize();
  }
  function _right() {
    return new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion).normalize();
  }
  function _localUp() {
    return new THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion).normalize();
  }

  /** 画面の高さから、1ピクセルあたりの回転角(rad)を求める。 */
  function _radiansPerPixel() {
    const height = domElement.clientHeight || 1;
    return RADIANS_PER_FULL_HEIGHT_DRAG / height;
  }

  /**
   * pivotを軸にカメラを回す(orbitAroundPivotの薄いラッパ)。orbit(pivot=クリックした
   * 点)・look(pivot=camera.position)の両方がこれを通る。
   *
   * look(pivot=camera.position)でカメラ位置が動かない理由: orbitAroundPivotは
   * offset=position-pivotを回転してからpivotに足し直す(position'=pivot+R*offset)。
   * position===pivotのときoffsetは常に(0,0,0)なので、回転してもpivot+0=pivotの
   * ままposition'=positionになる。一方quaternionは常にq(=yaw分×pitch分の回転)を
   * 掛けた値になるので、向きだけが変わる。「pivotを中心に位置と向きを同じ回転で
   * 回す」という1つの関数だけで、orbit(位置も動く)とlook(位置は不動)の両方を
   * 特別扱いのコードなしに表現できる——これがorbitAroundPivotをこの形にした理由。
   *
   * @param {THREE.Vector3} pivot
   * @param {number} dx 直前フレームからのポインタ移動量(px, 右が正)
   * @param {number} dy 直前フレームからのポインタ移動量(px, 下が正)
   * @param {number} yawSign +1 か -1(orbitとlookで別の符号を使う。定数の項参照)
   */
  function _applyRotation(pivot, dx, dy, yawSign) {
    const radiansPerPixel = _radiansPerPixel();
    const desiredYaw = yawSign * radiansPerPixel * dx;
    // pitchはdyと同じ符号(下にドラッグ=下を向く)。極角の可動範囲は
    // camera-math.jsの既定(MIN_POLAR/MAX_POLAR)にそのまま従う。
    const desiredPitch = radiansPerPixel * dy;
    const pitch = clampPolarDelta(currentPolar, desiredPitch);

    const result = orbitAroundPivot({
      position: camera.position,
      quaternion: camera.quaternion,
      pivot,
      yaw: desiredYaw,
      pitch,
      THREE,
    });
    camera.position.copy(result.position);
    camera.quaternion.copy(result.quaternion).normalize();
    currentPolar += pitch;
  }

  /**
   * 中ドラッグ(パン)。カメラの向きは変えず、右軸・上軸方向へ平行移動する
   * (worldUnitsPerPixelで画素をワールド単位に換算)。注視点も同じ量だけ動かす
   * (要件: 注視点も同じ量だけ動かす)。
   * @param {number} dx
   * @param {number} dy
   */
  function _applyPan(dx, dy) {
    const wupp = worldUnitsPerPixel(focusDistance, camera.fov, domElement.clientHeight || 1);
    const right = _right();
    const up = _localUp();
    // 画面を右にドラッグ=カメラは左へ動く(内容が指に付いてくるように見える)。
    // 画面を下にドラッグ=カメラは上へ動く(同様)。標準的な「掴んで動かす」パンの向き。
    const delta = new THREE.Vector3()
      .addScaledVector(right, -dx * wupp)
      .addScaledVector(up, dy * wupp);
    camera.position.add(delta);
    focus.add(delta);
  }

  /**
   * ホイール1回分のドリー(視線方向への前後移動、裁定D5)。1ノッチあたりの倍率は
   * 現在の注視距離に対する比で決まる(遠くでは大きく、近くでは小さく)。
   * 注視距離はMIN_FOCUS_DISTANCE未満にならないよう下限で止める。
   * @param {number} deltaY WheelEvent.deltaY(負=手前にスクロール=近付く)
   */
  function _applyDolly(deltaY) {
    const zoomScale = Math.pow(WHEEL_ZOOM_BASE, Math.abs(deltaY) / 100);
    const rawDistance = deltaY < 0 ? focusDistance * zoomScale : focusDistance / zoomScale;
    const newDistance = Math.max(rawDistance, MIN_FOCUS_DISTANCE);
    const moveAmount = focusDistance - newDistance; // 正=前進(近付く)
    camera.position.addScaledVector(_forward(), moveAmount);
    focusDistance = newDistance;
  }

  /**
   * 右ドラッグ開始時に軸を1回だけ決める(裁定D3)。pickPointが点を返せばそれ
   * (裁定D1)、nullなら現在の注視距離だけ視線方向に進んだ点(裁定D2)。
   * @param {number} clientX
   * @param {number} clientY
   * @returns {THREE.Vector3}
   */
  function _resolveOrbitPivot(clientX, clientY) {
    const picked = pickPoint ? pickPoint(clientX, clientY) : null;
    if (picked) return picked;
    return camera.position.clone().addScaledVector(_forward(), focusDistance);
  }

  function _onPointerDown(event) {
    // タッチは受け付けない(裁定D6: 専用コントローラへの差し替えでタッチ操作は
    // 落とした)。弾かないと、指1本のタッチが button=0 として扱われて
    // 「視点だけ首を振る」半端な状態になり、非対応と言いながら中途半端に動く
    // (レビュー指摘)。ペンはマウス相当なので通す。
    if (event.pointerType === "touch") return;

    const action = resolveDragAction(event.button);
    if (!action || activeAction) return; // 未割当ボタン、または既に別の操作中は無視。

    // 中ボタンの既定動作(Windowsのオートスクロール)を止める。mousedown側にも
    // 同じ処理を持つ理由は下の_onMouseDownのコメントを参照。
    if (event.button === 1) event.preventDefault();

    activeAction = action;
    activePointerId = event.pointerId;
    lastClientX = event.clientX;
    lastClientY = event.clientY;

    try {
      domElement.setPointerCapture(event.pointerId);
    } catch (_err) {
      // 合成PointerEvent(javascript_toolでのテスト等)ではInvalidPointerIdで
      // 失敗することがある(実マウスでは成功する)。失敗しても以降の
      // pointermove/pointerupはdomElementに直接ディスパッチされれば拾えるため、
      // ここで握って動作を継続する。
    }

    if (action === "orbit") {
      orbitPivot = _resolveOrbitPivot(event.clientX, event.clientY);
      // 裁定D7: 軸を注視点として保持する。
      focus.copy(orbitPivot);
      focusDistance = Math.max(camera.position.distanceTo(orbitPivot), MIN_FOCUS_DISTANCE);
    }
  }

  function _onPointerMove(event) {
    if (!activeAction || event.pointerId !== activePointerId) return;
    // ボタンが1つも押されていないのに動いているなら、pointerupを取り逃している。
    // setPointerCapture が効かなかった状態でキャンバス外へ出て離すとこれが起きる
    // (レビュー実測: ドラッグが終わらず、ボタンを押していないマウス移動で
    // カメラが回り続けた)。ここで打ち切って引きずらないようにする。
    if (event.buttons === 0) {
      _endDrag(event);
      return;
    }
    const dx = event.clientX - lastClientX;
    const dy = event.clientY - lastClientY;
    lastClientX = event.clientX;
    lastClientY = event.clientY;
    if (dx === 0 && dy === 0) return;

    if (activeAction === "orbit") {
      _applyRotation(orbitPivot, dx, dy, ORBIT_YAW_SIGN);
    } else if (activeAction === "look") {
      _applyRotation(camera.position, dx, dy, LOOK_YAW_SIGN);
    } else if (activeAction === "pan") {
      _applyPan(dx, dy);
    }
  }

  function _endDrag(event) {
    if (event.pointerId !== activePointerId) return;
    try {
      domElement.releasePointerCapture(event.pointerId);
    } catch (_err) {
      // 捕捉できていなかった場合(合成イベント等)でも解放の失敗は無害。
    }
    activeAction = null;
    activePointerId = null;
    orbitPivot = null;
  }

  function _onWheel(event) {
    event.preventDefault(); // ページ自体のスクロールを止める。
    _applyDolly(event.deltaY);
  }

  function _onContextMenu(event) {
    event.preventDefault(); // 右ドラッグ(回転)中に右クリックメニューを出さない。
  }

  // 中ボタンの既定動作(Windowsのオートスクロール、あの緑色の矢印アイコン)は
  // pointerdown側のpreventDefaultだけでは止まらない場合があるため、mousedown側にも
  // 同じ処理を保険として付ける。この環境では実マウスでの検証ができなかったため、
  // 両方に登録して防御的にしている(詳細は報告書「オートスクロール抑止」節)。
  function _onMouseDown(event) {
    if (event.button === 1) event.preventDefault();
  }

  domElement.addEventListener("pointerdown", _onPointerDown);
  domElement.addEventListener("pointermove", _onPointerMove);
  domElement.addEventListener("pointerup", _endDrag);
  domElement.addEventListener("pointercancel", _endDrag);
  domElement.addEventListener("wheel", _onWheel, { passive: false });
  domElement.addEventListener("contextmenu", _onContextMenu);
  domElement.addEventListener("mousedown", _onMouseDown);
  // pointerup は window にも張る。setPointerCapture が効かない状況でキャンバスの
  // 外まで一気にドラッグして離すと、domElement には pointerup が届かず
  // ドラッグ状態が残ってしまう(レビュー実測)。viewer.js のクリック判定が
  // 同じ理由で window に張っているのと揃える。
  window.addEventListener("pointerup", _endDrag);
  window.addEventListener("pointercancel", _endDrag);
  // OrbitControlsが実行時に設定していたもの(CSSには無い)。無いとタッチ操作で
  // ブラウザのスクロール/ズームジェスチャと衝突する(タッチ操作自体はD6で
  // 対応対象外だが、touch-actionを外すとpointer captureの前提が崩れうるため残す)。
  domElement.style.touchAction = "none";

  /**
   * 注視点と注視距離を外部から設定する(読込直後の_fitCameraToMesh等が使う)。
   * カメラの向きが外部で直接書き換えられた可能性があるため、極角の追跡状態も
   * ここで同期し直す(そうしないと以降のpitchクランプが古い向きを基準にしてしまう)。
   * @param {THREE.Vector3} point
   * @param {number} distance
   */
  function setFocus(point, distance) {
    focus.copy(point);
    focusDistance = Math.max(distance, MIN_FOCUS_DISTANCE);
    currentPolar = _polarAngleFromQuaternion(camera.quaternion, THREE);
  }

  /** 現在の注視点・注視距離を返す(検証/デバッグ用途)。 */
  function getFocus() {
    return { point: focus.clone(), distance: focusDistance };
  }

  /** 登録した全リスナを外す。 */
  function dispose() {
    domElement.removeEventListener("pointerdown", _onPointerDown);
    domElement.removeEventListener("pointermove", _onPointerMove);
    domElement.removeEventListener("pointerup", _endDrag);
    domElement.removeEventListener("pointercancel", _endDrag);
    domElement.removeEventListener("wheel", _onWheel);
    domElement.removeEventListener("contextmenu", _onContextMenu);
    domElement.removeEventListener("mousedown", _onMouseDown);
    window.removeEventListener("pointerup", _endDrag);
    window.removeEventListener("pointercancel", _endDrag);
  }

  return { setFocus, getFocus, dispose };
}

/**
 * カメラの向き(quaternion)から、視線方向とワールド+Zの間の角度(極角)を求める。
 * 0=真上を見ている、π=真下を見ている、π/2=水平を見ている。
 * @param {THREE.Quaternion} quaternion
 * @param {object} THREE
 * @returns {number}
 */
function _polarAngleFromQuaternion(quaternion, THREE) {
  const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(quaternion).normalize();
  const cosPolar = Math.min(Math.max(forward.dot(new THREE.Vector3(0, 0, 1)), -1), 1);
  return Math.acos(cosPolar);
}
