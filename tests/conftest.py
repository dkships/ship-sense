"""Session-level test hygiene."""
import shutil
from pathlib import Path

import pytest

OUTPUTS = Path(__file__).resolve().parent.parent / "outputs"


@pytest.fixture(scope="session", autouse=True)
def _clean_pytest_outputs():
    """Tests write real run dirs under outputs/pytest*; drop them at session end
    so the outputs tree doesn't grow by one set per pytest invocation forever."""
    yield
    for p in OUTPUTS.glob("pytest*"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
