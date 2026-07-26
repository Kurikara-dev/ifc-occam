// estimate.js -- 読込時間・メモリの推定(副作用のない純粋関数のみ)。
//
// サーバの GET /api/config が返す係数(fullopen_bytes_multiplier/
// fullopen_warn_bytes/load_estimate)を引数で受け取って計算するだけで、
// 係数そのものはこのファイルに持たない(JS側に写経しない。二重管理を避ける
// ため——監督者裁定3。ifc_occam/scan/aggregate.py の FULLOPEN_BYTES_MULTIPLIER
// と ifc_occam/cui/repl.py の _FULLOPEN_WARN_BYTES が唯一の値の出所)。
//
// 推定式の係数(sec_per_mb=0.72 / base_sec=30.0 / band_low=0.5 / band_high=2.0)は
// small.ifc(21.5MB→45秒)・large.ifc(102MB→103秒)という実測2点から出した
// 一次式(監督者裁定4)。開発機のCPUが定格より遅い状態で検証しても、
// この係数は変えない(実測環境側が遅いだけであり、係数の校正不足ではない)。

/**
 * ファイルサイズ(bytes)から読込所要時間とメモリの推定を返す。
 * secMid = base_sec + sec_per_mb * (bytes / 1048576) の一次式に
 * band_low/band_high を掛けて幅(レンジ)にする(D2=a: 推定は幅で出す)。
 * @param {number} bytes
 * @param {{fullopen_bytes_multiplier:number, fullopen_warn_bytes:number,
 *   load_estimate:{sec_per_mb:number, base_sec:number, band_low:number, band_high:number}}} config
 *   GET /api/config の戻り値そのもの。
 * @returns {{secLow:number, secHigh:number, memBytes:number, warn:boolean}}
 */
export function estimateLoad(bytes, config) {
  const { sec_per_mb, base_sec, band_low, band_high } = config.load_estimate;
  const secMid = base_sec + sec_per_mb * (bytes / 1048576);
  const secLow = secMid * band_low;
  const secHigh = secMid * band_high;
  const memBytes = bytes * config.fullopen_bytes_multiplier;
  const warn = memBytes > config.fullopen_warn_bytes;
  return { secLow, secHigh, memBytes, warn };
}

/**
 * 秒数を日本語の時間表記にする。
 * 60秒未満: "{n}秒"。60秒以上3600秒未満: "{分}分{秒}秒"。3600秒以上: "{時間}時間{分}分"。
 * 例: 90 -> "1分30秒" / 45 -> "45秒" / 4000 -> "1時間7分"。
 * @param {number} sec
 * @returns {string}
 */
export function formatDuration(sec) {
  const total = Math.round(sec);
  if (total < 60) return `${total}秒`;
  if (total < 3600) {
    const min = Math.floor(total / 60);
    const remSec = total % 60;
    return `${min}分${remSec}秒`;
  }
  const hour = Math.floor(total / 3600);
  const remMin = Math.round((total % 3600) / 60);
  return `${hour}時間${remMin}分`;
}

/**
 * バイト数を読みやすい単位(1024進)に整形する。例: 1536 -> "1.5 KB"。
 * B単位は整数、それ以外(KB/MB/GB/TB)は小数点1桁で表示する。
 * @param {number} bytes
 * @returns {string}
 */
export function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;
  while (Math.abs(value) >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex++;
  }
  const rounded = unitIndex === 0 ? String(Math.round(value)) : value.toFixed(1);
  return `${rounded} ${units[unitIndex]}`;
}
