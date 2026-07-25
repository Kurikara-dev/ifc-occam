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
from ifc_occam.core.ops import Operation, resolve_effective, validate_operations
from ifc_occam.core.presets import Preset, PresetRule, load_presets, resolve_preset, save_presets
from ifc_occam.core.simplify import count_shared_elements, get_shared_element_gids
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
) -> None:
    try:
        report = apply_operations(src_path, operations, output_path, consolidate=consolidate)
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


def _class_stats_dict(stats) -> dict:
    return {k: _plain_int(v) for k, v in dataclasses.asdict(stats).items()}


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


def _cascade_item_dict(item) -> dict:
    return {
        "global_id": item.global_id,
        "ifc_class": item.ifc_class,
        "name": item.name,
        "reason": item.reason,
    }


def create_app(presets_path: str | Path | None = None) -> FastAPI:
    app = FastAPI()
    state = AppState()
    app.state.ifc_state = state
    app.state.presets_path = Path(presets_path) if presets_path is not None else DEFAULT_PRESETS_PATH

    @app.post("/api/load", status_code=202)
    def load(body: LoadRequest):
        if not state.begin_loading():
            return JSONResponse(
                status_code=409, content={"detail": "load already in progress"}
            )
        thread = threading.Thread(target=_run_load, args=(state, body.path), daemon=True)
        thread.start()
        return {"status": "loading"}

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
        return {
            "schema": model.schema,
            "element_count": len(model.elements),
            "total_triangles": total_triangles,
            "class_stats": [_class_stats_dict(s) for s in stats],
            "duplicate_groups": [_duplicate_group_dict(g, gids_by_shape) for g in groups],
            "warnings": list(warnings),
            "layers": _unique_sorted_layers(model),
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
            args=(state, src_path, operations, body.output_path, body.consolidate),
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
                status_code=404, content={"detail": f"unknown preset name: {body.name!r}"}
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
