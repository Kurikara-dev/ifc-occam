// viewer.js -- Three.jsによる単一マージジオメトリの表示・カメラ・raycastを担う。
// DOM(サイドバー等)には関与しない。

import * as THREE from "./vendor/three.module.js";
import { OrbitControls } from "./vendor/OrbitControls.js";

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

/**
 * Three.jsシーンを初期化する。
 * @param {HTMLCanvasElement} canvas
 * @returns {object} viewer インターフェース
 */
export function initViewer(canvas) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x1a1a1a);

  const camera = new THREE.PerspectiveCamera(
    50,
    canvas.clientWidth / canvas.clientHeight,
    0.01,
    100000
  );
  camera.position.set(10, 10, 10);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);

  const ambient = new THREE.AmbientLight(0xffffff, 0.6);
  const directional = new THREE.DirectionalLight(0xffffff, 0.8);
  directional.position.set(1, 2, 3);
  scene.add(ambient, directional);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;

  let mesh = null;
  let meta = null;
  let baseColors = null; // Float32Array snapshot of the class-color palette
  let clickCallback = null;

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

  function _animate() {
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(_animate);
  }
  requestAnimationFrame(_animate);

  /**
   * メッシュを設定する。既存メッシュがあれば破棄する。
   * @param {object} meshMeta /api/mesh の meta JSON
   * @param {Float32Array} positions
   * @param {Uint32Array} indices
   */
  function setMesh(meshMeta, positions, indices) {
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

    const colors = new Float32Array(positions.length);
    for (const el of meta.elements) {
      const [r, g, b] = classColor(el.ifc_class);
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
    baseColors = colors.slice();

    const material = new THREE.MeshLambertMaterial({
      vertexColors: true,
      side: THREE.DoubleSide,
    });

    mesh = new THREE.Mesh(geometry, material);
    scene.add(mesh);

    _fitCameraToMesh();
  }

  function _fitCameraToMesh() {
    if (!mesh) return;
    mesh.geometry.computeBoundingSphere();
    const sphere = mesh.geometry.boundingSphere;
    if (!sphere) return;

    const radius = Math.max(sphere.radius, 0.01);
    const direction = new THREE.Vector3(1, 0.6, 1).normalize();
    const distance = radius / Math.sin((camera.fov * Math.PI) / 360);

    camera.position.copy(sphere.center).addScaledVector(direction, distance * 1.2);
    camera.near = Math.max(distance / 1000, 0.001);
    camera.far = distance * 1000;
    camera.updateProjectionMatrix();

    controls.target.copy(sphere.center);
    controls.update();
  }

  /**
   * 指定した要素(meta.elements の1件)の頂点色を上書きする。
   * 要素内は溶接済み頂点のため vertex_start/vertex_count の範囲を塗る。
   * @param {object} element meta.elements の要素オブジェクト
   */
  function setElementColor(element, r, g, b) {
    if (!mesh) return;
    const colorAttr = mesh.geometry.getAttribute("color");
    const vertexStart = element.vertex_start;
    const vertexCount = element.vertex_count;
    for (let v = 0; v < vertexCount; v++) {
      const idx = vertexStart + v;
      colorAttr.setXYZ(idx, r, g, b);
    }
    colorAttr.needsUpdate = true;
  }

  /**
   * 頂点色をクラス色パレットの初期状態に戻す。
   */
  function resetColors() {
    if (!mesh || !baseColors) return;
    const colorAttr = mesh.geometry.getAttribute("color");
    colorAttr.array.set(baseColors);
    colorAttr.needsUpdate = true;
  }

  /**
   * クリックされた三角形のindexを受け取るコールバックを登録する。
   * raycastはviewer内部で行う。
   * @param {(triIndex: number) => void} callback
   */
  function onTriangleClick(callback) {
    clickCallback = callback;
  }

  renderer.domElement.addEventListener("click", (event) => {
    if (!mesh || !clickCallback) return;
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObject(mesh, false);
    if (hits.length > 0 && hits[0].faceIndex !== undefined) {
      clickCallback(hits[0].faceIndex);
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

  return {
    setMesh,
    setElementColor,
    resetColors,
    onTriangleClick,
    getColorArray,
  };
}
