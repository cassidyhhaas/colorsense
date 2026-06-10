"""Network politeness: *mechanism, not policy*.

This module gives a consumer the tools to fetch pages considerately — a configurable
User-Agent, an opt-out ``robots.txt`` gate, a per-host rate limiter, and a render cache —
but it deliberately does **not** decide whether a given fetch is authorized. Authorization
is the caller's responsibility (see ``docs/usage.md`` on embedded vs server-side use). The
defaults are conservative: ``robots.txt`` is respected (including its ``Crawl-delay``,
capped), same-host fetches are spaced by one second, and only ``http(s)`` URLs are fetched
(``file://`` is an explicit opt-in).

The policy is the only place networking policy is enforced: calling
:func:`colorsense.harvest.harvest_page` or :class:`~colorsense.harvest.RenderSession`
directly bypasses every gate here (scheme validation, robots, throttle, cache).

``PolitenessPolicy`` is the single object the pipeline talks to. It is *not* a frozen
cross-WP contract (it never crosses the ``models.py`` boundary), so it lives here rather
than in the shared contracts surface.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from colorsense.config import Config
from colorsense.harvest import SharedBrowser, harvest_page
from colorsense.models import Harvest, Theme, Viewport

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; colorsense/0.1; +https://github.com/cassidyhhaas/colorsense)"
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

DEFAULT_MAX_CRAWL_DELAY = 30.0
"""Cap (seconds) applied to a ``robots.txt`` ``Crawl-delay`` before it joins the limiter.

A hostile or typo'd ``robots.txt`` (``Crawl-delay: 86400``) must not be able to stall a
pipeline arbitrarily, so the learned delay is clamped to this before being combined with
``min_interval``. Consumers who genuinely want to honor longer delays can raise
``max_crawl_delay`` on their policy.
"""

# Schemes for which robots.txt and rate limiting apply. ``file://`` URLs (opt-in via
# ``allow_file_urls=True``; used by the test suite) carry no host and no robots concept,
# so they bypass both gates.
_NETWORK_SCHEMES = frozenset({"http", "https"})

# All I/O seams are async: the harvester renders via async Playwright and the robots
# loader fetches over async httpx. The clock stays synchronous (a plain time source);
# only the sleeper is awaited so rate-limit waits yield the event loop. Both network
# seams carry the policy's configured ``user_agent`` so the identity sent on the wire
# (robots GET *and* page render) is the one the consumer configured.


class Harvester(Protocol):
    """Render seam: ``(url, theme, config, viewport, *, user_agent=, request_filter=, browser=)``.

    A :class:`~typing.Protocol` rather than a plain ``Callable`` because the policy passes
    its configured wire UA as the keyword-only ``user_agent``, its egress
    ``request_filter`` predicate, and the caller-supplied shared-``browser`` handle (all
    with defaults, so direct callers that ignore them remain expressible). The ``browser``
    handle is opaque to the policy — it is threaded through from :meth:`PolitenessPolicy.fetch`
    untouched; the harvester (and ultimately the render session) owns what it means.
    Defaults to :func:`colorsense.harvest.harvest_page`.
    """

    def __call__(
        self,
        url: str,
        theme: Theme,
        config: Config,
        viewport: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: Callable[[str], bool] | None = None,
        browser: SharedBrowser | None = None,
    ) -> Awaitable[Harvest]: ...


RobotsLoader = Callable[[str, str, Callable[[str], bool] | None], Awaitable[str | None]]
"""Robots seam: ``(robots_url, user_agent, request_filter) -> text | None``.

The UA is sent on the wire. ``request_filter`` is the policy's egress predicate (or
``None``): a loader must apply it to the robots URL itself *and* to every redirect hop it
follows, returning ``None`` (no rules) instead of fetching a rejected destination — the
robots GET is server-side ``httpx``, not the browser, so it would otherwise bypass the
browser-route filter entirely (SSRF via a robots redirect).
"""
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class RobotsDisallowedError(RuntimeError):
    """Raised when ``robots.txt`` disallows a URL and the active policy respects it."""

    def __init__(self, url: str) -> None:
        super().__init__(f"robots.txt disallows fetching {url!r}")
        self.url = url


class UnsupportedSchemeError(ValueError):
    """Raised by :meth:`PolitenessPolicy.fetch` for URLs whose scheme it refuses to render.

    Only ``http``/``https`` URLs are fetchable by default. ``file://`` (a local-file-read
    primitive) is an explicit opt-in via ``PolitenessPolicy(allow_file_urls=True)``; every
    other scheme (``ftp``, ``data``, ``javascript``, scheme-less, ...) is always rejected.
    The offending URL is available as :attr:`url`.
    """

    def __init__(self, url: str, *, hint: str | None = None) -> None:
        message = f"unsupported URL scheme for fetching {url!r}"
        if hint:
            message = f"{message} ({hint})"
        super().__init__(message)
        self.url = url


def _robots_url_for(url: str) -> str | None:
    """Return the ``robots.txt`` URL for ``url``'s host, or ``None`` for non-network URLs."""
    parts = urlsplit(url)
    if parts.scheme not in _NETWORK_SCHEMES or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


_MAX_ROBOTS_REDIRECTS = 20
"""Redirect-hop cap for the robots GET (httpx's own default ``max_redirects``)."""


async def _default_robots_loader(
    robots_url: str,
    user_agent: str,
    request_filter: Callable[[str], bool] | None = None,
    *,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """Fetch ``robots.txt`` text over http(s); return ``None`` on any failure.

    ``user_agent`` is the policy's configured wire UA, sent as the ``User-Agent`` header so
    the robots GET is attributable to the same identity as the page render. A missing or
    unreachable ``robots.txt`` is treated by callers as "no rules", which permits fetching —
    the conventional interpretation.

    ``request_filter`` is the policy's egress predicate. Redirects are followed *manually*
    (``follow_redirects=False`` plus a loop capped at :data:`_MAX_ROBOTS_REDIRECTS` hops)
    so the filter can vet **each** URL before it is requested — the initial robots URL and
    every redirect ``Location`` (resolved against the current URL). A rejected — or
    raising, which fails closed — verdict aborts the fetch and returns ``None``: the robots
    rules fail open as usual, while the navigation itself stays gated by the browser-side
    filter. Without a filter (``None``), redirects are simply followed up to the cap.

    ``_transport`` is a private test seam: an injected ``httpx`` transport (e.g.
    ``httpx.MockTransport``) standing in for the network.
    """

    def _permitted(candidate: str) -> bool:
        if request_filter is None:
            return True
        try:
            return bool(request_filter(candidate))
        except Exception:
            # A buggy/raising predicate must block, not permit (fail closed).
            return False

    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": user_agent},
            follow_redirects=False,
            transport=_transport,
        ) as client:
            url = robots_url
            for _ in range(_MAX_ROBOTS_REDIRECTS + 1):
                if not _permitted(url):
                    return None
                response = await client.get(url)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                return response.text
            # Redirect cap exhausted without reaching a terminal response.
            return None
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
        Identifiable User-Agent sent on the wire for *both* the ``robots.txt`` GET and the
        page render itself (forwarded to the harvester, which sets it on the browser
        context). Defaults to :data:`DEFAULT_USER_AGENT`.
    robots_agent:
        The product token matched against ``robots.txt`` ``User-agent:`` groups by
        :meth:`can_fetch`. Kept separate from ``user_agent`` because the descriptive wire UA
        begins with a browser token, so matching on it would ignore agent-specific rules.
        Defaults to :data:`DEFAULT_ROBOTS_AGENT` (``"colorsense"``).
    respect_robots:
        When ``True`` (default), :meth:`can_fetch` consults ``robots.txt`` and
        :meth:`fetch` raises :class:`RobotsDisallowedError` on a disallow. Set ``False`` to
        bypass the check entirely — the consumer then owns authorization. Disabling robots
        also disables ``Crawl-delay`` honoring (no ``robots.txt`` is ever fetched).
    allow_file_urls:
        Whether :meth:`fetch` may render ``file://`` URLs. ``False`` by default —
        ``file://`` reads arbitrary local files, so it must be an explicit opt-in (the test
        suite opts in to render its local fixtures). When allowed, ``file://`` still
        bypasses the robots gate and the rate limiter: it has no host and no robots
        concept. Schemes other than ``http``/``https``/``file`` are always rejected;
        rejections raise :class:`UnsupportedSchemeError`.
    request_filter:
        Optional synchronous predicate over **every request URL the browser makes** while
        rendering — the navigation itself *and* all sub-resources (scripts, images, XHR/
        ``fetch`` issued by the page's own JS) — **and** over the policy's own
        ``robots.txt`` GET (the initial robots URL and each redirect hop it follows; a
        rejected hop aborts the fetch, which fails open as "no rules" while the navigation
        stays gated by this same filter browser-side). Returning ``False`` aborts that
        request. This is the in-library mechanism against sub-resource SSRF: validating the
        navigation URL alone cannot stop the rendered page from requesting internal
        endpoints (e.g. ``169.254.169.254``). A predicate that *raises* fails closed (the
        request is aborted). ``None`` (default) installs no interception at all — zero
        overhead.
    max_concurrent_renders:
        Optional cap on simultaneous *renders* through this policy — the SECURITY.md §2
        concurrency bound, shipped as a knob. ``None`` (default) is unbounded, the previous
        behavior. When set, an :class:`asyncio.Semaphore` bounds concurrent harvester
        calls. Composition with the other gates: **cache hits never take a slot** (they
        return before the limiter), **single-flight followers never take a slot** (they
        await the leader's future), and the throttle/robots wait happens *outside* the
        slot — the semaphore wraps strictly the render itself, so a leader parked in a
        long ``Crawl-delay`` sleep does not starve renders to other hosts. The semaphore is
        created lazily inside the running event loop (and re-created if the policy is later
        used from a different loop), so one policy can serve sequential ``asyncio.run``
        calls and two policies never share a limiter. Must be ``>= 1`` when set.
    min_interval:
        Minimum seconds between same-host fetches (per-host rate limiter). When the host's
        ``robots.txt`` declares a ``Crawl-delay`` for this policy's ``robots_agent``, the
        effective per-host interval is ``max(min_interval, crawl_delay)`` with the crawl
        delay capped at ``max_crawl_delay``. The delay is learned from the ``robots.txt``
        fetch itself, which is the host's *first* throttled request — so it applies from
        the second fetch to that host onward.
    max_crawl_delay:
        Upper bound (seconds) on an honored ``robots.txt`` ``Crawl-delay``. Defaults to
        :data:`DEFAULT_MAX_CRAWL_DELAY` (30.0) so a hostile or typo'd ``robots.txt``
        cannot stall a pipeline arbitrarily; raise it to honor longer delays.
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
        allow_file_urls: bool = False,
        request_filter: Callable[[str], bool] | None = None,
        max_concurrent_renders: int | None = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_crawl_delay: float = DEFAULT_MAX_CRAWL_DELAY,
        max_cache_entries: int | None = DEFAULT_MAX_CACHE_ENTRIES,
        harvester: Harvester = harvest_page,
        robots_loader: RobotsLoader = _default_robots_loader,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.robots_agent = robots_agent
        self.respect_robots = respect_robots
        self.allow_file_urls = allow_file_urls
        self.request_filter = request_filter
        if max_concurrent_renders is not None and max_concurrent_renders < 1:
            raise ValueError("max_concurrent_renders must be >= 1 (or None for unbounded)")
        self.max_concurrent_renders = max_concurrent_renders
        # Lazily created inside the running loop (see _render_slots): asyncio primitives
        # bind to the loop they are first awaited on, so creating it here (often outside
        # any loop) or pinning it to one loop would break a policy reused across
        # sequential ``asyncio.run`` calls.
        self._render_semaphore: asyncio.Semaphore | None = None
        self._render_semaphore_loop: asyncio.AbstractEventLoop | None = None
        self.min_interval = min_interval
        self.max_crawl_delay = max_crawl_delay
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
        # In-flight render coalescing (single-flight): concurrent fetches for the *same*
        # cache key share one render instead of each launching a redundant headless render.
        # The leader registers a Future here before its first ``await``; followers await it.
        self._inflight: dict[tuple[str, str, int, int, float], asyncio.Future[Harvest]] = {}
        # ``_robots_cache`` and ``_last_fetch`` are intentionally unbounded: they hold one
        # small entry per host, so their size is bounded by the number of distinct hosts a
        # consumer fetches (tiny in practice), not by request volume.
        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self._last_fetch: dict[str, float] = {}
        # Per-host ``Crawl-delay`` learned from robots.txt, keyed by netloc — the same key
        # space as ``_last_fetch`` so the limiter can join the two. Populated as a side
        # effect of loading a robots parser; never populated when ``respect_robots=False``
        # (no robots fetch happens, so no crawl delay is ever honored).
        self._crawl_delay: dict[str, float] = {}
        # Serializes only the rate-limiter's read-and-stamp step (not the sleep), so
        # concurrent fetches (e.g. light + dark renders gathered by analyze()) reserve
        # distinct same-host slots and honor min_interval instead of racing through on a
        # stale timestamp. The sleep runs outside the lock, so waits for *different* hosts
        # never serialize through this one mutex.
        self._throttle_lock = asyncio.Lock()

    # -- scheme gate ---------------------------------------------------------

    def _validate_scheme(self, url: str) -> None:
        """Raise :class:`UnsupportedSchemeError` unless ``url``'s scheme is fetchable.

        ``http``/``https`` are always allowed (the robots/throttle gates apply downstream);
        ``file`` only when this policy opted in via ``allow_file_urls=True``; everything
        else (``ftp``, ``data``, ``javascript``, scheme-less, ...) is always rejected.
        """
        scheme = urlsplit(url).scheme.lower()
        if scheme in _NETWORK_SCHEMES:
            return
        if scheme == "file":
            if self.allow_file_urls:
                return
            raise UnsupportedSchemeError(
                url,
                hint=(
                    "file:// URLs read local files and are disabled by default; "
                    "opt in with PolitenessPolicy(allow_file_urls=True)"
                ),
            )
        raise UnsupportedSchemeError(url)

    # -- robots --------------------------------------------------------------

    async def _robots_parser(self, robots_url: str) -> RobotFileParser | None:
        """Load and memoize a parser for ``robots_url``; ``None`` when no rules apply."""
        if robots_url in self._robots_cache:
            return self._robots_cache[robots_url]
        # The configured wire UA identifies the robots GET, same as the page render; the
        # egress filter rides along so the loader gates the robots URL and every redirect
        # hop — without it, a robots redirect would be a server-side SSRF bypassing the
        # browser-route filter.
        text = await self._robots_loader(robots_url, self.user_agent, self.request_filter)
        parser: RobotFileParser | None
        if text is None:
            parser = None
        else:
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            # Record the host's ``Crawl-delay`` (for our robots agent) so the rate limiter
            # can honor it. The netloc key matches ``_last_fetch``. Because the robots GET
            # is itself the host's first throttled request, the delay learned here only
            # paces the *second* fetch to the host onward — the first fetch has already
            # passed the limiter by the time this parser exists.
            delay = parser.crawl_delay(self.robots_agent)
            if delay is not None:
                self._crawl_delay[urlsplit(robots_url).netloc] = float(delay)
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

    def _host_interval(self, host: str) -> float:
        """Effective pacing interval for ``host``: ``max(min_interval, capped Crawl-delay)``.

        The crawl delay (when the host's ``robots.txt`` declared one) is clamped to
        ``max_crawl_delay`` first, so a hostile/typo'd directive cannot stall the pipeline.
        With ``respect_robots=False`` no robots is ever fetched, ``_crawl_delay`` stays
        empty, and this reduces to ``min_interval``.
        """
        crawl_delay = self._crawl_delay.get(host)
        if crawl_delay is None:
            return self.min_interval
        return max(self.min_interval, min(crawl_delay, self.max_crawl_delay))

    async def _throttle(self, url: str) -> None:
        """Wait until the host's effective interval has elapsed since its last fetch.

        The interval is :meth:`_host_interval` — ``min_interval`` raised to the host's
        (capped) ``robots.txt`` ``Crawl-delay`` once one has been learned.

        The read-and-stamp step is guarded by an :class:`asyncio.Lock` so concurrent callers
        serialize correctly, but the actual sleep happens *outside* the lock: we reserve this
        caller's slot by stamping the projected next-fetch time (``last + interval``)
        before releasing, so two same-host callers arriving together chain (each waits
        the interval after the previous) instead of both computing a zero wait — yet
        waits for *different* hosts no longer serialize through one global mutex.
        """
        parts = urlsplit(url)
        if parts.scheme not in _NETWORK_SCHEMES or not parts.netloc:
            return
        host = parts.netloc
        async with self._throttle_lock:
            last = self._last_fetch.get(host)
            now = self._clock()
            # Projected fetch time: ``now`` if the interval has already elapsed, else the
            # next free slot after the previous (reserved) fetch. Stamping it under the lock
            # is what makes concurrent same-host callers chain rather than collide.
            interval = self._host_interval(host)
            projected = now if last is None else max(now, last + interval)
            self._last_fetch[host] = projected
        wait = projected - now
        if wait > 0:
            await self._sleeper(wait)

    # -- render concurrency --------------------------------------------------

    def _render_slots(self) -> asyncio.Semaphore | None:
        """The render-bounding semaphore, or ``None`` when unbounded.

        Created lazily inside the running loop, and re-created if the policy is later used
        from a *different* loop (e.g. sequential ``asyncio.run`` calls sharing one policy):
        an asyncio Semaphore binds to the loop it is first awaited on, so reusing one
        across loops would raise. Re-creation is safe — by the time a new loop runs, no
        task from the old loop can still hold a slot.
        """
        if self.max_concurrent_renders is None:
            return None
        loop = asyncio.get_running_loop()
        if self._render_semaphore is None or self._render_semaphore_loop is not loop:
            self._render_semaphore = asyncio.Semaphore(self.max_concurrent_renders)
            self._render_semaphore_loop = loop
        return self._render_semaphore

    # -- fetch ---------------------------------------------------------------

    async def fetch(
        self,
        url: str,
        theme: Theme,
        config: Config,
        viewport: Viewport,
        *,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        """Return a :class:`Harvest` for ``url``/``theme``/``viewport``, politely.

        ``browser`` is an optional shared-browser handle forwarded to the harvester
        verbatim — the policy itself knows nothing about browser lifecycle (neither
        launching nor closing). :func:`colorsense.analyze` passes one handle to all of a
        call's theme fetches so they share a single Chromium launch; the handle is lazy,
        so a fetch served from the cache (or coalesced onto an in-flight leader) never
        triggers a launch. The cache key is unchanged (URL + theme + viewport): sharing a
        browser does not change *what* is rendered, only where the context lives.

        The URL scheme is validated first — before even the cache lookup, so a previously
        cached ``file://`` harvest can never be served by a policy that forbids file URLs
        (raises :class:`UnsupportedSchemeError`; see ``allow_file_urls``).

        Cache hits return immediately (no robots check, no throttle, no render) and mark the
        entry most-recently-used. Otherwise the per-host rate limiter is applied, the robots
        gate is enforced (its ``robots.txt`` GET is the first throttled request to the host),
        and the harvester awaited; the result is cached (LRU-evicting the least-recently-used
        entry if the cache is bounded and now full) before return.

        Concurrent misses for the *same* key are coalesced (single-flight): the first caller
        becomes the leader and runs exactly one throttle → robots gate → render; any caller
        that arrives while that render is in flight becomes a follower, awaiting the leader's
        result instead of launching a redundant headless render. All followers receive the
        leader's :class:`Harvest` — or, if the leader's gate/render fails, the *same*
        exception (the failure is not cached, and the next fetch re-renders). Distinct keys
        never share, so unrelated renders still run in parallel.

        Cancellation does **not** fan out like a failure: if the *leader's* task is
        cancelled (e.g. its caller's ``analyze(max_total_seconds=...)`` deadline expires,
        or its HTTP client disconnects in a server), followers never inherit the
        ``CancelledError``. They re-elect instead — exactly one follower becomes the new
        leader (re-running throttle → robots gate → render) and the rest follow *it*; if
        the render somehow completed first, the re-checking loop serves it from the cache.
        A follower whose *own* task is cancelled still raises ``CancelledError`` normally,
        and the cancelled leader still propagates its own ``CancelledError`` to its caller.
        """
        # Gate the scheme BEFORE the cache lookup: order matters for clarity (each policy
        # owns its cache, but the gate must visibly come first) and ensures a cached
        # ``file://`` harvest is never served once file URLs are forbidden.
        self._validate_scheme(url)
        key = _cache_key(url, theme, viewport)
        loop = asyncio.get_running_loop()
        # Coalesce concurrent misses (single-flight). Each iteration's check sequence
        # (cache, then ``_inflight``) contains no ``await``, so it is race-free under the
        # single-threaded event loop: either we find a leader's Future (follower path) or
        # we fall through and install our own atomically (leader path). The loop exists for
        # cancellation recovery: a follower whose leader was cancelled comes back around to
        # re-check the cache and either follow a fresh leader or become one itself.
        while True:
            cached = self._cache.get(key)
            if cached is not None:
                # A hit is fresh usage: move it to the most-recently-used end so it survives
                # eviction. Safe under the single-threaded event loop (no await in between).
                # On a re-election iteration this also catches a render that completed in
                # the meantime, so it is never redone.
                self._cache.move_to_end(key)
                return cached
            existing = self._inflight.get(key)
            if existing is None:
                break  # no leader in flight — fall through and become the leader
            # Follower: share the leader's single render; do not re-run robots/throttle/
            # render. ``asyncio.shield`` keeps the follower's OWN cancellation from
            # cancelling the shared future (which the leader and other followers still
            # use) and lets the two cancellation sources be told apart below.
            try:
                return await asyncio.shield(existing)
            except asyncio.CancelledError:
                if not existing.cancelled():
                    # The shared future is intact, so this CancelledError is the
                    # follower's own task being cancelled — propagate it normally.
                    raise
                # The LEADER was cancelled (its fan-out cancels the shared future). That
                # cancellation belongs to the leader's caller (deadline expiry, client
                # disconnect), not to this follower — except in the race where the
                # follower's own cancellation lands in the same tick the future is
                # cancelled: then the follower's task has a pending ``Task.cancel()``
                # (``cancelling() > 0``) that must be honored, not swallowed by
                # re-electing.
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    raise
                # Leader cancelled, follower not: re-elect. Loop back, re-check the cache,
                # then either follow a fresh leader that registered first or become the
                # new leader (re-running throttle → robots → render).
                continue
        future: asyncio.Future[Harvest] = loop.create_future()
        self._inflight[key] = future
        try:
            # Throttle BEFORE the robots gate: the robots.txt GET inside ``can_fetch`` is
            # itself the first network request to this host, so it must respect the per-host
            # limiter too (otherwise the very first request to a host is un-throttled). One
            # reservation covers both the robots GET and the page nav that follow — they are
            # a single logical visit, so we deliberately do not pace them a full interval
            # apart. ``can_fetch`` fails OPEN (timeout/404/error => allow), so a throttled
            # robots fetch never turns a transient error into a block.
            await self._throttle(url)
            if not await self.can_fetch(url):
                raise RobotsDisallowedError(url)
            # The render slot (when ``max_concurrent_renders`` is set) wraps STRICTLY the
            # harvester await — deliberately not the throttle/robots sequence above. The
            # alternative (acquire around throttle+robots+render) is simpler but means a
            # leader sleeping out a long per-host ``Crawl-delay`` holds a slot the whole
            # time, starving renders to unrelated hosts; with this placement a slot is only
            # ever held while a browser is genuinely rendering. Cache hits and single-flight
            # followers returned earlier and never reach this point, so neither takes a slot.
            slots = self._render_slots()
            if slots is not None:
                await slots.acquire()
            try:
                # The configured wire UA rides along so the page render is attributable
                # too, and the egress request filter (if any) gates every request the
                # browser makes.
                harvest = await self._harvester(
                    url,
                    theme,
                    config,
                    viewport,
                    user_agent=self.user_agent,
                    request_filter=self.request_filter,
                    browser=browser,
                )
            finally:
                if slots is not None:
                    slots.release()
        except BaseException as err:  # re-raised after fanning out to followers
            # Fan the failure out to every waiting follower, then re-raise to the leader.
            # The failure is never cached. Cancellation is special: the leader's
            # ``CancelledError`` belongs to the leader's caller, so it is NOT set as the
            # followers' exception — the future is *cancelled* instead, which the shielded
            # followers detect (``existing.cancelled()``) and recover from by re-electing a
            # new leader. Both calls are skipped if the future is already resolved, to
            # avoid InvalidStateError.
            if not future.done():
                if isinstance(err, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(err)
            raise
        else:
            self._cache[key] = harvest
            self._cache.move_to_end(key)
            # Evict least-recently-used entries once over the bound. ``0``/``None`` => unbounded.
            if self.max_cache_entries:
                while len(self._cache) > self.max_cache_entries:
                    self._cache.popitem(last=False)
            if not future.done():
                future.set_result(harvest)
            return harvest
        finally:
            # Always release the slot so a later fetch can re-render (on failure) or the
            # entry is served from cache (on success) — even under cancellation.
            self._inflight.pop(key, None)
            # If the Future carries an exception that no follower happened to await, retrieve
            # it (the ``.exception()`` call marks it retrieved) so asyncio does not log a
            # spurious "exception was never retrieved" warning. The leader re-raises the same
            # error to its own caller regardless.
            if future.done() and not future.cancelled():
                future.exception()
