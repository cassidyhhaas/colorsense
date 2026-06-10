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
``file://`` fixtures with no public network. The pure per-theme CPU work is offloaded to
worker threads (``asyncio.to_thread``) so it never blocks the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

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
    DivergenceItem,
    Harvest,
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


class AnalysisTimeoutError(TimeoutError):
    """Raised by :func:`analyze` when ``max_total_seconds`` expires.

    Subclasses the builtin :class:`TimeoutError`, so a generic ``except TimeoutError``
    still catches it. The offending URL and the configured budget are available as
    :attr:`url` and :attr:`max_total_seconds`.
    """

    def __init__(self, url: str, max_total_seconds: float) -> None:
        super().__init__(
            f"analysis of {url!r} exceeded its overall deadline of {max_total_seconds:g}s"
        )
        self.url = url
        self.max_total_seconds = max_total_seconds


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
    max_total_seconds: float | None = None,
    browser_args: tuple[str, ...] = (),
) -> AnalysisResult:
    """Analyze ``url`` and return a typed :class:`AnalysisResult`.

    Async-native: the requested themes are rendered concurrently — sharing one lazily
    launched headless Chromium, each theme in its own browser context — gated by
    ``politeness``; the rest of the pipeline is pure CPU work, offloaded
    to worker threads via ``asyncio.to_thread`` so the event loop stays responsive.
    Awaitable directly from an asyncio event loop (e.g. a FastAPI ``async def`` endpoint).

    Parameters
    ----------
    url:
        Page to analyze. Any ``http(s)`` fetch is gated by ``politeness``. ``file://`` URLs
        (used by the test suite) are an explicit opt-in via
        ``PolitenessPolicy(allow_file_urls=True)``; all other schemes are rejected.
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
        for authorization — see ``SECURITY.md``.
    max_total_seconds:
        Optional overall deadline for the entire call — every theme render *plus* the CPU
        classification — enforced via :class:`asyncio.timeout` (the SECURITY.md §2
        deadline, shipped as a knob). ``None`` (default) imposes no deadline, the previous
        behavior. On expiry, all in-flight renders are cancelled, the shared browser is
        closed on the way out, and :class:`AnalysisTimeoutError` is raised (a
        :class:`TimeoutError` subclass carrying the url and budget). Must be positive when
        set (``<= 0`` raises :class:`ValueError`).
    browser_args:
        Extra command-line arguments for the call's Chromium launch, appended to the
        library's own launch arguments and passed **verbatim** to Chromium (the library
        does not validate or allowlist the flags — mechanism, not policy). Every render of
        this call — all themes share one browser — launches with them. Canonical use case:
        ``browser_args=("--js-flags=--max-old-space-size=512",)`` caps each renderer
        process's V8 heap at 512 MB. Note this bounds the **JS heap only**, not total
        renderer memory; hard per-render memory/CPU caps are the container/cgroup layer's
        job (see ``SECURITY.md`` §2). Default ``()``: no extra arguments, behavior
        unchanged. Non-string entries (or a bare string) raise :class:`TypeError` before
        any render.

    Raises
    ------
    colorsense.net.politeness.UnsupportedSchemeError
        If the URL scheme is not fetchable under the policy: only ``http(s)`` by default;
        ``file://`` requires ``PolitenessPolicy(allow_file_urls=True)``; every other scheme
        is always rejected.
    colorsense.net.politeness.RobotsDisallowedError
        If ``robots.txt`` disallows the fetch and the policy respects it.
    colorsense.harvest.RenderError
        If the page fails to render or navigate (DNS, timeout, TLS, or navigation error).
    AnalysisTimeoutError
        If ``max_total_seconds`` is set and the whole analysis does not finish within it.
    """
    # Validate eagerly: a broken browser_args value must raise here, before any robots
    # fetch or render starts (and on the deadline path, before the timer even exists).
    extra_args = normalize_browser_args(browser_args)
    if max_total_seconds is None:
        return await _analyze(url, config_path, viewport, themes, politeness, extra_args)
    if max_total_seconds <= 0:
        raise ValueError("max_total_seconds must be positive (or None for no deadline)")
    try:
        async with asyncio.timeout(max_total_seconds) as deadline:
            return await _analyze(url, config_path, viewport, themes, politeness, extra_args)
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
) -> AnalysisResult:
    """The deadline-free body of :func:`analyze` (which owns ``max_total_seconds``).

    On cancellation (including an ``analyze`` deadline expiring mid-render), the
    ``async with SharedBrowser()`` below unwinds: the ``TaskGroup`` cancels in-flight
    renders, then ``SharedBrowser.__aexit__`` closes the browser — so no headless Chromium
    outlives a timed-out call.
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
                    asyncio.to_thread(_analyze_theme, harvests[theme], config, viewport)
                )
                for theme in kept_themes
            }
    except ExceptionGroup as eg:
        _reraise_first_leaf(eg)
    outputs: dict[Theme, _ThemeOutput] = {
        theme: task.result() for theme, task in analyze_tasks.items()
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


def _reraise_first_leaf(eg: ExceptionGroup[Exception]) -> NoReturn:
    """Re-raise the first leaf exception of a (possibly nested) exception group.

    ``analyze`` documents plain exceptions (``RobotsDisallowedError``, ``RenderError``,
    ``ValueError``) — the ``TaskGroup`` wrapping is an implementation detail and must not
    leak ``ExceptionGroup`` to callers. The leaf keeps its original traceback; the group is
    attached as ``__cause__`` so the full failure context stays inspectable.
    """
    leaf: BaseException = eg
    while isinstance(leaf, BaseExceptionGroup):
        leaf = leaf.exceptions[0]
    raise leaf from eg


def _analyze_theme(harvest: Harvest, config: Config, viewport: Viewport) -> _ThemeOutput:
    """Run the per-theme classify → inventory → roles → reconcile chain.

    Pure CPU over immutable inputs (no I/O, no shared mutable state); ``analyze`` runs it
    on a worker thread via ``asyncio.to_thread`` to keep the event loop responsive.
    """
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
