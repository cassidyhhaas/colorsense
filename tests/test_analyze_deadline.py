"""``analyze(max_total_seconds=...)`` deadline tests — no browser, no network.

A slow injected harvester stands in for a stalling render; the deadline must surface as
the dedicated ``AnalysisTimeoutError`` (a ``TimeoutError`` subclass), validation must
reject non-positive budgets, an unset/generous budget must not change behavior, and the
shared-browser teardown must run on the timeout path (asserted through a fake
``SharedBrowser`` monkeypatched onto the pipeline module).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import TracebackType
from typing import ClassVar

import pytest

from colorsense import AnalysisTimeoutError, analyze
from colorsense.config import Config, load_default_config
from colorsense.harvest import SharedBrowser
from colorsense.models import Harvest, Theme, Viewport
from colorsense.net.politeness import PolitenessPolicy
from colorsense.pipeline import DEFAULT_VIEWPORT

URL = "https://example.test/page"


@pytest.fixture(scope="module")
def config() -> Config:
    return load_default_config()


async def _no_robots(
    _url: str, _user_agent: str, _request_filter: Callable[[str], bool] | None = None
) -> str | None:
    return None


class _SlowHarvester:
    """Harvester that hangs until cancelled, recording whether cancellation reached it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def __call__(
        self,
        url: str,
        theme: Theme,
        config: Config,
        viewport: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: Callable[[str], bool] | None = None,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        self.started.set()
        try:
            await asyncio.Event().wait()  # never set: only cancellation ends this render
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class _ImmediateHarvester:
    async def __call__(
        self,
        url: str,
        theme: Theme,
        config: Config,
        viewport: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: Callable[[str], bool] | None = None,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        return Harvest(url=url, theme=theme, viewport=viewport, screenshot_bins=[])


def _slow_policy() -> tuple[PolitenessPolicy, _SlowHarvester]:
    harvester = _SlowHarvester()
    return PolitenessPolicy(harvester=harvester, robots_loader=_no_robots), harvester


async def test_deadline_expiry_raises_analysis_timeout_error() -> None:
    policy, harvester = _slow_policy()
    with pytest.raises(AnalysisTimeoutError) as excinfo:
        await analyze(URL, politeness=policy, max_total_seconds=0.05)
    assert harvester.started.is_set()
    assert harvester.cancelled is True  # the in-flight render was cancelled, not abandoned
    assert excinfo.value.url == URL
    assert excinfo.value.max_total_seconds == 0.05
    assert URL in str(excinfo.value)
    assert "0.05" in str(excinfo.value)


async def test_error_subclasses_builtin_timeout_error() -> None:
    policy, _ = _slow_policy()
    # A consumer's generic ``except TimeoutError`` must still catch the dedicated error.
    with pytest.raises(TimeoutError):
        await analyze(URL, politeness=policy, max_total_seconds=0.05)
    assert issubclass(AnalysisTimeoutError, TimeoutError)


class _TimeoutRaisingHarvester:
    """Harvester that fails with its *own* ``TimeoutError`` (e.g. an upstream nav timeout)."""

    async def __call__(
        self,
        url: str,
        theme: Theme,
        config: Config,
        viewport: Viewport,
        *,
        user_agent: str | None = None,
        request_filter: Callable[[str], bool] | None = None,
        browser: SharedBrowser | None = None,
    ) -> Harvest:
        raise TimeoutError("unrelated upstream timeout")


async def test_inner_timeout_error_propagates_untranslated() -> None:
    # An unrelated TimeoutError raised inside the pipeline under an unexpired budget must
    # NOT be rebranded as AnalysisTimeoutError — only OUR deadline expiring is.
    policy = PolitenessPolicy(harvester=_TimeoutRaisingHarvester(), robots_loader=_no_robots)
    with pytest.raises(TimeoutError) as excinfo:
        await analyze(URL, politeness=policy, max_total_seconds=30.0)
    assert not isinstance(excinfo.value, AnalysisTimeoutError)
    assert str(excinfo.value) == "unrelated upstream timeout"


@pytest.mark.parametrize("budget", [0.0, -1.0])
async def test_non_positive_budget_rejected(budget: float) -> None:
    policy, harvester = _slow_policy()
    with pytest.raises(ValueError, match="max_total_seconds"):
        await analyze(URL, politeness=policy, max_total_seconds=budget)
    assert not harvester.started.is_set()  # rejected before any render


async def test_generous_budget_does_not_change_behavior() -> None:
    policy = PolitenessPolicy(harvester=_ImmediateHarvester(), robots_loader=_no_robots)
    result = await analyze(URL, politeness=policy, max_total_seconds=30.0)
    assert result.url == URL
    assert result.viewport == DEFAULT_VIEWPORT


async def test_no_budget_means_no_deadline() -> None:
    # The default (None) imposes no asyncio.timeout at all: a successful run is identical
    # to the pre-feature behavior.
    policy = PolitenessPolicy(harvester=_ImmediateHarvester(), robots_loader=_no_robots)
    result = await analyze(URL, politeness=policy)  # max_total_seconds defaults to None
    assert result.url == URL


class _FakeSharedBrowser:
    """Stands in for ``pipeline.SharedBrowser``; records enter/exit on the timeout path."""

    instances: ClassVar[list[_FakeSharedBrowser]] = []

    def __init__(self, *, browser_args: tuple[str, ...] = ()) -> None:
        self.browser_args = browser_args
        self.entered = False
        self.exited = False
        _FakeSharedBrowser.instances.append(self)

    async def __aenter__(self) -> _FakeSharedBrowser:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.exited = True


async def test_shared_browser_closed_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # The deadline cancels mid-render; the ``async with SharedBrowser()`` in the pipeline
    # must still unwind and close the browser (here: the fake's __aexit__ runs).
    _FakeSharedBrowser.instances.clear()
    monkeypatch.setattr("colorsense.pipeline.SharedBrowser", _FakeSharedBrowser)
    policy, harvester = _slow_policy()

    with pytest.raises(AnalysisTimeoutError):
        await analyze(URL, politeness=policy, max_total_seconds=0.05)

    assert harvester.cancelled is True
    assert len(_FakeSharedBrowser.instances) == 1
    (fake,) = _FakeSharedBrowser.instances
    assert fake.entered is True
    assert fake.exited is True  # browser teardown ran despite the cancellation
