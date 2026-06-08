"""Regression tests for two robustness fixes (network-free).

Fix 1 (M4): ``robots.txt`` ``User-agent: colorsense`` disallows must be honored under the
default wire UA (which begins with a browser token). These drive
:class:`PolitenessPolicy.can_fetch` with an injected ``robots_loader`` so no real network or
browser is touched.

Fix 2 (M3): a Playwright navigation/render failure must surface as the public
:class:`colorsense.harvest.RenderError`, not the version-private ``playwright._impl`` type.
This is exercised by monkeypatching the render seam so it raises a Playwright error, keeping
the test deterministic and browser-free.
"""

from __future__ import annotations

import pytest
from playwright.async_api import Error as PlaywrightError

import colorsense.harvest as harvest_mod
from colorsense.config import load_default_config
from colorsense.harvest import RenderError, harvest_page
from colorsense.models import Theme, Viewport
from colorsense.net.politeness import PolitenessPolicy

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


# --- Fix 1: agent-specific robots disallows -------------------------------------------


def _loader_for(text: str | None):
    async def _loader(_url: str) -> str | None:
        return text

    return _loader


async def test_agent_specific_disallow_blocks_under_default_ua() -> None:
    # A ``User-agent: colorsense`` group with ``Disallow: /`` must block, even though the
    # wire UA begins with "Mozilla/5.0" (the bug: prefix-matching the wire UA missed this).
    robots = "User-agent: colorsense\nDisallow: /\n"
    policy = PolitenessPolicy(robots_loader=_loader_for(robots))
    assert await policy.can_fetch("https://example.com/page") is False


async def test_wildcard_disallow_still_blocks() -> None:
    robots = "User-agent: *\nDisallow: /\n"
    policy = PolitenessPolicy(robots_loader=_loader_for(robots))
    assert await policy.can_fetch("https://example.com/page") is False


async def test_agent_specific_allow_permits() -> None:
    # Disallow everyone, but explicitly allow colorsense: agent-specific group must win.
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: colorsense\nDisallow:\n"
    policy = PolitenessPolicy(robots_loader=_loader_for(robots))
    assert await policy.can_fetch("https://example.com/page") is True


async def test_file_url_bypasses_robots() -> None:
    # file:// has no host/robots concept: always fetchable, even under a disallow-all loader.
    robots = "User-agent: colorsense\nDisallow: /\n"
    policy = PolitenessPolicy(robots_loader=_loader_for(robots))
    assert await policy.can_fetch("file:///tmp/x.html") is True


# --- Fix 2: RenderError wraps Playwright failures -------------------------------------


async def test_render_error_raised_on_navigation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "http://nonexistent.invalid/"
    original = PlaywrightError("net::ERR_NAME_NOT_RESOLVED at " + url)

    class _FailingSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        async def __aenter__(self) -> _FailingSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def goto(self, _url: str, **_kwargs: object) -> None:
            raise original

    monkeypatch.setattr(harvest_mod, "RenderSession", _FailingSession)

    config = load_default_config()
    with pytest.raises(RenderError) as excinfo:
        await harvest_page(url, Theme.light, config, VIEWPORT)

    err = excinfo.value
    assert isinstance(err, RenderError)  # catchable as the typed public class
    assert err.url == url
    assert err.__cause__ is original  # original Playwright error chained via ``from``
