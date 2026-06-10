"""Shared pytest fixtures.

Tests are network-free by default. Live-page tests (harvest and pipeline) run against
saved fixture HTML under ``tests/fixtures/`` served locally (``file://`` or a localhost
static server), never the public network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from colorsense.net.politeness import PolitenessPolicy

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
