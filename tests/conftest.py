"""Shared pytest fixtures.

Tests are network-free by default. Live-page tests (harvest and pipeline) run against
saved fixture HTML under ``tests/fixtures/`` served locally (``file://`` or a localhost
static server), never the public network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from colorsense.net.politeness import PolitenessPolicy

# Make the repo-root ``examples`` package importable so its tests can exercise the example
# code directly. The library itself is installed (src layout); ``examples`` is not — it is
# documentation that we still lint, type-check, and test.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Absolute path to the test fixtures directory."""
    return FIXTURES_DIR


def file_policy(**kwargs: object) -> PolitenessPolicy:
    """A :class:`PolitenessPolicy` that opts in to ``file://`` fixture URLs.

    ``file://`` is disabled by default (``allow_file_urls=False``), so every test that
    drives :func:`colorsense.analyze` / :meth:`PolitenessPolicy.fetch` at a local fixture
    needs this opt-in. Tests that call ``harvest_page``/``RenderSession`` directly are
    unaffected — the scheme gate lives in the policy, the only place networking policy is
    enforced.
    """
    return PolitenessPolicy(allow_file_urls=True, **kwargs)  # type: ignore[arg-type]
