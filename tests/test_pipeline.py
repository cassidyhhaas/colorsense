"""Pipeline + politeness tests.

The end-to-end cases render LOCAL fixture HTML via ``file://`` (no public network), the
same approach harvest uses. Politeness mechanics (robots gate, rate limiter, render cache) are
exercised with an injected fake harvester so they need neither Playwright nor real network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from colorsense import analyze
from colorsense.config import Config, load_config
from colorsense.models import (
    AnalysisResult,
    Harvest,
    ScreenshotBin,
    Theme,
    Viewport,
)
from colorsense.net.politeness import PolitenessPolicy, RobotsDisallowedError

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = str(REPO_ROOT / "config" / "palette_config.yaml")
VIEWPORT = Viewport(w=1280, h=800, device_scale_factor=1.0)


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config(CONFIG_PATH)


# ---------------------------------------------------------------------------
# Politeness: a fake harvester so these tests touch no browser / network.
# ---------------------------------------------------------------------------


class _CountingHarvester:
    """Records every (url, theme) it renders and returns a trivial Harvest."""

    def __init__(self, bins: list[ScreenshotBin] | None = None) -> None:
        self.calls: list[tuple[str, Theme]] = []
        self._bins = bins or []

    def __call__(self, url: str, theme: Theme, config: Config, viewport: Viewport) -> Harvest:
        self.calls.append((url, theme))
        return Harvest(
            url=url,
            theme=theme,
            viewport=viewport,
            screenshot_bins=self._bins,
        )


class _Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def test_cache_returns_without_re_render(config: Config) -> None:
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=lambda _u: None)
    url = "https://example.test/page"

    first = policy.fetch(url, Theme.light, config, VIEWPORT)
    second = policy.fetch(url, Theme.light, config, VIEWPORT)

    assert second is first  # identical object straight from cache
    assert harvester.calls == [(url, Theme.light)]  # rendered exactly once

    # A different theme is a distinct cache key and re-renders.
    policy.fetch(url, Theme.dark, config, VIEWPORT)
    assert harvester.calls == [(url, Theme.light), (url, Theme.dark)]


def test_robots_disallow_blocks_fetch(config: Config) -> None:
    disallow = lambda _u: "User-agent: *\nDisallow: /"  # noqa: E731
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=disallow)
    url = "https://example.test/secret"

    assert policy.can_fetch(url) is False
    with pytest.raises(RobotsDisallowedError):
        policy.fetch(url, Theme.light, config, VIEWPORT)
    assert harvester.calls == []  # never rendered


def test_respect_robots_false_bypasses_gate(config: Config) -> None:
    disallow = lambda _u: "User-agent: *\nDisallow: /"  # noqa: E731
    harvester = _CountingHarvester()
    policy = PolitenessPolicy(harvester=harvester, robots_loader=disallow, respect_robots=False)
    url = "https://example.test/secret"

    assert policy.can_fetch(url) is True
    policy.fetch(url, Theme.light, config, VIEWPORT)
    assert harvester.calls == [(url, Theme.light)]


def test_file_url_bypasses_robots(config: Config) -> None:
    # file:// has no host/robots concept: always fetchable even under a disallow loader.
    policy = PolitenessPolicy(
        harvester=_CountingHarvester(),
        robots_loader=lambda _u: "User-agent: *\nDisallow: /",
    )
    assert policy.can_fetch("file:///tmp/x.html") is True


def test_rate_limiter_spaces_same_host(config: Config) -> None:
    harvester = _CountingHarvester()
    clock = _Clock()
    slept: list[float] = []

    def sleeper(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    policy = PolitenessPolicy(
        harvester=harvester,
        robots_loader=lambda _u: None,
        min_interval=2.0,
        clock=clock,
        sleeper=sleeper,
    )

    policy.fetch("https://host.test/a", Theme.light, config, VIEWPORT)
    clock.t += 0.5  # only 0.5s elapses before the next same-host fetch
    policy.fetch("https://host.test/b", Theme.light, config, VIEWPORT)

    assert slept == [pytest.approx(1.5)]  # waited the remaining 1.5s of the interval


def test_analyze_propagates_robots_block(config: Config) -> None:
    policy = PolitenessPolicy(
        harvester=_CountingHarvester(),
        robots_loader=lambda _u: "User-agent: *\nDisallow: /",
    )
    with pytest.raises(RobotsDisallowedError):
        analyze("https://example.test/", config_path=CONFIG_PATH, politeness=policy)


# ---------------------------------------------------------------------------
# End-to-end on local fixtures (real Playwright harvest of file:// HTML).
# ---------------------------------------------------------------------------


def _analyze_fixture(name: str, fixtures_dir: Path, **kwargs: object) -> AnalysisResult:
    url = (fixtures_dir / name).as_uri()
    return analyze(url, config_path=CONFIG_PATH, viewport=VIEWPORT, **kwargs)  # type: ignore[arg-type]


@pytest.mark.browser
def test_end_to_end_light_and_dark(fixtures_dir: Path) -> None:
    # tokens.html has a `prefers-color-scheme: dark` block, so the two renders differ
    # and both themes survive collapse.
    result = _analyze_fixture("tokens.html", fixtures_dir)

    assert isinstance(result, AnalysisResult)
    assert set(result.themes) == {Theme.light, Theme.dark}
    assert result.metadata["single_theme"] == "false"
    assert 0.0 <= result.fit_score <= 1.0

    for theme, palette in result.themes.items():
        assert palette.theme is theme
        contrast = palette.recommendation.contrast
        # recommendation guarantees: text pairs >= 4.5, surfaces vs page >= 3.0.
        assert contrast["heading_text_on_heading_bg"] >= 4.5 - 1e-6
        assert contrast["cta_text_on_cta_bg"] >= 4.5 - 1e-6
        assert contrast["cta_bg_on_page"] >= 3.0 - 1e-6

    # Declared tokens were classified and carried onto the result.
    assert result.tokens
    token_names = {ct.record.name for ct in result.tokens}
    assert "--color-primary" in token_names

    # The result is a clean Pydantic round-trip.
    restored = AnalysisResult.model_validate_json(result.model_dump_json())
    assert restored == result


@pytest.mark.browser
def test_single_theme_site_collapses(fixtures_dir: Path) -> None:
    # hover.html has no dark-mode block: light and dark renders are identical, so the
    # pipeline collapses to one theme.
    result = _analyze_fixture("hover.html", fixtures_dir)

    assert len(result.themes) == 1
    assert result.metadata["single_theme"] == "true"
    assert result.metadata["themes_requested"] == "light,dark"
    assert result.metadata["themes_analyzed"] == "light"


@pytest.mark.browser
def test_hover_hint_feeds_recommendation(fixtures_dir: Path) -> None:
    # hover.html's #cta flips to #ff6600 on hover; that hint reaches recommend() and is
    # reported as a distinct hover background (the recommender does not enforce a contrast floor on
    # the hover color, only on the heading/cta surfaces and text pairs).
    result = _analyze_fixture("hover.html", fixtures_dir)

    (palette,) = result.themes.values()
    rec = palette.recommendation
    assert "cta_hover_bg_on_page" in rec.contrast
    assert rec.cta_hover_bg.hex != rec.cta_bg.hex


@pytest.mark.browser
def test_explicit_single_theme_request(fixtures_dir: Path) -> None:
    result = _analyze_fixture("tokens.html", fixtures_dir, themes=(Theme.light,))
    assert set(result.themes) == {Theme.light}
    assert result.metadata["themes_requested"] == "light"


def test_empty_themes_rejected(fixtures_dir: Path) -> None:
    url = (fixtures_dir / "tokens.html").as_uri()
    with pytest.raises(ValueError, match="at least one theme"):
        analyze(url, config_path=CONFIG_PATH, themes=())
