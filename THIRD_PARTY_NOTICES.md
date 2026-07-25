# Third-Party Notices

IFC Occam is licensed under the MIT License (see [LICENSE](LICENSE)). This
project bundles or depends on the following third-party components.

## Bundled in `web/vendor/`

| Component | Version | License | Notes |
|---|---|---|---|
| three.js | r160 | MIT | `web/vendor/three.module.js` and `web/vendor/OrbitControls.js` (from the three.js `examples/jsm/` addons). Copyright 2010-2023 Three.js Authors. |

## Python dependencies

| Package | Version | License | Notes |
|---|---|---|---|
| ifcopenshell | 0.8.5 | **LGPL-3.0-or-later** | Used as a library (imported from `ifc_occam/core/*.py`); not modified or relinked. When distributed as a bundled executable (PyInstaller `onedir` build), ifcopenshell's own files remain individually present under `_internal/` rather than merged into a single opaque binary, so they stay independently identifiable and replaceable, per LGPL's requirements for library-form use. |
| numpy | 2.4.6 | BSD-3-Clause | Direct dependency. |
| scipy | 1.17.1 | BSD-3-Clause | Direct dependency (`ConvexHull`). |
| fast-simplification | 0.1.13 | MIT | Direct dependency (mesh decimation). |
| fastapi | 0.139.2 | MIT | Direct dependency (GUI server API). |
| uvicorn | 0.51.0 | BSD-3-Clause | Direct dependency (ASGI server). |
| pydantic | 2.13.4 (pydantic_core 2.46.4) | MIT | Direct dependency (`ifc_occam/server/app.py` imports `BaseModel`/`Field`); previously an undeclared transitive dependency of fastapi, now declared explicitly. |
| starlette | 1.3.1 | BSD-3-Clause | Transitive dependency of fastapi (individually verified during the pre-release audit). |

Other transitive dependencies not listed above (e.g. click, h11, anyio,
idna, certifi) were spot-checked during the pre-release audit and found to be
MIT/BSD/Apache-licensed with no irregularities; a full `pip-licenses` sweep is
recommended before any binary distribution.

## Build tooling

PyInstaller (dev-only, used to produce optional standalone executables) is
GPL-2.0-or-later with an explicit exception permitting its use to build and
distribute non-free or commercial programs; it does not impose any license
obligation on IFC Occam itself or on the executables it produces.
