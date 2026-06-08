"""Playwright (async API) render session.

:class:`RenderSession` is an async context manager that launches a headless Chromium
browser, opens a page at a fixed :class:`~colorsense.models.Viewport` and color scheme,
navigates robustly to a URL (guarding ``networkidle`` so ``file://`` pages never hang),
neutralizes transitions/animations, step-scrolls to trigger lazy content, and detects
consent/overlay regions whose bounding rects can be masked out of the screenshot.

The Playwright :class:`~playwright.async_api.Page` is exposed as :attr:`RenderSession.page`
so the other harvest modules can run their own JS against the same live page. Built on the
**async** Playwright API so it runs natively on an asyncio event loop (e.g. inside a
FastAPI ``async def`` endpoint) and so sibling theme renders can overlap.
"""

from __future__ import annotations

import contextlib
from types import TracebackType
from typing import Literal, Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from colorsense.models import Rect, Theme, Viewport

# Map our Theme to Playwright's color_scheme literal.
_COLOR_SCHEME: dict[Theme, Literal["light", "dark"]] = {
    Theme.light: "light",
    Theme.dark: "dark",
}

# Inject CSS killing transitions/animations so computed colors are deterministic.
_DISABLE_MOTION_CSS: str = "* { transition: none !important; animation: none !important; }"

# Max step-scroll iterations to trigger lazy content (cap so we never loop forever).
_MAX_SCROLL_STEPS: int = 20

# Default navigation timeout (ms) for ``page.goto``. Made explicit (rather than relying on
# Playwright's implicit 30s default) so the value is documented and overridable per render.
# A ``goto`` that exceeds it raises a Playwright ``TimeoutError``, which ``harvest_page``
# wraps as the public ``RenderError``.
DEFAULT_NAV_TIMEOUT_MS: float = 30_000.0

# Timeout (ms) guarding wait_for_load_state("networkidle") on pages that never idle.
# Kept short on purpose: ``goto(wait_until="load")`` has already fired the load event (all
# synchronous resources fetched), so this only waits out async/lazy chatter (analytics,
# below-fold images). Measured against real sites (stripe/github/bootstrap), dropping this
# from 3s to 1s left the harvested palette, tokens, and hover hits unchanged while saving
# ~1.5s/render; the subsequent step-scroll still triggers genuinely lazy content.
_NETWORKIDLE_TIMEOUT_MS: float = 1000.0

# JS that step-scrolls the full document height and returns the iteration count.
_STEP_SCROLL_JS: str = """
(maxSteps) => {
    const step = window.innerHeight;
    let pos = 0;
    let iterations = 0;
    const limit = Math.max(document.body ? document.body.scrollHeight : 0,
                           document.documentElement.scrollHeight);
    while (pos < limit && iterations < maxSteps) {
        window.scrollTo(0, pos);
        pos += step;
        iterations += 1;
    }
    window.scrollTo(0, 0);
    return iterations;
}
"""

# JS that finds consent/overlay banners and returns their bounding rects.
_CONSENT_RECTS_JS: str = r"""
() => {
    const keywords = /cookie|consent|gdpr|onetrust|cookiebot|usercentrics|privacy|banner/i;
    const out = [];
    const seen = new Set();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const push = (el) => {
        if (seen.has(el)) return;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return;
        seen.add(el);
        out.push({x: r.x, y: r.y, w: r.width, h: r.height});
    };
    for (const el of document.querySelectorAll('*')) {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        const idClass = (el.id + ' ' + (el.className && el.className.toString
            ? el.className.toString() : '')).trim();
        const matchesKeyword = keywords.test(idClass);
        const pos = style.position;
        const z = parseInt(style.zIndex, 10);
        const r = el.getBoundingClientRect();
        const fullWidthish = r.width >= vw * 0.8;
        const fixedSticky = pos === 'fixed' || pos === 'sticky';
        const highZ = Number.isFinite(z) && z >= 1000;
        const coversBand = fullWidthish && r.height > 0 && r.height < vh;
        if (matchesKeyword && (fixedSticky || highZ || fullWidthish)) {
            push(el);
        } else if (fixedSticky && (highZ || coversBand) && fullWidthish) {
            push(el);
        }
    }
    return out;
}
"""


class RenderSession:
    """Context manager wrapping a headless Chromium page at a fixed theme/viewport.

    Usage::

        async with RenderSession(theme, viewport) as session:
            await session.goto(url)
            page = session.page  # run module JS against it
            consent = session.consent_rects
    """

    def __init__(self, theme: Theme, viewport: Viewport) -> None:
        self._theme = theme
        self._viewport = viewport
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._consent_rects: list[Rect] = []

    # -- context manager --------------------------------------------------

    async def __aenter__(self) -> Self:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            viewport={"width": self._viewport.width, "height": self._viewport.height},
            device_scale_factor=self._viewport.device_scale_factor,
            color_scheme=_COLOR_SCHEME[self._theme],
        )
        self._page = await self._context.new_page()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Always tear down, swallowing teardown errors so the original exception (if any)
        # propagates cleanly.
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    # -- accessors --------------------------------------------------------

    @property
    def page(self) -> Page:
        """The live Playwright page (only valid inside the ``with`` block)."""
        if self._page is None:
            raise RuntimeError("RenderSession.page accessed outside of an active session")
        return self._page

    @property
    def theme(self) -> Theme:
        """The theme this session renders under."""
        return self._theme

    @property
    def viewport(self) -> Viewport:
        """The viewport this session renders at."""
        return self._viewport

    @property
    def consent_rects(self) -> list[Rect]:
        """Bounding rects of detected consent/overlay banners (for masking)."""
        return list(self._consent_rects)

    # -- navigation -------------------------------------------------------

    async def goto(self, url: str, *, nav_timeout_ms: float = DEFAULT_NAV_TIMEOUT_MS) -> None:
        """Navigate to ``url`` and stabilize the page for harvesting.

        Performs ``goto(..., wait_until="load")``, a guarded ``networkidle`` wait, motion
        neutralization, step-scrolling to trigger lazy content, and consent-region
        detection. ``networkidle`` is guarded with a timeout/try-except so ``file://``
        pages that never report idle do not hang.

        Parameters
        ----------
        nav_timeout_ms:
            Per-navigation timeout in milliseconds, passed explicitly to ``page.goto``.
            Defaults to :data:`DEFAULT_NAV_TIMEOUT_MS`. Exceeding it raises a Playwright
            ``TimeoutError`` (wrapped as :class:`~colorsense.harvest.RenderError` upstream).
        """
        page = self.page
        await page.goto(url, wait_until="load", timeout=nav_timeout_ms)
        # file:// pages may never report networkidle; guard with a timeout.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_TIMEOUT_MS)

        await page.add_style_tag(content=_DISABLE_MOTION_CSS)

        # Step-scrolling is best-effort (triggers lazy content).
        with contextlib.suppress(Exception):
            await page.evaluate(_STEP_SCROLL_JS, _MAX_SCROLL_STEPS)

        self._consent_rects = await self._detect_consent_rects()

    async def _detect_consent_rects(self) -> list[Rect]:
        """Return bounding rects of consent/overlay banners without clicking them."""
        try:
            raw = await self.page.evaluate(_CONSENT_RECTS_JS)
        except Exception:  # detection is best-effort
            return []
        rects: list[Rect] = []
        if not isinstance(raw, list):
            return rects
        for item in raw:
            if not isinstance(item, dict):
                continue
            rects.append(
                Rect(
                    x=float(item["x"]),
                    y=float(item["y"]),
                    width=float(item["w"]),
                    height=float(item["h"]),
                )
            )
        return rects
