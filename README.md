# IFC Occam

**English** | [日本語](README.ja.md)

IFC Occam is a lightweighting workbench for IFC (ISO 16739) files, the standard exchange format for building BIM. It turns oversized models into smaller derivative files that ordinary BIM tools can actually open.

## Why it exists

IFC files coming out of detail-heavy workflows — steel fabrication is a common source — pile up bolts, welds, small fittings, and plates until a single model reaches hundreds of megabytes or several gigabytes. At that size, general-purpose BIM tools, and sometimes even IFC Occam's own full-model loader, struggle to open the file at all.

Reviewing a model like that element-by-element isn't realistic. What works is deciding **by class**: look at a ranked breakdown of which IFC classes dominate the element and geometry count, then make a handful of coarse, deliberate calls — "remove every fastener," "reduce every plate to a bounding box" — instead of thousands of individual judgments. IFC Occam is built around that workflow, in two forms depending on model size.

## Two modes: GUI and CUI

- **GUI** (`python -m ifc_occam serve`) — starts a local web server and opens a 3D viewer (three.js) in your browser. Select elements interactively; delete, bbox, convex-hull, or decimate them; review duplicate-shape groups; apply named presets; act on a whole IFC class at once; then export. Best for small-to-medium models (roughly up to ~300 MB as a rule of thumb).
- **CUI** (`python -m ifc_occam cui <file.ifc>`) — no 3D rendering. A lightweight text scan of the raw STEP file produces a per-class ranking (element count, estimated face count) in seconds to minutes. An interactive command loop then lets you commit to class-level operations before a single full open-and-export pass. Built for huge models (hundreds of MB to multi-GB). Pass `--scan-only` to print the ranking and exit without entering the interactive loop.

Both modes share the same underlying operations and the same rule: **the tool never decides on its own** — a human selects, confirms, and applies every change.

## Quick start

```bash
git clone https://github.com/Kurikara-dev/ifc-occam.git
cd ifc-occam
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .            # add ".[dev]" to also install test dependencies
```

Requires Python >= 3.11. Run the test suite with `pytest` (after installing the `dev` extra).

### GUI

```bash
python -m ifc_occam serve
```

Open the printed URL in your browser, then load a model by entering a path relative to the folder you launched the server from.

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

Interactive commands: `delete` / `bbox` / `hull` / `decimate` / `keep` / `undo` / `list` / `rank` / `apply` (plus `help` and `quit`). Note that both the CUI and the GUI's web interface currently display Japanese text only.

## Disclaimer

Output files are **derived, view/reference-only artifacts** — never treat them as the design or construction record of authority. Elements are **irreversibly** removed or simplified in the output. The original file itself is never modified, but once an element is gone from the output, that output cannot be turned back into the full model. Always make design and construction decisions from the original, unaltered file.

As a safeguard, every exported file's IFC header is stamped with lightweighting provenance — a non-authoritative-derivative disclosure, the source filename, and a count of elements deleted/simplified — appended to `FILE_DESCRIPTION.description` (existing entries such as `ViewDefinition` are preserved), plus `FILE_NAME.originating_system`. This happens automatically on every export, in both GUI and CUI.

This software is provided under the MIT License's "AS IS" terms, with no warranty of any kind.

## Development status

The GUI covers its intended feature set. The CUI is under active development; planned work includes a text-level deletion engine (editing the STEP file directly, without a full `ifcopenshell` open).

## License

MIT — see [LICENSE](LICENSE). Third-party components — the bundled `three.js` viewer and the LGPL-3.0 `ifcopenshell` dependency, among others — are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
