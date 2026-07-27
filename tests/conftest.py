"""Test configuration.

The `function_app` directory is put on sys.path rather than the repository root,
because that directory *is* the deployment root: in Azure, `railpulse` sits
directly beside `function_app.py` at /home/site/wwwroot. Importing it the same
way here means the tests exercise the same import graph the cloud does, and an
import that only works locally cannot pass.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FUNCTION_APP_DIR = REPO_ROOT / "function_app"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

if str(FUNCTION_APP_DIR) not in sys.path:
    sys.path.insert(0, str(FUNCTION_APP_DIR))


def load_fixture(name: str) -> dict:
    """Load a captured API payload.

    These are real responses recorded from api.irail.be on 2026-07-27, not
    hand-written samples. That matters: every quirk the parser defends against
    (numbers as strings, '?' platforms, 'S32' types, occupancy as a nested
    object) is present here because the feed really does it, so a test passing
    means the parser handles the actual API rather than an idea of it.
    """
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture()
def brussels_central() -> dict:
    return load_fixture("liveboard_brussels_central.json")


@pytest.fixture()
def brussels_midi() -> dict:
    return load_fixture("liveboard_brussels_midi.json")


@pytest.fixture()
def station_catalogue() -> dict:
    return load_fixture("stations_subset.json")


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT
