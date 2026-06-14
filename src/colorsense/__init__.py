"""colorsense — analyze a website's color palette from its rendered pages.

The top-level `colorsense` package is the **canonical public API**: everything needed
to call [`analyze`][colorsense.analyze] and to consume its typed result is re-exported here, so
``from colorsense import ...`` is the supported import path. The data contracts also live in
`colorsense.models`, but importing from the package root is preferred; internal modules
(e.g. ``colorsense.pipeline``) and the assembly models are not part of the public surface.

Entry point
-----------
* [`analyze`][colorsense.analyze] — the one-call **async** pipeline: ``result = await
  analyze(url)``.
* The ``colorsense`` console script (`colorsense.cli`) wraps it for the command
  line — a convenience entry point; its symbols are not re-exported here.

Result & contracts
------------------
* [`AnalysisResult`][colorsense.AnalysisResult] — the typed result, with
  [`RunMetadata`][colorsense.RunMetadata] provenance.
* [`ThemePalette`][colorsense.ThemePalette] — everything derived per theme. Its **canonical
  index** is ``colors`` (a [`ColorUsage`][colorsense.ColorUsage] tuple of measured colors, each
  carrying its [`Usage`][colorsense.Usage] slots — "how each color is used"); its **role-keyed
  projection** is ``usage`` ([`UsagePalette`][colorsense.UsagePalette] /
  [`UsageEntry`][colorsense.UsageEntry] — "which colors paint each usage role"); its **secondary**
  view is ``composition`` ([`Composition`][colorsense.Composition] /
  [`PaletteCandidate`][colorsense.PaletteCandidate]), the demoted measured-only 60/30/10
  interpretation with its ``fit_score``. ``divergence``
  ([`DivergenceItem`][colorsense.DivergenceItem]) reports declared-vs-measured discrepancies, and
  ``tokens`` ([`DesignToken`][colorsense.DesignToken]) carries the declared design tokens — opt-in
  via ``analyze(..., include_tokens=True)`` (``None`` when not requested, ``()`` when requested but
  no usable color tokens were found: pages that declare none, and pages whose declarations are all
  non-color or ignore-classified, both yield ``()``).
* [`Color`][colorsense.Color] / [`Viewport`][colorsense.Viewport] — value types (``Viewport`` is
  also an ``analyze`` argument).
* [`Theme`][colorsense.Theme], [`UsageRole`][colorsense.UsageRole],
  [`PropertyFamily`][colorsense.PropertyFamily] (with the
  [`family_of`][colorsense.family_of] role→family helper),
  [`PaletteRole`][colorsense.PaletteRole], [`ComponentType`][colorsense.ComponentType],
  [`TokenSemanticRole`][colorsense.TokenSemanticRole] — the enums that key the result (e.g.
  ``usage.mapping[UsageRole.cta]``, ``composition.roles[PaletteRole.primary]``).

Inputs & policy
---------------
* [`LIGHT_AND_DARK`][colorsense.LIGHT_AND_DARK] / [`DEFAULT_VIEWPORT`][colorsense.DEFAULT_VIEWPORT]
  — presets for the ``themes`` and ``viewport`` arguments (the default is light only).
* [`Config`][colorsense.Config] / [`load_default_config`][colorsense.load_default_config] /
  [`load_config`][colorsense.load_config] — load and inspect the palette config (the bundled
  default, or your own YAML by path).
* [`PolitenessPolicy`][colorsense.PolitenessPolicy] — opt-in fetch policy (robots, rate limit,
  cache, scheme gate, egress ``request_filter``, ``max_concurrent_renders`` render cap); the
  consumer owns authorization.
* [`block_private_networks`][colorsense.block_private_networks] — factory for an async
  ``request_filter`` predicate that rejects non-public destinations (loopback, RFC 1918,
  link-local/metadata, CGNAT, ...), failing closed, resolving hostnames off the event loop; the
  shipped SECURITY.md §1 egress filter.
* [`RequestFilter`][colorsense.RequestFilter] — the type a ``request_filter`` must satisfy: a sync
  **or** async ``url -> bool`` predicate (sync runs inline on the event loop and must not block;
  async is awaited).
* [`RenderError`][colorsense.RenderError] /
  [`RobotsDisallowedError`][colorsense.RobotsDisallowedError] /
  [`UnsupportedSchemeError`][colorsense.UnsupportedSchemeError] /
  [`AnalysisTimeoutError`][colorsense.AnalysisTimeoutError] — raised by
  [`analyze`][colorsense.analyze] when a page fails to render/navigate, when ``robots.txt``
  disallows the fetch, when the URL scheme is not fetchable under the policy (only ``http(s)`` by
  default; ``file://`` requires ``PolitenessPolicy(allow_file_urls=True)``), or when
  ``max_total_seconds`` expires.
"""

from __future__ import annotations

from colorsense.config import Config, load_config, load_default_config
from colorsense.harvest import RenderError, RequestFilter
from colorsense.models import (
    AnalysisResult,
    Color,
    ColorUsage,
    ComponentType,
    Composition,
    DesignToken,
    DivergenceItem,
    PaletteCandidate,
    PaletteRole,
    PropertyFamily,
    RunMetadata,
    Theme,
    ThemePalette,
    TokenSemanticRole,
    Usage,
    UsageEntry,
    UsagePalette,
    UsageRole,
    Viewport,
    family_of,
)
from colorsense.net.guard import block_private_networks
from colorsense.net.politeness import (
    PolitenessPolicy,
    RobotsDisallowedError,
    UnsupportedSchemeError,
)
from colorsense.pipeline import DEFAULT_VIEWPORT, LIGHT_AND_DARK, AnalysisTimeoutError, analyze

__all__ = [
    "DEFAULT_VIEWPORT",
    "LIGHT_AND_DARK",
    "AnalysisResult",
    "AnalysisTimeoutError",
    "Color",
    "ColorUsage",
    "ComponentType",
    "Composition",
    "Config",
    "DesignToken",
    "DivergenceItem",
    "PaletteCandidate",
    "PaletteRole",
    "PolitenessPolicy",
    "PropertyFamily",
    "RenderError",
    "RequestFilter",
    "RobotsDisallowedError",
    "RunMetadata",
    "Theme",
    "ThemePalette",
    "TokenSemanticRole",
    "UnsupportedSchemeError",
    "Usage",
    "UsageEntry",
    "UsagePalette",
    "UsageRole",
    "Viewport",
    "analyze",
    "block_private_networks",
    "family_of",
    "load_config",
    "load_default_config",
]
