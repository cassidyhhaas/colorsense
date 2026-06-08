"""The end-to-end pipeline and public ``analyze`` entry point.

``analyze`` wires every stage into one typed call: render each requested
theme (gated by :class:`~colorsense.net.politeness.PolitenessPolicy`), classify tokens
and components, build a color inventory, assign palette roles, and
reconcile usage against declared intent — per theme. Sites that ignore
``prefers-color-scheme`` (near-identical light/dark renders)
are collapsed to a single theme. The whole thing is assembled into a typed
:class:`~colorsense.models.AnalysisResult`.

Networking lives entirely behind ``PolitenessPolicy``/``harvest_page``; everything else is
pure given a :class:`~colorsense.models.Harvest`, so tests drive the pipeline against local
``file://`` fixtures with no public network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from colorsense.classify.components import classify_components
from colorsense.classify.tokens import classify_tokens
from colorsense.color.primitives import delta_e
from colorsense.config import Config, load_config, load_default_config
from colorsense.models import (
    AnalysisResult,
    ClassifiedToken,
    Color,
    ColorCluster,
    ComponentType,
    DivergenceItem,
    Harvest,
    RoleResults,
    RunMetadata,
    Theme,
    ThemePalette,
    Viewport,
)
from colorsense.net.politeness import PolitenessPolicy
from colorsense.palette.inventory import build_inventory
from colorsense.palette.reconcile import reconcile
from colorsense.palette.roles import assign_roles

DEFAULT_VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)

# Light only by default: most sites ship no dark mode, and a second theme roughly doubles
# render cost (a whole extra headless render). A consumer analyzing their own (or a client's)
# site knows whether dark mode exists, so dark is opt-in via ``themes=LIGHT_AND_DARK``.
DEFAULT_THEMES: tuple[Theme, ...] = (Theme.light,)
LIGHT_AND_DARK: tuple[Theme, ...] = (Theme.light, Theme.dark)

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
    config_path: str | Path | None = None,
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
        Path to a palette config YAML to override the default. When ``None`` (the default)
        the configuration bundled with the package is used, so no file needs to exist on
        disk. Copy the bundled ``data/palette_config.yaml`` and pass its path here to tune.
    viewport:
        Render viewport; defaults to 1280x800 at 1x scale.
    themes:
        Themes to render, in priority order (the first is "primary" and supplies the
        top-level token/divergence/fit-score fields). Duplicates are ignored. Defaults to
        **light only** — most sites have no dark mode and a second theme roughly doubles the
        work. Pass ``themes=(Theme.light, Theme.dark)`` (or the exported
        :data:`LIGHT_AND_DARK`) to also analyze dark mode; near-identical renders still
        collapse to a single reported theme.
    politeness:
        Fetch policy (robots gate, rate limit, render cache). A conservative default
        :class:`PolitenessPolicy` is created when omitted. The **consumer** is responsible
        for authorization — see the README.

    Raises
    ------
    colorsense.net.politeness.RobotsDisallowedError
        If ``robots.txt`` disallows the fetch and the policy respects it.
    colorsense.harvest.RenderError
        If the page fails to render or navigate (DNS, timeout, TLS, or navigation error).
    """
    config = load_default_config() if config_path is None else load_config(config_path)
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
        tokens=tuple(primary.tokens),
        third_party_colors=tuple(
            _dedupe_colors(color for out in outputs.values() for color in out.third_party_colors)
        ),
        status_colors=tuple(
            _dedupe_colors(color for out in outputs.values() for color in out.status_colors)
        ),
        divergence=tuple(primary.divergence),
        fit_score=primary.fit_score,
        metadata=_build_metadata(ordered_themes, kept_themes, policy),
    )


def _analyze_theme(harvest: Harvest, config: Config, viewport: Viewport) -> _ThemeOutput:
    """Run the per-theme classify → inventory → roles → reconcile chain."""
    classified_tokens, status_colors = classify_tokens(harvest.tokens, config)
    classified_elements = classify_components(harvest.elements, config, viewport)
    clusters = build_inventory(harvest, classified_elements)

    usage_roles, fit_score = assign_roles(clusters)
    reconciled_roles, divergence = reconcile(usage_roles, classified_tokens)

    palette = ThemePalette(theme=harvest.theme, roles=reconciled_roles)
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
    """Whether two renders' dominant screenshot colors mutually match within the threshold.

    The two top-bin sets must agree *symmetrically*: every dominant bin of ``a`` has a close
    perceptual match in ``b`` **and** vice versa. The one-directional form would wrongly
    collapse a theme whose dominant colors are a superset of the primary's (e.g. a dark mode
    that keeps the light background somewhere on the page) — symmetry guards against that.

    Sorting and the inner matching break ``area_fraction`` ties deterministically on the
    color hex so the result never depends on incidental bin insertion order.
    """
    bins_a = sorted(a.screenshot_bins, key=lambda s: (-s.area_fraction, s.color.hex))
    bins_b = sorted(b.screenshot_bins, key=lambda s: (-s.area_fraction, s.color.hex))
    top_a = bins_a[:_COLLAPSE_TOP_BINS]
    top_b = bins_b[:_COLLAPSE_TOP_BINS]
    if not top_a or not top_b:
        return False
    a_matches_b = all(
        min(delta_e(sb.color, ob.color) for ob in top_b) <= _COLLAPSE_DELTA_E for sb in top_a
    )
    b_matches_a = all(
        min(delta_e(sb.color, ob.color) for ob in top_a) <= _COLLAPSE_DELTA_E for sb in top_b
    )
    return a_matches_b and b_matches_a


def _third_party_colors(clusters: list[ColorCluster]) -> list[Color]:
    """Colors of clusters whose component mix is dominated by third-party widgets."""
    out: list[Color] = []
    for cluster in clusters:
        mix = cluster.component_mix
        if not mix:
            continue
        # Stable secondary key (the component-type value) so ties don't depend on dict order.
        dominant = max(mix, key=lambda key: (mix[key], key.value))
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
) -> RunMetadata:
    """Provenance for the run: themes requested vs analyzed, collapse flag, fetch policy."""
    return RunMetadata(
        themes_requested=tuple(requested),
        themes_analyzed=tuple(kept),
        single_theme=len(kept) == 1,
        user_agent=policy.user_agent,
        respect_robots=policy.respect_robots,
    )


# ``RoleResults`` is re-exported for callers that build/inspect palettes without importing
# from ``models`` directly.
__all__ = ["DEFAULT_THEMES", "DEFAULT_VIEWPORT", "LIGHT_AND_DARK", "RoleResults", "analyze"]
