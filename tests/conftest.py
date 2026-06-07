"""Shared pytest fixtures.

Tests are network-free by default. Live-page work packages (WP4, WP11) run against
saved fixture HTML under ``tests/fixtures/`` served locally (``file://`` or a localhost
static server), never the public network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Absolute path to the test fixtures directory."""
    return FIXTURES_DIR
