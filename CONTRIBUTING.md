# Contributing to IFC Occam

## Dev setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Requires Python >= 3.11.

## Running tests

```bash
pytest
```

The full suite must pass (all green) before any change is merged. New behavior should be developed test-first (TDD): write a failing test, then the minimal code to make it pass.

Large IFC test fixtures (multi-hundred-MB to multi-GB models) are not included in this repository; they are excluded via `.gitignore` (`*.ifc`). Use your own sample files, or the small fixtures under `tests/`, when working locally.

## Documentation

Some docstrings reference internal design and planning documents (`docs/design.md`, `docs/cui-design.md`, `docs/plans/`); those are not included in this public distribution.

## Commit style

Use [Conventional Commits](https://www.conventionalcommits.org/) (e.g. `feat: ...`, `fix: ...`, `docs: ...`, `chore: ...`) for commit subjects.
