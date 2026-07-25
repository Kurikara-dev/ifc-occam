from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

@pytest.fixture(scope="session")
def small_ifc_path() -> Path:
    p = PROJECT_ROOT / "small.ifc"
    if not p.exists():
        pytest.skip("small.ifc not found")
    return p

@pytest.fixture(scope="session")
def large_ifc_path() -> Path:
    p = PROJECT_ROOT / "large.ifc"
    if not p.exists():
        pytest.skip("large.ifc not found")
    return p
