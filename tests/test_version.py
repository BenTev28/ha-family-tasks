"""The version lives in manifest.json and const.VERSION (card cache-busting); keep them equal."""

import json
from pathlib import Path

from awesomeversion import AwesomeVersion

from custom_components.family_tasks.const import VERSION

MANIFEST = Path(__file__).parents[1] / "custom_components" / "family_tasks" / "manifest.json"


def test_version_matches_manifest() -> None:
    assert json.loads(MANIFEST.read_text())["version"] == VERSION


def test_version_is_valid_for_hacs() -> None:
    assert AwesomeVersion(VERSION).valid
