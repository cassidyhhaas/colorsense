"""The end-to-end pipeline and public ``analyze`` entry point.

``analyze`` wires every stage into one typed call: render each requested
theme (gated by [`PolitenessPolicy`][colorsense.PolitenessPolicy]), classify tokens
and components, fuse per-``(color, role)`` evidence, then detect-plus-rank to build the
color-keyed index, role-keyed usage view, and divergence report — per theme.
Sites that ignore ``prefers-color-scheme`` (near-identical light/dark renders)
are collapsed to a single theme. The whole thing is assembled into a typed
[`AnalysisResult`][colorsense.AnalysisResult].

Networking lives entirely behind ``PolitenessPolicy``/``harvest_page``; everything else is
pure given a `Harvest`, so tests drive the pipeline against local
``file://`` fixtures with no public network. The pure per-theme CPU work is offloaded to
worker threads (``asyncio.to_thread``) so it never blocks the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from colorsense._util import dedupe_by
from colorsense.classify.components import classify_components
from colorsense.classify.tokens import classify_tokens
from colorsense.color.primitives import delta_e
from colorsense.config import Config, load_config, load_default_config
from colorsense.harvest import SharedBrowser
from colorsense.harvest.render import normalize_browser_args
from colorsense.models import (
    AnalysisResult,
    ClassifiedToken,
    Color,
    ColorCluster,
    ComponentType,
    DesignToken,
    Harvest,
    RunMetadata,
    Theme,
    ThemePalette,
    TokenSemanticRole,
    Viewport,
)
from colorsense.net.politeness import PolitenessPolicy
from colorsense.palette.detect import detect
from colorsense.palette.fusion import build_evidence
from colorsense.palette.inventory import build_inventory

DEFAULT_VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)

# Light only by default: most sites ship no dark mode, and a second theme roughly doubles
# render cost (a whole extra headless render). A consumer analyzing their own (or a client's)
# site knows whether dark mode exists, so dark is opt-in via ``themes=LIGHT_AND_DARK``.
DEFAULT_THEMES: tuple[Theme, ...] = (Theme.LIGHT,)
LIGHT_AND_DARK: tuple[Theme, ...] = (Theme.LIGHT, Theme.DARK)

# Two renders are "the same site" when every dominant screenshot bin in one has a close
# perceptual match in the other. A genuine dark mode flips the large-area background bin,
# pushing it well past this OKLab threshold; a non-responsive site stays under it.
_COLLAPSE_DELTA_E = 0.06
_COLLAPSE_TOP_BINS = 4


class AnalysisTimeoutError(TimeoutError):
    """Raised by [`analyze`][colorsense.analyze] when ``max_total_seconds`` expires.

    Subclasses the builtin `TimeoutError`, so a generic ``except TimeoutError``
    still catches it.

    Attributes:
        url: The URL whose analysis exceeded the deadline.
        max_total_seconds: The configured overall deadline, in seconds.

    """

    def __init__(self, url: str, max_total_seconds: float) -> None:
        """Build the error from the offending URL and the configured budget.

        Args:
            url: The URL whose analysis exceeded the deadline.
            max_total_seconds: The configured overall deadline, in seconds.

        """
        super().__init__(
            f"analysis of {url!r} exceeded its overall deadline of {max_total_seconds:g}s"
        )
        self.url = url
        self.max_total_seconds = max_total_seconds


@dataclass
class _ThemeOutput:
    """Everything derived for one rendered theme.

    The per-theme analysis (color-keyed index, role-keyed usage view, divergence, tokens)
    lives on the [`ThemePalette`][colorsense.ThemePalette] itself; only the cross-theme
    aggregates ride alongside.

    Attributes:
        palette: The fully-derived palette for the theme.
        third_party_colors: Colors attributed to third-party widgets, carried alongside the
            palette for cross-theme aggregation.

    """

    palette: ThemePalette
    third_party_colors: list[Color]


async def analyze(
    url: str,
    *,
    config_path: str | Path | None = None,
    viewport: Viewport = DEFAULT_VIEWPORT,
    themes: tuple[Theme, ...] = DEFAULT_THEMES,
    politeness: PolitenessPolicy | None = None,
    max_total_seconds: float | None = None,
    browser_args: tuple[str, ...] = (),
    include_tokens: bool = False,
) -> AnalysisResult:
    """Analyze ``url`` and return a typed [`AnalysisResult`][colorsense.AnalysisResult].

    Async-native: the requested themes are rendered concurrently — sharing one lazily
    launched headless Chromium, each theme in its own browser context — gated by
    ``politeness``; the rest of the pipeline is pure CPU work, offloaded
    to worker threads via ``asyncio.to_thread`` so the event loop stays responsive.
    Awaitable directly from an asyncio event loop (e.g. a FastAPI ``async def`` endpoint).

    Args:
        url: Page to analyze. Any ``http(s)`` fetch is gated by ``politeness``. ``file://``
            URLs (used by the test suite) are an explicit opt-in via
            ``PolitenessPolicy(allow_file_urls=True)``; all other schemes are rejected.
        config_path: Path to a palette config YAML to override the default. When ``None``
            (the default) the configuration bundled with the package is used, so no file
            needs to exist on disk. Copy the bundled ``data/palette_config.yaml`` and pass
            its path here to tune.
        viewport: Render viewport; defaults to 1280x800 at 1x scale.
        themes: Themes to render, in priority order (the first is "primary": it is the theme
            kept when near-identical renders collapse). Duplicates are ignored. Defaults to
            **light only** — most sites have no dark mode and a second theme roughly doubles
            the work. Pass ``themes=(Theme.LIGHT, Theme.DARK)`` (or the exported
            [`LIGHT_AND_DARK`][colorsense.LIGHT_AND_DARK]) to also analyze dark mode;
            near-identical renders still collapse to a single reported theme.
        politeness: Fetch policy (robots gate, rate limit, render cache). A conservative
            default [`PolitenessPolicy`][colorsense.PolitenessPolicy] is created when
            omitted. The **consumer** is responsible for authorization — see ``SECURITY.md``.
        max_total_seconds: Optional overall deadline for the entire call — every theme render
            *plus* the CPU classification — enforced via `asyncio.timeout` (the SECURITY.md §2
            deadline, shipped as a knob). ``None`` (default) imposes no deadline, the previous
            behavior. On expiry, all in-flight renders are cancelled, the shared browser is
            closed on the way out, and
            [`AnalysisTimeoutError`][colorsense.AnalysisTimeoutError] is raised (a
            `TimeoutError` subclass carrying the url and budget). Must be positive when set
            (``<= 0`` raises `ValueError`).
        browser_args: Extra command-line arguments for the call's Chromium launch, appended
            to the library's own launch arguments and passed **verbatim** to Chromium (the
            library does not validate or allowlist the flags — mechanism, not policy). Every
            render of this call — all themes share one browser — launches with them.
            Canonical use case: ``browser_args=("--js-flags=--max-old-space-size=512",)``
            caps each renderer process's V8 heap at 512 MB. Note this bounds the **JS heap
            only**, not total renderer memory; hard per-render memory/CPU caps are the
            container/cgroup layer's job (see ``SECURITY.md`` §2). Default ``()``: no extra
            arguments, behavior unchanged. Non-string entries (or a bare string) raise
            `TypeError` before any render.
        include_tokens: When ``True``, each [`ThemePalette`][colorsense.ThemePalette] carries
            its declared design tokens as ``tokens`` (a tuple of
            [`DesignToken`][colorsense.DesignToken]; ``()`` when no usable color tokens were
            found — none declared, or all declarations filtered as
            non-color/ignore/zero-weight). When ``False`` (the default) ``tokens`` is
            ``None``. The flag gates **only output assembly**: token classification and
            reconciliation always run, so every other field is identical either way.

    Returns:
        A typed [`AnalysisResult`][colorsense.AnalysisResult] with the per-theme palettes,
        the cross-theme third-party colors, and the run metadata.

    Raises:
        colorsense.net.politeness.UnsupportedSchemeError: If the URL scheme is not fetchable
            under the policy: only ``http(s)`` by default; ``file://`` requires
            ``PolitenessPolicy(allow_file_urls=True)``; every other scheme is always rejected.
        colorsense.net.politeness.RobotsDisallowedError: If ``robots.txt`` disallows the
            fetch and the policy respects it.
        colorsense.harvest.RenderError: If the page fails to render or navigate (DNS,
            timeout, TLS, or navigation error).
        AnalysisTimeoutError: If ``max_total_seconds`` is set and the whole analysis does not
            finish within it.

    """
    # Validate eagerly: a broken browser_args value must raise here, before any robots
    # fetch or render starts (and on the deadline path, before the timer even exists).
    extra_args = normalize_browser_args(browser_args)
    if max_total_seconds is None:
        return await _analyze(
            url, config_path, viewport, themes, politeness, extra_args, include_tokens
        )
    if max_total_seconds <= 0:
        raise ValueError("max_total_seconds must be positive (or None for no deadline)")
    try:
        async with asyncio.timeout(max_total_seconds) as deadline:
            return await _analyze(
                url, config_path, viewport, themes, politeness, extra_args, include_tokens
            )
    except TimeoutError as err:
        # Only OUR deadline expiring becomes AnalysisTimeoutError; any other TimeoutError
        # surfacing from inside the pipeline propagates untranslated.
        if deadline.expired():
            raise AnalysisTimeoutError(url, max_total_seconds) from err
        raise


async def _analyze(
    url: str,
    config_path: str | Path | None,
    viewport: Viewport,
    themes: tuple[Theme, ...],
    politeness: PolitenessPolicy | None,
    browser_args: tuple[str, ...],
    include_tokens: bool,
) -> AnalysisResult:
    """Run the deadline-free body of [`analyze`][colorsense.analyze] (owns ``max_total_seconds``).

    On cancellation (including an ``analyze`` deadline expiring mid-render), the
    ``async with SharedBrowser()`` below unwinds: the ``TaskGroup`` cancels in-flight
    renders, then ``SharedBrowser.__aexit__`` closes the browser — so no headless Chromium
    outlives a timed-out call.

    Returns:
        The assembled [`AnalysisResult`][colorsense.AnalysisResult] for the run.

    """
    config = load_default_config() if config_path is None else load_config(config_path)
    policy = politeness if politeness is not None else PolitenessPolicy()

    ordered_themes = list(dict.fromkeys(themes))
    if not ordered_themes:
        raise ValueError("analyze() requires at least one theme")

    # Render every requested theme concurrently; the per-host rate limiter inside
    # ``policy.fetch`` still spaces the underlying navigations. ``TaskGroup`` (unlike a bare
    # ``gather``) cancels sibling in-flight renders as soon as one fetch fails, so a robots
    # block or render error doesn't leave an abandoned headless Chromium running. All themes
    # share ONE lazily-launched Chromium (each render opens its own browser context inside
    # it), so a multi-theme analysis pays a single browser launch — and a run whose fetches
    # are all cache hits pays none. The ``async with`` closes the shared browser as soon as
    # the renders finish (before the CPU phase), including on the exception path.
    try:
        async with (
            SharedBrowser(browser_args=browser_args) as shared_browser,
            asyncio.TaskGroup() as tg,
        ):
            fetch_tasks = [
                tg.create_task(policy.fetch(url, theme, config, viewport, browser=shared_browser))
                for theme in ordered_themes
            ]
    except ExceptionGroup as eg:
        _reraise_first_leaf(eg)
    harvests: dict[Theme, Harvest] = {
        theme: task.result() for theme, task in zip(ordered_themes, fetch_tasks, strict=True)
    }

    kept_themes = _collapse_themes(ordered_themes, harvests)

    # The per-theme analysis is pure CPU (O(n^2) perceptual clustering) over immutable
    # inputs; offload each kept theme to a worker thread so the event loop stays free,
    # running them concurrently.
    try:
        async with asyncio.TaskGroup() as tg:
            analyze_tasks = {
                theme: tg.create_task(
                    asyncio.to_thread(
                        _analyze_theme, harvests[theme], config, viewport, include_tokens
                    )
                )
                for theme in kept_themes
            }
    except ExceptionGroup as eg:
        _reraise_first_leaf(eg)
    outputs: dict[Theme, _ThemeOutput] = {
        theme: task.result() for theme, task in analyze_tasks.items()
    }

    return AnalysisResult(
        url=url,
        viewport=viewport,
        themes={theme: out.palette for theme, out in outputs.items()},
        third_party_colors=tuple(
            _dedupe_colors(color for out in outputs.values() for color in out.third_party_colors)
        ),
        metadata=_build_metadata(ordered_themes, kept_themes, policy),
    )


def _reraise_first_leaf(eg: ExceptionGroup[Exception]) -> NoReturn:
    """Re-raise the first leaf exception of a (possibly nested) exception group.

    ``analyze`` documents plain exceptions (``RobotsDisallowedError``, ``RenderError``,
    ``ValueError``) — the ``TaskGroup`` wrapping is an implementation detail and must not
    leak ``ExceptionGroup`` to callers. The leaf keeps its original traceback; the group is
    attached as ``__cause__`` so the full failure context stays inspectable.

    Args:
        eg: The (possibly nested) exception group raised by a ``TaskGroup``.

    Raises:
        Exception: The first leaf exception of ``eg``, with ``eg`` attached as ``__cause__``.

    """
    leaf: BaseException = eg
    while isinstance(leaf, BaseExceptionGroup):
        leaf = leaf.exceptions[0]
    raise leaf from eg


def _analyze_theme(
    harvest: Harvest, config: Config, viewport: Viewport, include_tokens: bool
) -> _ThemeOutput:
    """Run the per-theme classify → fuse evidence → detect (rank + intent) chain.

    Pure CPU over immutable inputs (no I/O, no shared mutable state); ``analyze`` runs it
    on a worker thread via ``asyncio.to_thread`` to keep the event loop responsive.

    Args:
        harvest: Everything extracted from the rendered page for this theme.
        config: The loaded palette configuration driving all classifier weights.
        viewport: The viewport the page was rendered at.
        include_tokens: Whether to attach declared design tokens to the palette.

    Returns:
        The derived palette plus this theme's third-party colors.

    """
    classified_tokens = classify_tokens(harvest.tokens, config)
    classified_elements = classify_components(harvest.elements, config, viewport)

    # Detection-plus-ranking: fuse per-(color, role) evidence, then detect on absolute
    # evidence, rank survivors, fold declared intent in as a bounded multiplier, and
    # normalize only for display. The color-keyed index, role-keyed projection, and
    # divergences all fall out of the one detection pass.
    evidence = build_evidence(harvest, classified_elements, config, viewport)
    usage, color_index, divergence = detect(evidence, classified_tokens, config)

    # Third-party-dominated colors carry no usage role (so they never enter the views) and
    # ride on AnalysisResult.third_party_colors instead. They are still read off the cluster
    # view, whose component mix records third-party dominance.
    clusters = build_inventory(harvest, classified_elements)
    palette = ThemePalette(
        theme=harvest.theme,
        colors=color_index,
        usage=usage,
        divergence=tuple(divergence),
        tokens=_design_tokens(classified_tokens) if include_tokens else None,
    )
    return _ThemeOutput(
        palette=palette,
        third_party_colors=_third_party_colors(clusters),
    )


def _design_tokens(classified: list[ClassifiedToken]) -> tuple[DesignToken, ...]:
    """Project classified tokens onto the public [`DesignToken`][colorsense.DesignToken] shape.

    Keeps only meaningful tokens: a resolved color present, a semantic role other than
    ``ignore``, and a positive classification weight. Dedupes by name keeping the FIRST
    occurrence in document order — the harvester resolves every record against the
    rendered ``:root``, so duplicate-name records share one resolved color and dropping
    later ones loses nothing. Sorted by name for stable output.

    Args:
        classified: The classified tokens to project, in document order.

    Returns:
        The meaningful tokens as public [`DesignToken`][colorsense.DesignToken]s, deduped by
        name and sorted by name.

    """
    meaningful = [
        token
        for token in classified
        if token.record.resolved is not None
        and token.semantic_role is not TokenSemanticRole.IGNORE
        and token.weight > 0.0
    ]
    # Dedupe by name keeping the first meaningful occurrence (the per-token filters above
    # are independent of the dedup, so filter-then-dedupe matches the former interleaved
    # ``seen`` set exactly).
    deduped = dedupe_by(meaningful, key=lambda t: t.record.name)
    out = [
        DesignToken(
            name=token.record.name,
            color=token.record.resolved,
            semantic_role=token.semantic_role,
        )
        for token in deduped
        if token.record.resolved is not None
    ]
    out.sort(key=lambda t: t.name)
    return tuple(out)


def _collapse_themes(ordered_themes: list[Theme], harvests: dict[Theme, Harvest]) -> list[Theme]:
    """Drop later themes whose render is perceptually identical to the primary's.

    A site that ignores ``prefers-color-scheme`` renders the same under every theme; there
    is no point reporting two identical palettes, so only the primary theme survives.

    Args:
        ordered_themes: The requested themes in priority order (the first is primary).
        harvests: The harvested render of each ordered theme.

    Returns:
        The themes to analyze: the primary plus every later theme that differs from it.

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

    Args:
        a: One render's harvest (compared symmetrically against ``b``).
        b: The other render's harvest.

    Returns:
        ``True`` if the two renders' dominant screenshot colors match symmetrically within
        the collapse threshold, else ``False``.

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
    """Colors of clusters whose component mix is dominated by third-party widgets.

    Args:
        clusters: The color clusters built for the theme.

    Returns:
        The deduped colors of clusters whose dominant component is third-party.

    """
    out: list[Color] = []
    for cluster in clusters:
        mix = cluster.component_mix
        if not mix:
            continue
        # Stable secondary key (the component-type value) so ties don't depend on dict order.
        dominant = max(mix, key=lambda key: (mix[key], key.value))
        if dominant is ComponentType.THIRD_PARTY:
            out.append(cluster.color)
    return _dedupe_colors(out)


def _dedupe_colors(colors: Iterable[Color]) -> list[Color]:
    """Order-preserving dedupe of colors by hex.

    Args:
        colors: The colors to dedupe.

    Returns:
        The colors with later duplicates (by hex) removed, in first-seen order.

    """
    return dedupe_by(colors, key=lambda c: c.hex)


def _build_metadata(
    requested: list[Theme], kept: list[Theme], policy: PolitenessPolicy
) -> RunMetadata:
    """Provenance for the run: themes requested vs analyzed, and the fetch policy.

    Args:
        requested: The themes the caller requested, in priority order.
        kept: The themes actually analyzed after near-identical renders collapsed.
        policy: The fetch policy in effect for the run.

    Returns:
        The assembled [`RunMetadata`][colorsense.RunMetadata] for the run.

    """
    return RunMetadata(
        themes_requested=tuple(requested),
        themes_analyzed=tuple(kept),
        user_agent=policy.user_agent,
        respect_robots=policy.respect_robots,
    )


# ``colorsense.pipeline`` is an internal orchestration module; the supported public surface
# is the top-level ``colorsense`` package (see ``colorsense.__all__``). ``__all__`` here only
# scopes ``from colorsense.pipeline import *`` and the names the package facade re-exports.
__all__ = [
    "DEFAULT_THEMES",
    "DEFAULT_VIEWPORT",
    "LIGHT_AND_DARK",
    "AnalysisTimeoutError",
    "analyze",
]
