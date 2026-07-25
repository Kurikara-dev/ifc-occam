# IFC Occam

[English](README.md) | **日本語**

IFC Occam は、建築BIMの標準交換形式である IFC(ISO 16739)を軽量化するワークベンチです。BIMツールでは開けないほど肥大化したモデルを、実際に開けるサイズの派生ファイルに縮小します。

## なぜ存在するか

鉄骨ファブリケーションのような詳細形状を多く含むワークフローから出力されるIFCは、ボルト・溶接・小金物・プレートが積み重なり、1モデルが数百MBから数GBに達することがあります。このサイズになると、一般的なBIMツールはおろか、IFC Occam自身のフルオープンでさえ開けなくなることがあります。

このような規模のモデルを要素単位で見て回るのは現実的ではありません。実務で機能するのは**クラス単位の決め打ち**です。IFCクラス別に要素数・形状の重さのランキングを見て、「このクラスは全部削除する」「このクラスは全部bboxにする」といった粗い判断を数回下す方が、個々の要素への無数の判断より実用的です。IFC Occam はこの考え方を軸に、モデルの規模に応じた2つのモードを提供します。

## 2つのモード: GUI と CUI

- **GUI**(`python -m ifc_occam serve`)— ローカルサーバを起動し、ブラウザで3Dビューア(three.js)を開きます。要素を選んで削除・bbox化・凸包化・間引きし、重複形状群を確認し、プリセットを適用し、クラス単位で一括操作してから出力します。中小モデル(目安〜300MB程度まで)向けです。
- **CUI**(`python -m ifc_occam cui <file.ifc>`)— 3D描画を行いません。STEPファイルへの軽量なテキストスキャンにより、クラス別ランキング(要素数・推定Face数)を数秒〜数分で表示します。続く対話コマンドでクラス単位の操作を決め切ったうえで、フルオープンと出力を1回だけ実行します。数百MB〜GB級の巨大モデル向けです。`--scan-only` を付けるとランキングを表示して終了し、対話ループには入りません。

どちらのモードも同じ操作群を共有し、同じ原則に従います。**ツールは自らは判定しません**。選択・確認・実行は常に人間が行います。

## Quick start

```bash
git clone https://github.com/Kurikara-dev/ifc-occam.git
cd ifc-occam
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .            # テスト依存も入れる場合は ".[dev]" を指定
```

Python 3.11 以上が必要です。`dev` extra を入れていれば、`pytest` でテストを実行できます。

### GUI

```bash
python -m ifc_occam serve
```

表示されたURLをブラウザで開き、サーバを起動したフォルダを基準にした相対パスでモデルファイルを指定して読み込みます。

### CUI

```bash
python -m ifc_occam cui heavy_steel.ifc
```

```
IFC Occam CUI — 軽量スキャン中...
=== クラス別ランキング (推定Face数[展開]降順) ===
...
操作を入力してください (h でヘルプ):
> delete IfcMechanicalFastener
> bbox IfcPlate
> list
> apply
```

対話コマンド: `delete` / `bbox` / `hull` / `decimate` / `keep` / `undo` / `list` / `rank` / `apply`(ほかに `help`・`quit`)。CUIおよびGUIのWeb画面は、現時点では日本語表示のみです。

## 免責事項・用途の限定

出力ファイルは**閲覧・参照用の派生物**であり、設計・施工の正本として使用しないでください。出力においては要素が**不可逆に**削除・簡略化されます。元のファイル自体は変更されませんが、一度要素が失われた出力ファイルを完全なモデルに戻すことはできません。設計・施工に関わる判断は、必ず軽量化前の原本に基づいて行ってください。

安全策として、出力ファイルのIFCヘッダには軽量化の由来情報が刻印されます。非正本の派生物である旨・元ファイル名・削除/簡略化した要素数の要約を `FILE_DESCRIPTION.description` に追記し(`ViewDefinition` 等の既存エントリは保持されます)、`FILE_NAME.originating_system` も設定します。この刻印はGUI・CUIどちらのエクスポートでも自動的に行われます。

本ソフトウェアは MIT ライセンスの「AS IS」条項の下で提供され、いかなる保証もありません。

## 開発状況

GUI版は想定していた機能をカバーしています。CUI版は活発に開発中で、`ifcopenshell` のフルオープンを介さずSTEPファイルを直接編集するテキストレベル削除エンジンを計画しています。

## ライセンス

MIT ライセンスです。全文は [LICENSE](LICENSE) を参照してください。同梱している `three.js` ビューアや、LGPL-3.0 の `ifcopenshell` 依存など、サードパーティ由来の構成要素は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) にまとめます。

## Contributing

コントリビューションについては [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。
