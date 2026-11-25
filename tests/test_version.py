"""The version lives in manifest.json, const.VERSION (card cache-busting) and the card's CARD_VERSION; keep them equal."""

import json
from pathlib import Path
import re

from awesomeversion import AwesomeVersion

from custom_components.family_tasks.const import VERSION

MANIFEST = Path(__file__).parents[1] / "custom_components" / "family_tasks" / "manifest.json"


def test_version_matches_manifest() -> None:
    assert json.loads(MANIFEST.read_text())["version"] == VERSION


def test_version_is_valid_for_hacs() -> None:
    assert AwesomeVersion(VERSION).valid


def test_card_version_matches() -> None:
    card = (MANIFEST.parent / "frontend" / "family-tasks-card.js").read_text()
    match = re.search(r'^const CARD_VERSION = "([^"]+)";', card, re.MULTILINE)
    assert match, "CARD_VERSION not found in the card"
    assert match.group(1) == VERSION
