"""Harvest integration tests.

All tests run against LOCAL fixture HTML loaded via ``file://`` URLs (no public network).
A single Chromium-backed harvest is reasonably cheap; each test harvests its own fixture
under the light theme (the dark-theme media block is asserted indirectly via the token
records, which capture every declared scope including the dark ``@media`` block).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from colorsense.config import Config, load_default_config
from colorsense.harvest import RenderSession, harvest_page
from colorsense.models import Harvest, Theme, Viewport

VIEWPORT = Viewport(w=1280, h=800, device_scale_factor=1.0)

# Every test here drives a real Chromium render; skip in browserless CI.
pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def config() -> Config:
    """The real palette config (vendor prefixes drive vendor_match)."""
    return load_default_config()


async def _harvest(fixture: Path, config: Config, theme: Theme = Theme.light) -> Harvest:
    return await harvest_page(fixture.as_uri(), theme, config, VIEWPORT)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


async def test_tokens_extracted_and_alias_graph(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "tokens.html", config)

    by_name = {token.name: token for token in harvest.tokens}

    # Declared custom properties appear.
    assert "--color-primary" in by_name
    assert "--accent" in by_name
    assert "--btn-bg" in by_name

    # Resolved colors parse for color-valued tokens.
    assert by_name["--color-primary"].resolved is not None
    assert by_name["--accent"].resolved is not None
    assert by_name["--accent"].resolved is not None
    assert by_name["--accent"].resolved.hex == "#e91e63"

    # The non-color token resolves to None.
    assert by_name["--not-a-color"].resolved is None

    # Alias graph: --btn-bg: var(--accent) -> alias_target carries the leading '--'.
    assert by_name["--btn-bg"].alias_target == "--accent"

    # The dark @media block is captured with its media text on at least one record.
    dark_records = [t for t in harvest.tokens if t.media is not None and "dark" in t.media]
    assert dark_records, "expected at least one token declared under prefers-color-scheme: dark"


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------


async def test_elements_extracted_with_colors_and_exclusions(
    fixtures_dir: Path, config: Config
) -> None:
    harvest = await _harvest(fixtures_dir / "tokens.html", config)

    classes = {tuple(el.class_tokens) for el in harvest.elements}
    assert ("primary-box",) in classes
    assert ("btn",) in classes

    primary = next(el for el in harvest.elements if "primary-box" in el.class_tokens)
    assert primary.bg is not None
    assert primary.bg.hex == "#3366cc"
    assert primary.text is not None
    assert primary.text.hex == "#ffffff"

    # Hidden (display:none) and zero-area elements are excluded.
    for el in harvest.elements:
        assert "hidden-box" not in el.class_tokens
        assert "zero-box" not in el.class_tokens

    # Every returned element is visible and not aria-hidden.
    assert all(el.visible and not el.aria_hidden for el in harvest.elements)


async def test_vendor_match_detected(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "tokens.html", config)
    intercom = next(el for el in harvest.elements if "intercom-launcher" in el.class_tokens)
    assert intercom.vendor_match is True


# ---------------------------------------------------------------------------
# Hover states
# ---------------------------------------------------------------------------


async def test_hover_color_change_detected(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "hover.html", config)

    cta = next(el for el in harvest.elements if el.id == "cta")
    assert cta.clickable is True
    assert cta.has_hover_color_change is True
    assert cta.hover_bg is not None
    assert cta.hover_bg.hex == "#ff6600"

    # A clickable button with no :hover rule does not report a change.
    plain = next(el for el in harvest.elements if el.id == "plain")
    assert plain.has_hover_color_change is False


# ---------------------------------------------------------------------------
# Screenshot bins + consent masking
# ---------------------------------------------------------------------------


async def test_screenshot_bins_valid(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "tokens.html", config)

    assert harvest.screenshot_bins, "expected non-empty screenshot bins"
    for bin_ in harvest.screenshot_bins:
        assert 0.0 <= bin_.area_fraction <= 1.0
    total = sum(b.area_fraction for b in harvest.screenshot_bins)
    assert total <= 1.0 + 1e-6


async def test_consent_region_masked(fixtures_dir: Path, config: Config) -> None:
    harvest = await _harvest(fixtures_dir / "consent.html", config)

    # The banner detector found the fixed full-width cookie banner.
    # The banner's unique magenta (#ff00aa) must not dominate the bins after masking.
    banner_hex = "#ff00aa"
    banner_fraction = sum(
        b.area_fraction for b in harvest.screenshot_bins if b.color.hex == banner_hex
    )
    # Masking should strongly suppress the banner color (it covered ~18% unmasked).
    assert banner_fraction < 0.02, (
        f"banner color not masked: fraction={banner_fraction}, "
        f"bins={[(b.color.hex, round(b.area_fraction, 3)) for b in harvest.screenshot_bins]}"
    )

    # White page background should dominate instead.
    assert harvest.screenshot_bins
    assert harvest.screenshot_bins[0].color.hex in {"#ffffff", "#fefefe"}


# ---------------------------------------------------------------------------
# RenderSession contract
# ---------------------------------------------------------------------------


async def test_render_session_exposes_page_and_consent(fixtures_dir: Path) -> None:
    async with RenderSession(Theme.light, VIEWPORT) as session:
        await session.goto((fixtures_dir / "consent.html").as_uri())
        # The page is exposed for module JS.
        title = await session.page.title()
        assert title == "Consent fixture"
        # A consent banner region was detected for masking.
        assert session.consent_rects, "expected a detected consent region"
