"""colorsense — analyze a website's color palette from its rendered pages.

Public API
----------
* :func:`analyze` — the one-call **async** pipeline: ``from colorsense import analyze``;
  ``result = await analyze(url)``.
* :class:`AnalysisResult` — the typed result it returns.
* :class:`Config` / :func:`load_default_config` / :func:`load_config` — load and inspect
  the palette config (the bundled default, or your own YAML by path).
* :class:`PolitenessPolicy` — opt-in fetch policy (robots, rate limit, cache); the consumer
  owns authorization.
* :data:`LIGHT_AND_DARK` — pass as ``themes=`` to opt into dark-mode analysis (the default
  is light only).
"""

from __future__ import annotations

from colorsense.config import Config, load_config, load_default_config
from colorsense.models import AnalysisResult
from colorsense.net.politeness import PolitenessPolicy, RobotsDisallowedError
from colorsense.pipeline import LIGHT_AND_DARK, analyze

__all__ = [
    "LIGHT_AND_DARK",
    "AnalysisResult",
    "Config",
    "PolitenessPolicy",
    "RobotsDisallowedError",
    "analyze",
    "load_config",
    "load_default_config",
]
