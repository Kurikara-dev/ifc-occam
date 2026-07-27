// rendercheck.js -- 「実際に何色で描かれたか」を測るための検証専用モジュール。
// 本番の描画経路からは呼ばれない(app.js の window.__debug からのみ使う)。
//
// この環境ではブラウザペインが非表示でタブが hidden 判定になり、
// requestAnimationFrame が止まる。canvas のピクセルも読めない。そこで
// 同じ scene を別の WebGLRenderer で1フレームだけ描いて readPixels する。

// 計測用レンダラは1つだけ作って使い回す。呼ぶたびに new WebGLRenderer すると
// ブラウザのWebGLコンテキスト上限(概ね16)に当たり、古い方から強制的に失われる。
// 実測では20回連続で呼ぶと本番canvasのコンテキストまで奪われて画面が死んだ
// (renderer.dispose() はthree.js側のGPUリソースを解放するだけで、ブラウザ側の
// コンテキストスロットは即座には返らない)。使い回せば余分なコンテキストは常に1つ。
let _renderer = null;
let _canvas = null;

/**
 * 計測用のオフスクリーンレンダラを返す(無ければ作る)。コンテキストを失っていたら
 * 作り直す(GPUリセット等で失われた後も計測を続けられるようにするため)。
 */
function _acquireRenderer(THREE, width, height) {
  if (_renderer) {
    const gl = _renderer.getContext();
    if (gl && !gl.isContextLost()) {
      _canvas.width = width;
      _canvas.height = height;
      _renderer.setSize(width, height, false);
      return _renderer;
    }
    _renderer.dispose();
    _renderer = null;
    _canvas = null;
  }
  _canvas = document.createElement("canvas");
  _canvas.width = width;
  _canvas.height = height;
  _renderer = new THREE.WebGLRenderer({
    canvas: _canvas,
    antialias: false,
    preserveDrawingBuffer: true,
  });
  _renderer.setSize(width, height, false);
  return _renderer;
}

/**
 * 計測用レンダラを明示的に解放する(通常は呼ばなくてよい)。
 * WebGLコンテキストのスロットを即座に返したいときだけ使う。
 */
export function disposeRenderCheck() {
  if (!_renderer) return;
  const gl = _renderer.getContext();
  _renderer.dispose();
  gl?.getExtension("WEBGL_lose_context")?.loseContext();
  _renderer = null;
  _canvas = null;
}

/**
 * シーンをオフスクリーンに1枚描き、画素の統計を返す。
 * @param {object} scene THREE.Scene
 * @param {object} THREE three.js モジュール名前空間
 * @param {{width?:number, height?:number, chromaThreshold?:number}} [options]
 *   width/height: 計測解像度(既定 480x300)。coloredPx は解像度に比例するので、
 *     前後比較するときは同じ値で測ること。
 *   chromaThreshold: 「着色されている」とみなす彩度(max-min)の下限(既定 18)。
 *     無彩色のステージ・背景を数えないための閾値。
 * @returns {{coloredPx:number, coloredPct:number, avgLumaOfColored:number,
 *            darkPct:number, totalPx:number}}
 *   coloredPx: 彩度が閾値を超えた画素数。モデルが着色されていれば増える。
 *   avgLumaOfColored: その画素の平均輝度(0-255)。減光の効き具合の指標。
 *
 * 制約: カメラのフレーミングは scene 全体のAABBから決めるため、非表示
 * (visible=false)のステージも大きさの計算には含まれる(Box3.setFromObject は
 * visible を見ない)。モデルが画面に占める割合がわずかに変わるだけで、
 * 「着色されているか否か」の判定には影響しない。
 */
export function measureRenderedColors(scene, THREE, options = {}) {
  const width = options.width ?? 480;
  const height = options.height ?? 300;
  const chromaThreshold = options.chromaThreshold ?? 18;

  const renderer = _acquireRenderer(THREE, width, height);

  const box = new THREE.Box3().setFromObject(scene);
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const radius = Math.max(sphere.radius, 0.001);
  const camera = new THREE.PerspectiveCamera(50, width / height, radius / 1000, radius * 100);
  const distance = (radius / Math.sin((50 * Math.PI) / 360)) * 1.1;
  camera.position
    .copy(sphere.center)
    .add(new THREE.Vector3(1, 0.45, 1).normalize().multiplyScalar(distance));
  camera.lookAt(sphere.center);

  renderer.render(scene, camera);

  const gl = renderer.getContext();
  const pixels = new Uint8Array(width * height * 4);
  gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
  // ここでは dispose() しない。次回の呼び出しで同じレンダラを使い回すため
  // (毎回捨てるとコンテキストが枯渇する。上の _acquireRenderer のコメント参照)。

  let colored = 0;
  let lumaSum = 0;
  let dark = 0;
  const total = width * height;
  for (let i = 0; i < pixels.length; i += 4) {
    const r = pixels[i];
    const g = pixels[i + 1];
    const b = pixels[i + 2];
    const chroma = Math.max(r, g, b) - Math.min(r, g, b);
    const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    if (chroma > chromaThreshold) {
      colored++;
      lumaSum += luma;
    }
    if (luma < 40) dark++;
  }
  return {
    coloredPx: colored,
    coloredPct: Number(((100 * colored) / total).toFixed(2)),
    avgLumaOfColored: Number((lumaSum / Math.max(colored, 1)).toFixed(1)),
    darkPct: Number(((100 * dark) / total).toFixed(2)),
    totalPx: total,
  };
}
