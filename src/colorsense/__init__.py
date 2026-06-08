"""colorsense — analyze a website's color palette from its rendered pages.

Public API
----------
* :func:`analyze` — the one-call **async** pipeline: ``from colorsense import analyze``;
  ``result = await analyze(url)``.
* :class:`AnalysisResult` — the typed result it returns.
* :class:`Config` / :func:`load_config` — load and inspect the palette config.
* :class:`PolitenessPolicy` — opt-in fetch policy (robots, rate limit, cache); the consumer
  owns authorization.
"""

from __future__ import annotations

from colorsense.config import Config, load_config
from colorsense.models import AnalysisResult
from colorsense.net.politeness import PolitenessPolicy, RobotsDisallowedError
from colorsense.pipeline import analyze

__all__ = [
    "AnalysisResult",
    "Config",
    "PolitenessPolicy",
    "RobotsDisallowedError",
    "analyze",
    "load_config",
]
