"""The end-to-end pipeline and public ``analyze`` entry point.

``analyze`` wires every stage into one typed call: render each requested
theme (gated by :class:`~colorsense.net.politeness.PolitenessPolicy`), classify tokens
and components, build a color inventory, assign palette roles,
reconcile usage against declared intent, and recommend WCAG-safe widget colors
— per theme. Sites that ignore ``prefers-color-scheme`` (near-identical light/dark renders)
are collapsed to a single theme. The whole thing is assembled into a frozen
:class:`~colorsense.models.AnalysisResult`.

Networking lives entirely behind ``PolitenessPolicy``/``harvest_page``; everything else is
pure given a :class:`~colorsense.models.Harvest`, so tests drive the pipeline against local
``file://`` fixtures with no public network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass

from colorsense.classify.components import classify_components
from colorsense.classify.tokens import classify_tokens
from colorsense.color.primitives import delta_e
from colorsense.config import Config, load_config
from colorsense.models import (
    AnalysisResult,
    ClassifiedToken,
    Color,
    ColorCluster,
    ComponentType,
    DivergenceItem,
    Harvest,
    RoleResults,
    Theme,
    ThemePalette,
    Viewport,
)
from colorsense.net.politeness import PolitenessPolicy
from colorsense.palette.inventory import build_inventory
from colorsense.palette.reconcile import reconcile
from colorsense.palette.roles import assign_roles
from colorsense.recommend import recommend

DEFAULT_CONFIG_PATH = "config/palette_config.yaml"
DEFAULT_VIEWPORT = Viewport(w=1280, h=800, device_scale_factor=1.0)
DEFAULT_THEMES: tuple[Theme, ...] = (Theme.light, Theme.dark)

# Two renders are "the same site" when every dominant screenshot bin in one has a close
# perceptual match in the other. A genuine dark mode flips the large-area background bin,
# pushing it well past this OKLab threshold; a non-responsive site stays under it.
_COLLAPSE_DELTA_E = 0.06
_COLLAPSE_TOP_BINS = 4


@dataclass
class _ThemeOutput:
    """Everything derived for one rendered theme."""

    palette: ThemePalette
    tokens: list[ClassifiedToken]
    status_colors: list[Color]
    third_party_colors: list[Color]
    divergence: list[DivergenceItem]
    fit_score: float


async def analyze(
    url: str,
    *,
    config_path: str = DEFAULT_CONFIG_PATH,
    viewport: Viewport = DEFAULT_VIEWPORT,
    themes: tuple[Theme, ...] = DEFAULT_THEMES,
    politeness: PolitenessPolicy | None = None,
) -> AnalysisResult:
    """Analyze ``url`` and return a typed :class:`AnalysisResult`.

    Async-native: the requested themes are rendered concurrently (each its own headless
    Chromium), gated by ``politeness``, and the rest of the pipeline is pure CPU work.
    Awaitable directly from an asyncio event loop (e.g. a FastAPI ``async def`` endpoint).

    Parameters
    ----------
    url:
        Page to analyze. ``file://`` URLs are supported (and used by the test suite); any
        ``http(s)`` fetch is gated by ``politeness``.
    config_path:
        Path to the palette config YAML (defaults to the bundled config, resolved relative
        to the current working directory).
    viewport:
        Render viewport; defaults to 1280x800 at 1x scale.
    themes:
        Themes to render, in priority order (the first is "primary" and supplies the
        top-level token/divergence/fit-score fields). Duplicates are ignored.
    politeness:
        Fetch policy (robots gate, rate limit, render cache). A conservative default
        :class:`PolitenessPolicy` is created when omitted. The **consumer** is responsible
        for authorization — see the README.

    Raises
    ------
    colorsense.net.politeness.RobotsDisallowedError
        If ``robots.txt`` disallows the fetch and the policy respects it.
    """
    config = load_config(config_path)
    policy = politeness if politeness is not None else PolitenessPolicy()

    ordered_themes = list(dict.fromkeys(themes))
    if not ordered_themes:
        raise ValueError("analyze() requires at least one theme")

    # Render every requested theme concurrently; the per-host rate limiter inside
    # ``policy.fetch`` still spaces the underlying navigations.
    rendered = await asyncio.gather(
        *(policy.fetch(url, theme, config, viewport) for theme in ordered_themes)
    )
    harvests: dict[Theme, Harvest] = dict(zip(ordered_themes, rendered, strict=True))

    kept_themes = _collapse_themes(ordered_themes, harvests)

    outputs: dict[Theme, _ThemeOutput] = {
        theme: _analyze_theme(harvests[theme], config, viewport) for theme in kept_themes
    }

    primary = outputs[kept_themes[0]]

    return AnalysisResult(
        url=url,
        viewport=viewport,
        themes={theme: out.palette for theme, out in outputs.items()},
        tokens=primary.tokens,
        third_party_colors=_dedupe_colors(
            color for out in outputs.values() for color in out.third_party_colors
        ),
        status_colors=_dedupe_colors(
            color for out in outputs.values() for color in out.status_colors
        ),
        divergence=primary.divergence,
        fit_score=primary.fit_score,
        metadata=_build_metadata(ordered_themes, kept_themes, policy),
    )


def _analyze_theme(harvest: Harvest, config: Config, viewport: Viewport) -> _ThemeOutput:
    """Run the per-theme classify → inventory → roles → reconcile → recommend chain."""
    classified_tokens, status_colors = classify_tokens(harvest.tokens, config)
    classified_elements = classify_components(harvest.elements, config, viewport)
    clusters = build_inventory(harvest, classified_elements)

    usage_roles, fit_score = assign_roles(clusters)
    reconciled_roles, divergence = reconcile(usage_roles, classified_tokens)

    recommendation = recommend(reconciled_roles, harvest.theme, _hover_hint(harvest))

    palette = ThemePalette(
        theme=harvest.theme,
        roles=reconciled_roles,
        recommendation=recommendation,
    )
    return _ThemeOutput(
        palette=palette,
        tokens=classified_tokens,
        status_colors=status_colors,
        third_party_colors=_third_party_colors(clusters),
        divergence=divergence,
        fit_score=fit_score,
    )


def _collapse_themes(ordered_themes: list[Theme], harvests: dict[Theme, Harvest]) -> list[Theme]:
    """Drop later themes whose render is perceptually identical to the primary's.

    A site that ignores ``prefers-color-scheme`` renders the same under every theme; there
    is no point reporting two identical palettes, so only the primary theme survives.
    """
    if len(ordered_themes) <= 1:
        return ordered_themes
    primary = ordered_themes[0]
    kept = [primary]
    for theme in ordered_themes[1:]:
        if not _near_identical(harvests[primary], harvests[theme]):
            kept.append(theme)
    return kept


def _near_identical(a: Harvest, b: Harvest) -> bool:
    """Whether two renders' dominant screenshot colors all match within the OKLab threshold."""
    bins_a = sorted(a.screenshot_bins, key=lambda s: s.area_fraction, reverse=True)
    bins_b = sorted(b.screenshot_bins, key=lambda s: s.area_fraction, reverse=True)
    top_a = bins_a[:_COLLAPSE_TOP_BINS]
    top_b = bins_b[:_COLLAPSE_TOP_BINS]
    if not top_a or not top_b:
        return False
    return all(
        min(delta_e(sb.color, ob.color) for ob in top_b) <= _COLLAPSE_DELTA_E for sb in top_a
    )


def _hover_hint(harvest: Harvest) -> Color | None:
    """The hover background of the largest hover-changing clickable element, if any."""
    best: Color | None = None
    best_area = 0.0
    for element in harvest.elements:
        if element.has_hover_color_change and element.hover_bg is not None:
            area = element.rect.w * element.rect.h
            if area > best_area:
                best_area = area
                best = element.hover_bg
    return best


def _third_party_colors(clusters: list[ColorCluster]) -> list[Color]:
    """Colors of clusters whose component mix is dominated by third-party widgets."""
    out: list[Color] = []
    for cluster in clusters:
        mix = cluster.component_mix
        if not mix:
            continue
        dominant = max(mix, key=lambda key: mix[key])
        if dominant is ComponentType.third_party:
            out.append(cluster.color)
    return _dedupe_colors(out)


def _dedupe_colors(colors: Iterable[Color]) -> list[Color]:
    """Order-preserving dedupe of colors by hex."""
    seen: set[str] = set()
    out: list[Color] = []
    for color in colors:
        if color.hex not in seen:
            seen.add(color.hex)
            out.append(color)
    return out


def _build_metadata(
    requested: list[Theme], kept: list[Theme], policy: PolitenessPolicy
) -> dict[str, str]:
    """Provenance for the run: themes requested vs analyzed, collapse flag, fetch policy."""
    return {
        "themes_requested": ",".join(str(theme) for theme in requested),
        "themes_analyzed": ",".join(str(theme) for theme in kept),
        "single_theme": str(len(kept) == 1).lower(),
        "user_agent": policy.user_agent,
        "respect_robots": str(policy.respect_robots).lower(),
    }


# ``RoleResults`` is re-exported for callers that build/inspect palettes without importing
# from ``models`` directly.
__all__ = ["DEFAULT_VIEWPORT", "RoleResults", "analyze"]
