"""colorsense — analyze a website's color palette from its rendered pages.

The top-level :mod:`colorsense` package is the **canonical public API**: everything needed
to call :func:`analyze` and to consume its typed result is re-exported here, so
``from colorsense import ...`` is the supported import path. The data contracts also live in
:mod:`colorsense.models`, but importing from the package root is preferred; internal modules
(e.g. ``colorsense.pipeline``) and the assembly models are not part of the public surface.

Entry point
-----------
* :func:`analyze` — the one-call **async** pipeline: ``result = await analyze(url)``.

Result & contracts
------------------
* :class:`AnalysisResult` — the typed result, with :class:`RunMetadata` provenance.
* :class:`ThemePalette`, :class:`RoleResults`, :class:`PaletteCandidate`,
  :class:`DivergenceItem`, :class:`ClassifiedToken`, :class:`TokenRecord` — the models
  reachable when navigating a result.
* :class:`Color` / :class:`Viewport` — value types (``Viewport`` is also an ``analyze``
  argument).
* :class:`Theme`, :class:`PaletteRole`, :class:`TokenSemanticRole` — the enums that key the
  result (e.g. ``roles.mapping[PaletteRole.primary]``).

Inputs & policy
---------------
* :data:`LIGHT_AND_DARK` / :data:`DEFAULT_VIEWPORT` — presets for the ``themes`` and
  ``viewport`` arguments (the default is light only).
* :class:`Config` / :func:`load_default_config` / :func:`load_config` — load and inspect
  the palette config (the bundled default, or your own YAML by path).
* :class:`PolitenessPolicy` — opt-in fetch policy (robots, rate limit, cache); the consumer
  owns authorization.
* :class:`RenderError` / :class:`RobotsDisallowedError` — raised by :func:`analyze` when a
  page fails to render/navigate, or when ``robots.txt`` disallows the fetch.
"""

from __future__ import annotations

from colorsense.config import Config, load_config, load_default_config
from colorsense.harvest import RenderError
from colorsense.models import (
    AnalysisResult,
    ClassifiedToken,
    Color,
    DivergenceItem,
    PaletteCandidate,
    PaletteRole,
    RoleResults,
    RunMetadata,
    Theme,
    ThemePalette,
    TokenRecord,
    TokenSemanticRole,
    Viewport,
)
from colorsense.net.politeness import PolitenessPolicy, RobotsDisallowedError
from colorsense.pipeline import DEFAULT_VIEWPORT, LIGHT_AND_DARK, analyze

__all__ = [
    "DEFAULT_VIEWPORT",
    "LIGHT_AND_DARK",
    "AnalysisResult",
    "ClassifiedToken",
    "Color",
    "Config",
    "DivergenceItem",
    "PaletteCandidate",
    "PaletteRole",
    "PolitenessPolicy",
    "RenderError",
    "RobotsDisallowedError",
    "RoleResults",
    "RunMetadata",
    "Theme",
    "ThemePalette",
    "TokenRecord",
    "TokenSemanticRole",
    "Viewport",
    "analyze",
    "load_config",
    "load_default_config",
]
