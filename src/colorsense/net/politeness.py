"""Network politeness: *mechanism, not policy*.

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

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from colorsense.config import Config
from colorsense.harvest import harvest_page
from colorsense.models import Harvest, Theme, Viewport

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; colorsense/0.1; +https://github.com/colorsense/colorsense)"
)
"""A real browser-engine base token plus an identifiable ``colorsense`` token and a URL."""

DEFAULT_ROBOTS_AGENT = "colorsense"
"""The product token matched against ``robots.txt`` ``User-agent:`` groups.

This is deliberately distinct from :data:`DEFAULT_USER_AGENT`. ``RobotFileParser`` matches a
robots ``User-agent`` group by checking whether the group name is a case-insensitive prefix
of the agent string passed to :meth:`RobotFileParser.can_fetch`. Passing the full
descriptive UA (which begins with ``"Mozilla/5.0 ..."``) would only ever match the wire
browser token, so a site-specific ``User-agent: colorsense`` group would be silently ignored.
Matching on the bare product token honors those agent-specific rules while the full
:data:`DEFAULT_USER_AGENT` is still what is sent on the wire.
"""

DEFAULT_MIN_INTERVAL = 1.0
"""Seconds enforced between consecutive fetches to the same host."""

DEFAULT_MAX_CACHE_ENTRIES = 256
"""Default upper bound on the render cache (largest objects; LRU-evicted past this)."""

# Schemes for which robots.txt and rate limiting apply. ``file://`` fixtures (used by the
# test suite) carry no host and no robots concept, so they bypass both gates.
_NETWORK_SCHEMES = frozenset({"http", "https"})

# All I/O seams are async: the harvester renders via async Playwright and the robots
# loader fetches over async httpx. The clock stays synchronous (a plain time source);
# only the sleeper is awaited so rate-limit waits yield the event loop.
Harvester = Callable[[str, Theme, Config, Viewport], Awaitable[Harvest]]
RobotsLoader = Callable[[str], Awaitable[str | None]]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


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


async def _default_robots_loader(robots_url: str) -> str | None:
    """Fetch ``robots.txt`` text over http(s); return ``None`` on any failure.

    A missing or unreachable ``robots.txt`` is treated by callers as "no rules", which
    permits fetching — the conventional interpretation.
    """
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            follow_redirects=True,
        ) as client:
            response = await client.get(robots_url)
            response.raise_for_status()
            return response.text
    except (httpx.HTTPError, ValueError):
        return None


def _cache_key(url: str, theme: Theme, viewport: Viewport) -> tuple[str, str, int, int, float]:
    """A render is identified by URL + theme + viewport geometry."""
    return (url, str(theme), viewport.width, viewport.height, viewport.device_scale_factor)


class PolitenessPolicy:
    """Gate, pace, and cache page renders on behalf of a consumer.

    Parameters
    ----------
    user_agent:
        Identifiable User-Agent sent on the wire when reading ``robots.txt`` (the renderer's
        own UA is set elsewhere). Defaults to :data:`DEFAULT_USER_AGENT`.
    robots_agent:
        The product token matched against ``robots.txt`` ``User-agent:`` groups by
        :meth:`can_fetch`. Kept separate from ``user_agent`` because the descriptive wire UA
        begins with a browser token, so matching on it would ignore agent-specific rules.
        Defaults to :data:`DEFAULT_ROBOTS_AGENT` (``"colorsense"``).
    respect_robots:
        When ``True`` (default), :meth:`can_fetch` consults ``robots.txt`` and
        :meth:`fetch` raises :class:`RobotsDisallowedError` on a disallow. Set ``False`` to
        bypass the check entirely — the consumer then owns authorization.
    min_interval:
        Minimum seconds between same-host fetches (per-host rate limiter).
    max_cache_entries:
        Upper bound on the render cache (``_cache``), which holds full :class:`Harvest`
        objects — the largest things this policy retains. When the cache would exceed this,
        the least-recently-used entry is evicted (the cache is an :class:`OrderedDict`; a
        hit moves its key to the most-recently-used end). Defaults to
        :data:`DEFAULT_MAX_CACHE_ENTRIES`. Pass ``0`` or ``None`` for an unbounded cache
        (the legacy grow-forever behavior — only sensible for short-lived runs).
    harvester:
        The render function, injectable for testing. Defaults to
        :func:`colorsense.harvest.harvest_page`.
    robots_loader / clock / sleeper:
        Injectable async seams for ``robots.txt`` retrieval and sleeping, plus a sync time
        source — swapped out by the test suite so no real network or wall-clock delay is
        incurred.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        robots_agent: str = DEFAULT_ROBOTS_AGENT,
        respect_robots: bool = True,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_cache_entries: int | None = DEFAULT_MAX_CACHE_ENTRIES,
        harvester: Harvester = harvest_page,
        robots_loader: RobotsLoader = _default_robots_loader,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.robots_agent = robots_agent
        self.respect_robots = respect_robots
        self.min_interval = min_interval
        # ``0``/``None`` mean unbounded; any positive value is the LRU ceiling.
        self.max_cache_entries = max_cache_entries
        self._harvester = harvester
        self._robots_loader = robots_loader
        self._clock = clock
        self._sleeper = sleeper
        # LRU-bounded render cache: holds full Harvest objects (the largest retained state),
        # so it is the one cache that can leak in a long-running server. Most-recently-used
        # keys live at the end; overflow evicts from the front (``popitem(last=False)``).
        self._cache: OrderedDict[tuple[str, str, int, int, float], Harvest] = OrderedDict()
        # ``_robots_cache`` and ``_last_fetch`` are intentionally unbounded: they hold one
        # small entry per host, so their size is bounded by the number of distinct hosts a
        # consumer fetches (tiny in practice), not by request volume.
        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self._last_fetch: dict[str, float] = {}
        # Serializes the rate-limiter's read-wait-stamp sequence so concurrent fetches
        # (e.g. light + dark renders gathered by analyze()) still honor min_interval
        # instead of all racing through on a stale timestamp.
        self._throttle_lock = asyncio.Lock()

    # -- robots --------------------------------------------------------------

    async def _robots_parser(self, robots_url: str) -> RobotFileParser | None:
        """Load and memoize a parser for ``robots_url``; ``None`` when no rules apply."""
        if robots_url in self._robots_cache:
            return self._robots_cache[robots_url]
        text = await self._robots_loader(robots_url)
        parser: RobotFileParser | None
        if text is None:
            parser = None
        else:
            parser = RobotFileParser()
            parser.parse(text.splitlines())
        self._robots_cache[robots_url] = parser
        return parser

    async def can_fetch(self, url: str) -> bool:
        """Whether ``url`` may be fetched under this policy.

        Non-network URLs (e.g. ``file://`` fixtures) and a disabled robots check always
        return ``True``. A missing/unreachable ``robots.txt`` permits fetching.
        """
        if not self.respect_robots:
            return True
        robots_url = _robots_url_for(url)
        if robots_url is None:
            return True
        parser = await self._robots_parser(robots_url)
        if parser is None:
            return True
        # Match on the bare product token, not the descriptive wire UA: RobotFileParser
        # matches a ``User-agent`` group by prefix, and the wire UA starts with "Mozilla/5.0",
        # which would mask any site-specific ``User-agent: colorsense`` rule.
        return parser.can_fetch(self.robots_agent, url)

    # -- rate limiting -------------------------------------------------------

    async def _throttle(self, url: str) -> None:
        """Wait until ``min_interval`` has elapsed since the last fetch to this host.

        The read-wait-stamp sequence is guarded by an :class:`asyncio.Lock` so concurrent
        callers serialize correctly; the (potentially long) render then runs outside the
        lock, so distinct fetches still overlap once spaced by ``min_interval``.
        """
        parts = urlsplit(url)
        if parts.scheme not in _NETWORK_SCHEMES or not parts.netloc:
            return
        host = parts.netloc
        async with self._throttle_lock:
            last = self._last_fetch.get(host)
            now = self._clock()
            if last is not None:
                wait = self.min_interval - (now - last)
                if wait > 0:
                    await self._sleeper(wait)
                    now = self._clock()
            self._last_fetch[host] = now

    # -- fetch ---------------------------------------------------------------

    async def fetch(self, url: str, theme: Theme, config: Config, viewport: Viewport) -> Harvest:
        """Return a :class:`Harvest` for ``url``/``theme``/``viewport``, politely.

        Cache hits return immediately (no robots check, no throttle, no render) and mark the
        entry most-recently-used. Otherwise the robots gate is enforced, the per-host rate
        limiter applied, and the harvester awaited; the result is cached (LRU-evicting the
        least-recently-used entry if the cache is bounded and now full) before return.
        """
        key = _cache_key(url, theme, viewport)
        cached = self._cache.get(key)
        if cached is not None:
            # A hit is fresh usage: move it to the most-recently-used end so it survives
            # eviction. Safe under the single-threaded event loop (no await in between).
            self._cache.move_to_end(key)
            return cached
        if not await self.can_fetch(url):
            raise RobotsDisallowedError(url)
        await self._throttle(url)
        harvest = await self._harvester(url, theme, config, viewport)
        self._cache[key] = harvest
        self._cache.move_to_end(key)
        # Evict least-recently-used entries once over the bound. ``0``/``None`` => unbounded.
        if self.max_cache_entries:
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)
        return harvest
