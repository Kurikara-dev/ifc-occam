"""ファイル一覧APIの実体 (GUI改修 Task4)。

ブラウザの `<input type="file">` は選んだファイルの絶対パスを返さない
(セキュリティ制約)ため、サーバでディレクトリ一覧を返す自前のファイル
ブラウザを組む。参照できるのは **サーバ起動フォルダ(root)以下だけ**
(監督者裁定1)。root は create_app が起動時に1回だけ resolve() して
確定させ、以後変えない(呼び出し側の責務。ここでは root を毎回
受け取るだけの純粋関数として提供する)。

閉じ込め判定は文字列の前方一致(startswith)では絶対に行わない——
`C:\\work` を root にしたとき、名前が接頭辞を共有する兄弟ディレクトリ
`C:\\work-secret` を「内側」と誤判定してしまう(監督者裁定2)。
必ず resolve() 後の実パスを Path.is_relative_to() で比較する。これにより
".." によるルート脱出、絶対パス指定、root の外を指すシンボリックリンク
経由の脱出のいずれも同じ1つのチェックで防げる。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["resolve_within_root", "list_directory"]


def resolve_within_root(root: Path, relative: str) -> Path:
    """root 配下に閉じ込めた絶対パスを返す。root の外を指す場合は ValueError。

    判定は resolve() 後の実パスで行う(シンボリックリンクや .. による脱出を
    防ぐ)。relative が絶対パスの場合、pathlib の `/` 演算子は root 側を
    捨てて relative そのものを採用する(ドライブ文字は root 側を継承する
    ケースがある)仕様だが、ここでは意図的にそれを許容する——その後の
    resolve() + is_relative_to() の1点判定だけで内外を決めるため、
    「絶対パス表記だから拒否」ではなく「実体が root の外だから拒否」という
    正しい理由で弾かれる(root 配下を絶対パスで指定した場合は正当に許可される)。
    """
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValueError(f"パスがルートの外を指しています: {relative!r}")
    return candidate


def _display_path(path: Path, root: Path) -> str:
    """root からの相対パスをフォワードスラッシュ区切りの文字列で返す。

    path が root 自身のときは空文字列を返す(Path.relative_to は '.' を
    返すため、そのまま使うと空文字列であるべき「ルート自身」の表現が
    崩れる。呼び出し側の GET /api/files?path=<この文字列> で再度
    root を指せるように、ルート自身は必ず空文字列に統一する)。
    """
    if path == root:
        return ""
    return path.relative_to(root).as_posix()


def list_directory(root: Path, relative: str, suffixes: tuple[str, ...]) -> dict:
    """{"path": <root相対の表示用パス>, "parent": <root相対 or None>,
    "entries": [{"name", "is_dir", "size", "mtime"}]} を返す。

    ディレクトリを先、その後ファイル名の昇順(大小文字は区別しない)で
    並べる。suffixes(大小文字を無視して比較)に一致しないファイルは除く
    (is_dir なエントリは suffixes に関係なく常に含める。掘れなくなるのを
    防ぐため)。suffixes が空タプルならファイルの拡張子フィルタを行わない。

    relative が root の外を指す場合は ValueError(呼び出し側で400に写す)、
    存在しないパス・ディレクトリでないパスを指す場合は FileNotFoundError
    (呼び出し側で404に写す)。
    """
    root_resolved = root.resolve()
    target = resolve_within_root(root_resolved, relative)

    if not target.exists():
        raise FileNotFoundError(f"パスが見つかりません: {relative!r}")
    if not target.is_dir():
        raise FileNotFoundError(f"ディレクトリではありません: {relative!r}")

    lowered_suffixes = {s.lower() for s in suffixes}

    entries = []
    for child in target.iterdir():
        is_dir = child.is_dir()
        if not is_dir and lowered_suffixes and child.suffix.lower() not in lowered_suffixes:
            continue
        try:
            st = child.stat()
        except OSError:
            # 権限エラー等で読めないエントリは一覧から黙って除外する
            # (1件読めないだけでディレクトリ全体の一覧が失敗するのは避ける)。
            continue
        entries.append(
            {
                "name": child.name,
                "is_dir": is_dir,
                "size": None if is_dir else st.st_size,
                "mtime": st.st_mtime,
            }
        )
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

    parent = None if target == root_resolved else _display_path(target.parent, root_resolved)

    return {
        "path": _display_path(target, root_resolved),
        "parent": parent,
        "entries": entries,
    }
