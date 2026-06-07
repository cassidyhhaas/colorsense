"""WP11 — network politeness: *mechanism, not policy*.

This module gives a consumer the tools to fetch pages considerately — a configurable
User-Agent, an opt-out ``robots.txt`` gate, a per-host rate limiter, and a render cache —
but it deliberately does **not** decide whether a given fetch is authorized. Authorization
is the caller's responsibility (see the README note on embedded vs server-side use). The
defaults are conservative: ``robots.txt`` is respected and same-host fetches are spaced by
one second.

``PolitenessPolicy`` is the single object the pipeline talks to. It is *not* a frozen
cross-WP contract (it never crosses the ``models.py`` boundary), so it lives here rather
than in the shared contracts surface.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from colorsense.config import Config
from colorsense.harvest import harvest_page
from colorsense.models import Harvest, Theme, Viewport

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; colorsense/0.1; +https://github.com/colorsense/colorsense)"
)
"""A real browser-engine base token plus an identifiable ``colorsense`` token and a URL."""

DEFAULT_MIN_INTERVAL = 1.0
"""Seconds enforced between consecutive fetches to the same host."""

# Schemes for which robots.txt and rate limiting apply. ``file://`` fixtures (used by the
# test suite) carry no host and no robots concept, so they bypass both gates.
_NETWORK_SCHEMES = frozenset({"http", "https"})

Harvester = Callable[[str, Theme, Config, Viewport], Harvest]
RobotsLoader = Callable[[str], str | None]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


class RobotsDisallowedError(RuntimeError):
    """Raised when ``robots.txt`` disallows a URL and the active policy respects it."""

    def __init__(self, url: str) -> None:
        super().__init__(f"robots.txt disallows fetching {url!r}")
        self.url = url


def _robots_url_for(url: str) -> str | None:
    """Return the ``robots.txt`` URL for ``url``'s host, or ``None`` for non-network URLs."""
    parts = urlsplit(url)
    if parts.scheme not in _NETWORK_SCHEMES or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def _default_robots_loader(robots_url: str) -> str | None:
    """Fetch ``robots.txt`` text over http(s); return ``None`` on any failure.

    A missing or unreachable ``robots.txt`` is treated by callers as "no rules", which
    permits fetching — the conventional interpretation.
    """
    request = urllib.request.Request(robots_url, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text: str = response.read().decode(charset, errors="replace")
            return text
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None


def _cache_key(url: str, theme: Theme, viewport: Viewport) -> tuple[str, str, int, int, float]:
    """A render is identified by URL + theme + viewport geometry."""
    return (url, str(theme), viewport.w, viewport.h, viewport.device_scale_factor)


class PolitenessPolicy:
    """Gate, pace, and cache page renders on behalf of a consumer.

    Parameters
    ----------
    user_agent:
        Identifiable User-Agent sent when reading ``robots.txt`` (the renderer's own UA is
        set elsewhere). Defaults to :data:`DEFAULT_USER_AGENT`.
    respect_robots:
        When ``True`` (default), :meth:`can_fetch` consults ``robots.txt`` and
        :meth:`fetch` raises :class:`RobotsDisallowedError` on a disallow. Set ``False`` to
        bypass the check entirely — the consumer then owns authorization.
    min_interval:
        Minimum seconds between same-host fetches (per-host rate limiter).
    harvester:
        The render function, injectable for testing. Defaults to
        :func:`colorsense.harvest.harvest_page`.
    robots_loader / clock / sleeper:
        Injectable seams for ``robots.txt`` retrieval, time source, and sleeping — swapped
        out by the test suite so no real network or wall-clock delay is incurred.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        respect_robots: bool = True,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        harvester: Harvester = harvest_page,
        robots_loader: RobotsLoader = _default_robots_loader,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.min_interval = min_interval
        self._harvester = harvester
        self._robots_loader = robots_loader
        self._clock = clock
        self._sleeper = sleeper
        self._cache: dict[tuple[str, str, int, int, float], Harvest] = {}
        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self._last_fetch: dict[str, float] = {}

    # -- robots --------------------------------------------------------------

    def _robots_parser(self, robots_url: str) -> RobotFileParser | None:
        """Load and memoize a parser for ``robots_url``; ``None`` when no rules apply."""
        if robots_url in self._robots_cache:
            return self._robots_cache[robots_url]
        text = self._robots_loader(robots_url)
        parser: RobotFileParser | None
        if text is None:
            parser = None
        else:
            parser = RobotFileParser()
            parser.parse(text.splitlines())
        self._robots_cache[robots_url] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        """Whether ``url`` may be fetched under this policy.

        Non-network URLs (e.g. ``file://`` fixtures) and a disabled robots check always
        return ``True``. A missing/unreachable ``robots.txt`` permits fetching.
        """
        if not self.respect_robots:
            return True
        robots_url = _robots_url_for(url)
        if robots_url is None:
            return True
        parser = self._robots_parser(robots_url)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    # -- rate limiting -------------------------------------------------------

    def _throttle(self, url: str) -> None:
        """Block until ``min_interval`` has elapsed since the last fetch to this host."""
        parts = urlsplit(url)
        if parts.scheme not in _NETWORK_SCHEMES or not parts.netloc:
            return
        host = parts.netloc
        last = self._last_fetch.get(host)
        now = self._clock()
        if last is not None:
            wait = self.min_interval - (now - last)
            if wait > 0:
                self._sleeper(wait)
                now = self._clock()
        self._last_fetch[host] = now

    # -- fetch ---------------------------------------------------------------

    def fetch(self, url: str, theme: Theme, config: Config, viewport: Viewport) -> Harvest:
        """Return a :class:`Harvest` for ``url``/``theme``/``viewport``, politely.

        Cache hits return immediately (no robots check, no throttle, no render). Otherwise
        the robots gate is enforced, the per-host rate limiter applied, and the harvester
        invoked; the result is cached before return.
        """
        key = _cache_key(url, theme, viewport)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not self.can_fetch(url):
            raise RobotsDisallowedError(url)
        self._throttle(url)
        harvest = self._harvester(url, theme, config, viewport)
        self._cache[key] = harvest
        return harvest
