"""サーバの読み込み状態 (phase2 plan §サーバAPI, phase3 plan Task5で拡張)。

threading.Lock で保護し、load/export は1度に1つだけ実行できる。
state 遷移: idle -> loading -> ready -> (exporting -> ready | error) | error。
export の失敗は state="error"(終端、要再ロード)にはせず、set_export_failed で
ready に復帰させる(model/opsは無傷なため)。error はload失敗専用。
loading/exporting 中の再実行は呼び出し側(app.py)で 409 を返す判断に使う。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ifc_occam.core.duplicates import DuplicateGroup
from ifc_occam.core.ops import Operation
from ifc_occam.core.types import ClassStats, ModelData

State = str  # "idle" | "loading" | "ready" | "exporting" | "error"


@dataclass
class AppState:
    """読み込み状態と結果一式を保持する。全フィールドは lock 経由でのみ変更する。"""

    lock: threading.Lock = field(default_factory=threading.Lock)
    state: State = "idle"
    message: str = ""
    ifc_file: object | None = None  # load時に開いた ifcopenshell.file (原本、非破壊で保持)
    src_path: str | None = None  # export の apply_operations に渡す元パス
    model: ModelData | None = None
    stats: list[ClassStats] | None = None
    groups: list[DuplicateGroup] | None = None
    warnings: list[str] | None = None
    payload: bytes | None = None
    operations: list[Operation] = field(default_factory=list)
    export_result: dict | None = None
    _load_started_at: float | None = field(default=None, repr=False)

    def begin_loading(self) -> bool:
        """loading 状態への遷移を試みる。既に loading/exporting 中なら False (409用)。"""
        with self.lock:
            if self.state in ("loading", "exporting"):
                return False
            self.state = "loading"
            self.message = ""
            self._load_started_at = time.monotonic()
            return True

    def set_ready(
        self,
        ifc_file,
        src_path: str,
        model: ModelData,
        stats: list[ClassStats],
        groups: list[DuplicateGroup],
        warnings: list[str],
        payload: bytes,
        message: str = "",
    ) -> None:
        with self.lock:
            self.state = "ready"
            self.ifc_file = ifc_file
            self.src_path = src_path
            self.model = model
            self.stats = stats
            self.groups = groups
            self.warnings = warnings
            self.payload = payload
            self.message = message
            self.operations = []
            self.export_result = None

    def set_error(self, message: str) -> None:
        with self.lock:
            self.state = "error"
            self.message = message

    def begin_exporting(self) -> bool:
        """ready 状態からのみ exporting へ遷移できる。それ以外(loading/exporting/idle/error)は False。"""
        with self.lock:
            if self.state != "ready":
                return False
            self.state = "exporting"
            self.message = "exporting"
            return True

    def get_export_context(self) -> tuple[str | None, list[Operation]]:
        """export スレッドに渡す src_path と操作リストのスナップショットを返す。"""
        with self.lock:
            return self.src_path, list(self.operations)

    def set_export_result(self, result: dict) -> None:
        with self.lock:
            self.state = "ready"
            self.export_result = result
            self.message = ""

    def set_export_failed(self, message: str) -> None:
        """export失敗時に呼ぶ。load失敗(set_error)とは異なり、model/opsは無傷なので
        state="ready" に復帰させる(再ロード無しで再操作/再exportできるように)。
        失敗理由は export_result に格納し、/api/status 経由でフロントに伝える。"""
        with self.lock:
            self.state = "ready"
            self.export_result = {"error": message}
            self.message = ""

    def get_ready_snapshot(self):
        """state=='ready' なら (model, ifc_file) を返す。そうでなければ None (409用)。"""
        with self.lock:
            if self.state != "ready":
                return None
            return self.model, self.ifc_file

    def get_operations(self) -> list[Operation]:
        with self.lock:
            return list(self.operations)

    def set_operations(self, operations: list[Operation]) -> None:
        with self.lock:
            self.operations = list(operations)

    def snapshot_status(self) -> dict:
        with self.lock:
            elapsed = 0.0
            if self._load_started_at is not None:
                elapsed = time.monotonic() - self._load_started_at
            return {
                "state": self.state,
                "message": self.message,
                "elapsed_sec": elapsed,
                "export_result": self.export_result,
            }

    def is_ready(self) -> bool:
        with self.lock:
            return self.state == "ready"
