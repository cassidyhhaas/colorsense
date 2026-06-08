"""colorsense — analyze a website's color palette from its rendered pages.

Public API
----------
* :func:`analyze` — the one-call **async** pipeline: ``from colorsense import analyze``;
  ``result = await analyze(url)``.
* :class:`AnalysisResult` — the typed result it returns; its ``metadata`` is a typed
  :class:`RunMetadata`.
* :class:`Theme` — the color scheme enum; pass e.g. ``themes=(Theme.light, Theme.dark)``.
* :class:`RenderError` — raised by :func:`analyze` when a page fails to render/navigate.
* :class:`Config` / :func:`load_default_config` / :func:`load_config` — load and inspect
  the palette config (the bundled default, or your own YAML by path).
* :class:`PolitenessPolicy` — opt-in fetch policy (robots, rate limit, cache); the consumer
  owns authorization.
* :data:`LIGHT_AND_DARK` — pass as ``themes=`` to opt into dark-mode analysis (the default
  is light only).
"""

from __future__ import annotations

from colorsense.config import Config, load_config, load_default_config
from colorsense.harvest import RenderError
from colorsense.models import AnalysisResult, RunMetadata, Theme
from colorsense.net.politeness import PolitenessPolicy, RobotsDisallowedError
from colorsense.pipeline import LIGHT_AND_DARK, analyze

__all__ = [
    "LIGHT_AND_DARK",
    "AnalysisResult",
    "Config",
    "PolitenessPolicy",
    "RenderError",
    "RobotsDisallowedError",
    "RunMetadata",
    "Theme",
    "analyze",
    "load_config",
    "load_default_config",
]
