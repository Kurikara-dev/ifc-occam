"""ローカルFastAPIサーバ (phase2 plan §サーバAPI, phase3 plan Task5で拡張)。

POST /api/load はバックグラウンドスレッドで extract_model → aggregate_by_class
→ find_duplicates → build_mesh_payload を実行し、AppState を更新する
(ステージごとの所要秒を status message に載せる)。

Phase3: 操作リスト(GET/POST /api/ops)、削除プレビュー(preview-delete)、
共有数照会(sharing)、出力(export)を追加する。load 時に開いた ifcopenshell.file を
AppState に保持し、preview-delete/sharing はこれを読み取り専用で使い回す。
export は原本を新たに open して(export.apply_operations が担う)別ファイルへ書き出す。
"""

from __future__ import annotations

import dataclasses
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Literal

import ifcopenshell
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ifc_occam.core.cascade import compute_delete_closure
from ifc_occam.core.diagnose import aggregate_by_class
from ifc_occam.core.duplicates import find_duplicates
from ifc_occam.core.export import apply_operations
from ifc_occam.core.extract import extract_model
from ifc_occam.core.layers import aggregate_by_layer
from ifc_occam.core.ops import Operation, resolve_effective, validate_operations
from ifc_occam.core.presets import (
    Preset,
    PresetRule,
    delete_preset,
    load_presets,
    resolve_preset,
    save_presets,
)
from ifc_occam.core.simplify import count_shared_elements, get_shared_element_gids
from ifc_occam.cui.repl import _FULLOPEN_WARN_BYTES
from ifc_occam.scan.aggregate import FULLOPEN_BYTES_MULTIPLIER
from ifc_occam.server.files import list_directory, resolve_within_root
from ifc_occam.server.meshpack import build_mesh_payload
from ifc_occam.server.state import AppState

if getattr(sys, "frozen", False):
    # PyInstaller onedir バンドル: sys._MEIPASS 下に datas として同梱した web/ を使う。
    WEB_DIR = Path(sys._MEIPASS) / "web"  # type: ignore[attr-defined]
else:
    WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"

logger = logging.getLogger(__name__)

# presets.json の既定パス(サーバ起動時のcwd相対、プロジェクトルート想定)。
# テストは create_app(presets_path=...) で tmp_path に注入して差し替える。
DEFAULT_PRESETS_PATH = Path("presets.json")

# ファイル選択ダイアログ(GET /api/files)で表示する拡張子(大小文字は
# ifc_occam.server.files.list_directory 側で無視して比較する)。
FILE_LIST_SUFFIXES: tuple[str, ...] = (".ifc",)

# GET /api/config の load_estimate。実測2点(small.ifc 21.5MB→45秒、
# large.ifc 102MB→103秒)から出した一次式の係数(監督者裁定4)。
# sec_per_mb/base_sec は secMid = base_sec + sec_per_mb * MB の係数、
# band_low/band_high は secMid に掛けて幅を作る係数。この値は開発機の
# CPU性能に依らない一次近似であり、遅い実行環境で推定より長くかかっても
# 係数側を調整しない(実測環境そのものが定格より遅いだけであるため)。
LOAD_ESTIMATE_CONFIG = {
    "sec_per_mb": 0.72,
    "base_sec": 30.0,
    "band_low": 0.5,
    "band_high": 2.0,
}


# ---------------------------------------------------------------------------
# pydantic モデル (ボディ検証)
# ---------------------------------------------------------------------------


class LoadRequest(BaseModel):
    path: str


class OperationModel(BaseModel):
    op: Literal["delete", "simplify", "keep"]
    targets: list[str]
    scope: Literal["element", "shared"] = "element"
    params: dict = Field(default_factory=dict)


class OpsRequest(BaseModel):
    operations: list[OperationModel]


class PreviewDeleteRequest(BaseModel):
    targets: list[str]


class SharingBatchRequest(BaseModel):
    gids: list[str]


class ExportRequest(BaseModel):
    output_path: str
    consolidate: bool = False
    # 書き出し時のゴミ回収方式(export.apply_operations にそのまま渡す)。
    # "gc"(既定)=一括GC(fat一時ファイル+約1.6倍メモリ)、"inline"=逐次
    # (省メモリ。要素ごとに即時回収するためGCより遅いことがある。
    # かつてのバッチ化は残置を生むためPhase Iで撤去済み)。
    geometry_cleanup: Literal["gc", "inline"] = "gc"


class PresetRuleModel(BaseModel):
    match: dict
    op: dict


class PresetModel(BaseModel):
    name: str
    description: str = ""
    rules: list[PresetRuleModel] = Field(default_factory=list)


class PresetResolveRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# バックグラウンド処理
# ---------------------------------------------------------------------------


def open_ifc_file(path: str):
    """IFCファイルを開く。extract_model が開いた file object を再利用できるよう分離。"""
    return ifcopenshell.open(str(path))


def _run_load(state: AppState, path: str) -> None:
    try:
        t0 = time.monotonic()
        ifc_file = open_ifc_file(path)
        model, warnings = extract_model(ifc_file)
        t_extract = time.monotonic()

        stats = aggregate_by_class(model)
        t_diagnose = time.monotonic()

        groups = find_duplicates(model.shapes)
        t_duplicates = time.monotonic()

        payload = build_mesh_payload(model)
        t_meshpack = time.monotonic()

        message = (
            f"extract {t_extract - t0:.1f}s / diagnose {t_diagnose - t_extract:.1f}s / "
            f"duplicates {t_duplicates - t_diagnose:.1f}s / meshpack {t_meshpack - t_duplicates:.1f}s"
        )
        logger.info("load完了: %s", message)
        state.set_ready(ifc_file, path, model, stats, groups, warnings, payload, message)
    except Exception as exc:  # noqa: BLE001 - サーバは生き続ける、原因を message に格納
        state.set_error(str(exc))


def _run_export(
    state: AppState,
    src_path: str | None,
    operations: list[Operation],
    output_path: str,
    consolidate: bool = False,
    geometry_cleanup: str = "gc",
) -> None:
    try:
        report = apply_operations(
            src_path,
            operations,
            output_path,
            consolidate=consolidate,
            geometry_cleanup=geometry_cleanup,
        )
        state.set_export_result(
            {
                "deleted": len(report.deleted),
                "simplified": len(report.simplified),
                "skipped": len(report.skipped),
                "output_path": report.output_path,
                "warnings": list(report.warnings),
                "consolidated_groups": report.consolidated_groups,
                "consolidated_elements": report.consolidated_elements,
                "stage_seconds": dict(report.stage_seconds),
            }
        )
    except Exception as exc:  # noqa: BLE001 - サーバは生き続ける、原因を export_result に格納
        state.set_export_failed(str(exc))


# ---------------------------------------------------------------------------
# レスポンス整形ヘルパー
# ---------------------------------------------------------------------------


def _plain_int(value: object) -> object:
    """numpy整数など json 非対応の数値型を素の int に変換する。"""
    if hasattr(value, "item") and not isinstance(value, (bool, str)):
        try:
            return int(value.item())
        except (TypeError, ValueError):
            return value
    return value


def _dataclass_dict_plain_int(obj) -> dict:
    """dataclass インスタンスを、数値フィールドを素の int に変換した dict に直す。

    ClassStats/LayerStats はフィールド構成が異なるだけで変換規則は同一なので、
    この変換自体は共有する(集計ロジックの非共通化(aggregate_by_class/
    aggregate_by_layer)とは別の話)。
    """
    return {k: _plain_int(v) for k, v in dataclasses.asdict(obj).items()}


def _class_stats_dict(stats) -> dict:
    return _dataclass_dict_plain_int(stats)


def _layer_stats_dict(stats) -> dict:
    return _dataclass_dict_plain_int(stats)


def _element_gids_by_shape(model) -> dict[str, list[str]]:
    """shape_id → 参照要素GlobalIdリスト。診断の duplicate_groups 拡充に使う。"""
    mapping: dict[str, list[str]] = {}
    for elem in model.elements:
        if elem.shape_id is not None:
            mapping.setdefault(elem.shape_id, []).append(elem.global_id)
    return mapping


def _duplicate_group_dict(group, gids_by_shape: dict[str, list[str]]) -> dict:
    d = {k: _plain_int(v) for k, v in dataclasses.asdict(group).items()}
    d["element_gids"] = [gids_by_shape.get(shape_id, []) for shape_id in group.shape_ids]
    return d


def _unique_sorted_layers(model) -> list[str]:
    return sorted({e.layer for e in model.elements if e.layer is not None})


def _layerless_element_count(model) -> int:
    """layer(IfcPresentationLayerAssignment)が未設定の要素数。

    aggregate_by_layer は layer=None の要素を結果から除外するため、その件数が
    診断レスポンスから消えてしまう(監督者裁定2)。GUIでレイヤー別集計の合計が
    全要素数に一致しないときに「集計が壊れている」と誤解されないよう、
    別フィールドとして明示する。
    """
    return sum(1 for e in model.elements if e.layer is None)


def _cascade_item_dict(item) -> dict:
    return {
        "global_id": item.global_id,
        "ifc_class": item.ifc_class,
        "name": item.name,
        "reason": item.reason,
    }


def create_app(presets_path: str | Path | None = None, root: Path | None = None) -> FastAPI:
    app = FastAPI()
    state = AppState()
    app.state.ifc_state = state
    app.state.presets_path = Path(presets_path) if presets_path is not None else DEFAULT_PRESETS_PATH
    # ファイル一覧/読込パスの閉じ込め判定基準(監督者裁定1)。起動時に1回だけ
    # resolve() して確定させ、以後変えない。root は既定でサーバのカレント
    # ディレクトリ(Path.cwd())——テストは root=tmp_path で差し替える。
    app.state.files_root = (root if root is not None else Path.cwd()).resolve()

    @app.post("/api/load", status_code=202)
    def load(body: LoadRequest):
        # 監督者裁定6: ファイル選択ダイアログだけでなく手打ち欄からのパスも
        # root外なら拒否する(ダイアログを塞いでも手打ち欄が素通しなら意味が
        # 無い)。存在確認はしない(既存のFileNotFoundError経路
        # (test_load_nonexistent_path_sets_error_state)は非同期のerror状態
        # 遷移のままにする——ここでのチェックは閉じ込め判定のみ)。
        try:
            resolved_path = resolve_within_root(app.state.files_root, body.path)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})

        if not state.begin_loading():
            return JSONResponse(
                status_code=409, content={"detail": "load already in progress"}
            )
        thread = threading.Thread(
            target=_run_load, args=(state, str(resolved_path)), daemon=True
        )
        thread.start()
        return {"status": "loading"}

    @app.get("/api/files")
    def list_files(path: str = ""):
        try:
            result = list_directory(app.state.files_root, path, FILE_LIST_SUFFIXES)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        except FileNotFoundError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        except OSError as exc:
            # 読めないディレクトリ(権限拒否、OneDrive等のプレースホルダ、
            # ネットワークドライブの一時不通、ウイルス対策ソフトのロック)。
            # FileNotFoundError より後に置くこと——あちらも OSError の
            # サブクラスなので、順序を逆にすると404が403に化ける。
            # 捕まえずに落とすと 500 "Internal Server Error" になり、
            # ダイアログにその英語がそのまま出る(Task 4 レビュー Important-1:
            # レビュアが icacls で読み取り拒否したフォルダを作って再現した)。
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        f"フォルダを読めませんでした(アクセス拒否またはI/Oエラー): "
                        f"{path!r} ({exc.__class__.__name__})"
                    )
                },
            )
        return result

    @app.get("/api/config")
    def get_config():
        # fullopen_bytes_multiplier/fullopen_warn_bytesはaggregate.py/repl.pyの
        # 定数をそのまま返す(JS側への写経禁止。二重管理を避けるため)。
        return {
            "fullopen_bytes_multiplier": FULLOPEN_BYTES_MULTIPLIER,
            "fullopen_warn_bytes": _FULLOPEN_WARN_BYTES,
            "load_estimate": dict(LOAD_ESTIMATE_CONFIG),
        }

    @app.get("/api/status")
    def status():
        return state.snapshot_status()

    @app.get("/api/diagnostics")
    def diagnostics():
        with state.lock:
            if state.state != "ready":
                return JSONResponse(
                    status_code=409, content={"detail": "model not ready"}
                )
            model = state.model
            stats = state.stats
            groups = state.groups
            warnings = state.warnings

        total_triangles = sum(_plain_int(s.total_triangles) for s in stats)
        gids_by_shape = _element_gids_by_shape(model)
        layer_stats = aggregate_by_layer(model)
        return {
            "schema": model.schema,
            "element_count": len(model.elements),
            "total_triangles": total_triangles,
            "class_stats": [_class_stats_dict(s) for s in stats],
            "duplicate_groups": [_duplicate_group_dict(g, gids_by_shape) for g in groups],
            "warnings": list(warnings),
            "layers": _unique_sorted_layers(model),
            "layer_stats": [_layer_stats_dict(s) for s in layer_stats],
            "layerless_element_count": _layerless_element_count(model),
        }

    @app.get("/api/mesh")
    def mesh():
        with state.lock:
            if state.state != "ready":
                return JSONResponse(
                    status_code=409, content={"detail": "model not ready"}
                )
            payload = state.payload
        return Response(content=payload, media_type="application/octet-stream")

    @app.get("/api/ops")
    def get_ops():
        return {"operations": [op.to_dict() for op in state.get_operations()]}

    @app.post("/api/ops")
    def set_ops(body: OpsRequest):
        ready = state.get_ready_snapshot()
        if ready is None:
            return JSONResponse(status_code=409, content={"detail": "model not ready"})
        model, _ifc_file = ready

        known_gids = {e.global_id for e in model.elements}
        operations = [
            Operation(op=o.op, targets=list(o.targets), scope=o.scope, params=dict(o.params))
            for o in body.operations
        ]
        warnings = validate_operations(operations, known_gids)
        state.set_operations(operations)
        return {"warnings": warnings}

    @app.post("/api/ops/preview-delete")
    def preview_delete(body: PreviewDeleteRequest):
        ready = state.get_ready_snapshot()
        if ready is None:
            return JSONResponse(status_code=409, content={"detail": "model not ready"})
        model, ifc_file = ready

        known_gids = {e.global_id for e in model.elements}
        unknown = [gid for gid in body.targets if gid not in known_gids]
        if unknown:
            return JSONResponse(
                status_code=400,
                content={"detail": f"unknown GlobalId(s): {unknown}"},
            )

        closure = compute_delete_closure(ifc_file, body.targets)

        # Final Review Fix2: 連鎖削除はkeep指定に優先する(design.md §4.5)。
        # ただし黙って上書きすると誠実でないため、現在保存中の操作リストで
        # op=="keep" になっている連鎖メンバーを keep_overridden として明示する。
        # export側の挙動(closureが常に勝つ)は変えない。プレビューのみの追加情報。
        effective = resolve_effective(state.get_operations())
        keep_overridden = [
            {"global_id": item.global_id, "ifc_class": item.ifc_class, "name": item.name}
            for item in closure.cascaded
            if effective.get(item.global_id) is not None and effective[item.global_id].op == "keep"
        ]

        return {
            "direct": len(closure.direct),
            "cascaded": [_cascade_item_dict(item) for item in closure.cascaded],
            "total": len(closure.all_gids),
            "keep_overridden": keep_overridden,
        }

    @app.get("/api/element/{gid}/sharing")
    def sharing(gid: str):
        ready = state.get_ready_snapshot()
        if ready is None:
            return JSONResponse(status_code=409, content={"detail": "model not ready"})
        model, ifc_file = ready

        known_gids = {e.global_id for e in model.elements}
        if gid not in known_gids:
            return JSONResponse(
                status_code=404, content={"detail": f"unknown GlobalId: {gid}"}
            )

        shared_count = count_shared_elements(ifc_file, gid)
        return {"shared_count": shared_count}

    @app.post("/api/elements/sharing")
    def sharing_batch(body: SharingBatchRequest):
        """複数gidの共有数と兄弟gid(同一RepresentationMap参照要素、自身を除く)を
        一括照会する(選択要素数分のfetch fan-outを避ける)。未知gidはエラーにせず
        count=0/siblings=[]で含める(フロントは選択中の全gidを渡すだけでよい)。
        siblings は共有波及の着色(scope="shared"のsimplify確定時)に使う
        (Phase4 Task4)。"""
        ready = state.get_ready_snapshot()
        if ready is None:
            return JSONResponse(status_code=409, content={"detail": "model not ready"})
        model, ifc_file = ready

        known_gids = {e.global_id for e in model.elements}
        counts = {
            gid: (count_shared_elements(ifc_file, gid) if gid in known_gids else 0)
            for gid in body.gids
        }
        siblings = {
            gid: (get_shared_element_gids(ifc_file, gid) if gid in known_gids else [])
            for gid in body.gids
        }
        return {"counts": counts, "siblings": siblings}

    @app.post("/api/export", status_code=202)
    def export(body: ExportRequest):
        if not state.begin_exporting():
            return JSONResponse(
                status_code=409,
                content={"detail": "model not ready or export already in progress"},
            )
        src_path, operations = state.get_export_context()
        thread = threading.Thread(
            target=_run_export,
            args=(state, src_path, operations, body.output_path, body.consolidate, body.geometry_cleanup),
            daemon=True,
        )
        thread.start()
        return {"status": "exporting"}

    @app.get("/api/presets")
    def get_presets():
        presets = load_presets(app.state.presets_path)
        return [p.to_dict() for p in presets]

    @app.post("/api/presets")
    def set_presets(body: list[PresetModel]):
        presets = [
            Preset(
                name=p.name,
                description=p.description,
                rules=[PresetRule(match=dict(r.match), op=dict(r.op)) for r in p.rules],
            )
            for p in body
        ]
        save_presets(app.state.presets_path, presets)
        return [p.to_dict() for p in presets]

    @app.delete("/api/presets")
    def delete_preset_endpoint(name: str):
        # GUI改修Task6・監督者裁定4: 当初案の DELETE /api/presets/{name}
        # (パスパラメータ)は、name に "/" を含む場合にStarletteのデフォルト
        # コンバータ(単一セグメント、"/"非許容)がマッチせず404になる
        # (ASGIサーバが%2Fを事前に生の"/"へデコードしてルーティングに渡すため)。
        # そのためクエリパラメータ方式に変更した(GET /api/files?path=... と
        # 同じhouse style)。name はFastAPIが自動でURLデコードするため、
        # ここではデコード後の文字列とプリセット名を完全一致で比較するだけでよい。
        presets = load_presets(app.state.presets_path)
        if not any(p.name == name for p in presets):
            return JSONResponse(
                status_code=404, content={"detail": f"その名前の操作パターンはありません: {name!r}"}
            )
        remaining = delete_preset(app.state.presets_path, name)
        return [p.to_dict() for p in remaining]

    @app.post("/api/presets/resolve")
    def resolve_presets_endpoint(body: PresetResolveRequest):
        ready = state.get_ready_snapshot()
        if ready is None:
            return JSONResponse(status_code=409, content={"detail": "model not ready"})
        model, _ifc_file = ready

        presets = load_presets(app.state.presets_path)
        preset = next((p for p in presets if p.name == body.name), None)
        if preset is None:
            return JSONResponse(
                status_code=404, content={"detail": f"その名前の操作パターンはありません: {body.name!r}"}
            )

        results, warnings = resolve_preset(preset, model)
        rules = [
            {
                "match": dict(rule.match),
                "op": dict(rule.op),
                "count": len(gids),
                "gids": gids,
            }
            for rule, gids in results
        ]
        return {"rules": rules, "warnings": warnings}

    @app.middleware("http")
    async def _no_store_static(request, call_next):
        """静的ファイル(非/apiレスポンス)にno-storeを付け、開発時のブラウザキャッシュ
        取り残し(古いapp.js等が読まれるトラップ)を防ぐ。"""
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app
