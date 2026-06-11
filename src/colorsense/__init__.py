"""colorsense — analyze a website's color palette from its rendered pages.

The top-level :mod:`colorsense` package is the **canonical public API**: everything needed
to call :func:`analyze` and to consume its typed result is re-exported here, so
``from colorsense import ...`` is the supported import path. The data contracts also live in
:mod:`colorsense.models`, but importing from the package root is preferred; internal modules
(e.g. ``colorsense.pipeline``) and the assembly models are not part of the public surface.

Entry point
-----------
* :func:`analyze` — the one-call **async** pipeline: ``result = await analyze(url)``.
* The ``colorsense`` console script (:mod:`colorsense.cli`) wraps it for the command
  line — a convenience entry point; its symbols are not re-exported here.

Result & contracts
------------------
* :class:`AnalysisResult` — the typed result, with :class:`RunMetadata` provenance.
* :class:`ThemePalette` — everything derived per theme. Its **primary view** is
  ``usage`` (:class:`UsagePalette` / :class:`UsageEntry` — what colors paint surfaces,
  text, interactive elements, and borders); ``roles`` (:class:`RoleResults` /
  :class:`PaletteCandidate`) is the **derived** measured-only 60/30/10 interpretation.
  ``divergence`` (:class:`DivergenceItem`) reports declared-vs-measured discrepancies,
  and ``tokens`` (:class:`DesignToken`) carries the declared design tokens — opt-in via
  ``analyze(..., include_tokens=True)`` (``None`` when not requested, ``()`` when
  requested but none declared).
* :class:`Color` / :class:`Viewport` — value types (``Viewport`` is also an ``analyze``
  argument).
* :class:`Theme`, :class:`UsageCategory`, :class:`PaletteRole`, :class:`ComponentType`,
  :class:`TokenSemanticRole` — the enums that key the result (e.g.
  ``usage.mapping[UsageCategory.surface]``, ``roles.mapping[PaletteRole.primary]``).

Inputs & policy
---------------
* :data:`LIGHT_AND_DARK` / :data:`DEFAULT_VIEWPORT` — presets for the ``themes`` and
  ``viewport`` arguments (the default is light only).
* :class:`Config` / :func:`load_default_config` / :func:`load_config` — load and inspect
  the palette config (the bundled default, or your own YAML by path).
* :class:`PolitenessPolicy` — opt-in fetch policy (robots, rate limit, cache, scheme gate,
  egress ``request_filter``, ``max_concurrent_renders`` render cap); the consumer owns
  authorization.
* :func:`block_private_networks` — factory for an async ``request_filter`` predicate that
  rejects non-public destinations (loopback, RFC 1918, link-local/metadata, CGNAT, ...),
  failing closed, resolving hostnames off the event loop; the shipped SECURITY.md §1
  egress filter.
* :data:`RequestFilter` — the type a ``request_filter`` must satisfy: a sync **or** async
  ``url -> bool`` predicate (sync runs inline on the event loop and must not block; async
  is awaited).
* :class:`RenderError` / :class:`RobotsDisallowedError` / :class:`UnsupportedSchemeError` /
  :class:`AnalysisTimeoutError` — raised by :func:`analyze` when a page fails to
  render/navigate, when ``robots.txt`` disallows the fetch, when the URL scheme is not
  fetchable under the policy (only ``http(s)`` by default; ``file://`` requires
  ``PolitenessPolicy(allow_file_urls=True)``), or when ``max_total_seconds`` expires.
"""

from __future__ import annotations

from colorsense.config import Config, load_config, load_default_config
from colorsense.harvest import RenderError, RequestFilter
from colorsense.models import (
    AnalysisResult,
    Color,
    ComponentType,
    DesignToken,
    DivergenceItem,
    PaletteCandidate,
    PaletteRole,
    RoleResults,
    RunMetadata,
    Theme,
    ThemePalette,
    TokenSemanticRole,
    UsageCategory,
    UsageEntry,
    UsagePalette,
    Viewport,
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
    "ComponentType",
    "Config",
    "DesignToken",
    "DivergenceItem",
    "PaletteCandidate",
    "PaletteRole",
    "PolitenessPolicy",
    "RenderError",
    "RequestFilter",
    "RobotsDisallowedError",
    "RoleResults",
    "RunMetadata",
    "Theme",
    "ThemePalette",
    "TokenSemanticRole",
    "UnsupportedSchemeError",
    "UsageCategory",
    "UsageEntry",
    "UsagePalette",
    "Viewport",
    "analyze",
    "block_private_networks",
    "load_config",
    "load_default_config",
]
