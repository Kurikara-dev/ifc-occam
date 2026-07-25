"""診断CLI (design.md §8-1)。extract→aggregate→find_duplicates→出力。

format_report は純粋関数(I/Oなし)としてテスト対象にする。main() が実際の
ファイル読み込みと標準出力への書き込みを担う。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

from ifc_occam.core.diagnose import aggregate_by_class
from ifc_occam.core.duplicates import DuplicateGroup, find_duplicates
from ifc_occam.core.extract import extract_model
from ifc_occam.core.types import ClassStats, ModelData
from ifc_occam.cui.repl import run as run_cui

_TOP_DUPLICATE_GROUPS = 20


def format_report(
    model: ModelData,
    stats: list[ClassStats],
    groups: list[DuplicateGroup],
    warnings: list[str],
) -> str:
    """診断結果を人間可読なレポート文字列に組み立てる(純粋関数)。"""
    total_triangles = sum(s.total_triangles for s in stats)
    lines: list[str] = []

    lines.append(f"スキーマ: {model.schema}")
    lines.append(f"要素数: {len(model.elements)}")
    lines.append(f"総三角形数: {total_triangles}")
    lines.append("")

    lines.append("=== クラス別ランキング ===")
    lines.append(
        f"{'IFCクラス':<30}{'要素数':>10}{'形状数':>10}"
        f"{'三角形数':>12}{'共有経由':>10}{'最大単体':>10}"
    )
    for s in stats:
        lines.append(
            f"{s.ifc_class:<30}{s.element_count:>10}{s.unique_shape_count:>10}"
            f"{s.total_triangles:>12}{s.mapped_count:>10}{s.max_single_shape_triangles:>10}"
        )
    lines.append("")

    lines.append(f"=== 重複形状群 (上位{_TOP_DUPLICATE_GROUPS}件) ===")
    if not groups:
        lines.append("重複形状は検出されませんでした。")
    else:
        for g in groups[:_TOP_DUPLICATE_GROUPS]:
            lines.append(
                f"件数={len(g.shape_ids)} 節約可能三角形数={g.savable_triangles} "
                f"(1形状あたり{g.triangle_count}三角形)"
            )
    lines.append("")

    lines.append(f"警告: {len(warnings)}")
    for w in warnings:
        lines.append(f"  - {w}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="ifc_occam")
    subparsers = parser.add_subparsers(dest="command", required=True)

    diagnose_parser = subparsers.add_parser("diagnose", help="IFCファイルを診断する")
    diagnose_parser.add_argument("path", help="診断対象のIFCファイルパス")

    serve_parser = subparsers.add_parser("serve", help="ローカルWebサーバを起動する")
    serve_parser.add_argument(
        "--port", type=int, default=None,
        help="待ち受けポート(省略時は8000から空きポートを自動探索)",
    )
    serve_parser.add_argument(
        "--no-browser", action="store_true", help="起動後にブラウザを自動で開かない"
    )

    cui_parser = subparsers.add_parser("cui", help="対話的にIFCを軽量化する(CUI)")
    cui_parser.add_argument("path", help="対象のIFCファイルパス")
    cui_parser.add_argument(
        "--output", default=None, help="出力ファイルパス(省略時は<元名>_light.ifc)"
    )
    cui_parser.add_argument(
        "--scan-only", action="store_true",
        help="軽量スキャンとランキング表示のみ行い、対話ループに入らず終了する",
    )

    args = parser.parse_args(argv)

    if args.command == "diagnose":
        model, warnings = extract_model(args.path)
        stats = aggregate_by_class(model)
        groups = find_duplicates(model.shapes)
        print(format_report(model, stats, groups, warnings))
    elif args.command == "serve":
        _serve(port=args.port, open_browser=not args.no_browser)
    elif args.command == "cui":
        run_cui(args.path, output=args.output, scan_only=args.scan_only)


def _find_free_port(start: int, host: str = "127.0.0.1", tries: int = 20) -> int:
    """start から順に空きポートを探す。見つからなければ start を返す。"""
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, p)) != 0:
                return p
    return start


def _serve(port: int | None, open_browser: bool) -> None:
    """uvicorn でサーバを起動し、起動成功後にブラウザを開く。

    port が明示指定されていない場合(None)は8000から空きポートを自動探索する。
    明示指定された場合はそのポートをそのまま使う(ユーザー指定優先)。
    """
    import uvicorn

    from ifc_occam.server.app import create_app

    app = create_app()

    resolved_port = port if port is not None else _find_free_port(8000)
    url = f"http://127.0.0.1:{resolved_port}/"
    print(url, flush=True)

    if open_browser:
        def _open_when_up() -> None:
            webbrowser.open(url)

        threading.Timer(1.0, _open_when_up).start()

    uvicorn.run(app, host="127.0.0.1", port=resolved_port)


if __name__ == "__main__":
    main()
