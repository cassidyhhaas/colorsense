"""Tests for the example FastAPI service — no browser, no network.

``analyze`` is monkeypatched on the app module, so these tests cover the endpoint's own
responsibilities: pre-call validation rejects bad URLs *before* any render, library errors
map to the documented HTTP statuses, and the response is the trimmed shape. Skips cleanly
when fastapi is not installed (it lives in the ``examples`` dependency group, not ``dev``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from colorsense import (
    AnalysisResult,
    AnalysisTimeoutError,
    Color,
    PaletteCandidate,
    PaletteRole,
    RenderError,
    RobotsDisallowedError,
    RoleResults,
    Theme,
    ThemePalette,
    UnsupportedSchemeError,
    Viewport,
)
from examples.webservice import app as app_module

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


def fake_result(url: str) -> AnalysisResult:
    candidate = PaletteCandidate(
        color=Color(hex="#336699", lightness=0.5, chroma=0.1, hue=250.0),
        probability=0.9,
        area=0.6,
        evidence={"internal": 1.0},  # must be trimmed from the API response
    )
    return AnalysisResult(
        url=url,
        viewport=VIEWPORT,
        themes={
            Theme.light: ThemePalette(
                theme=Theme.light,
                roles=RoleResults(mapping={PaletteRole.primary: (candidate,)}),
            )
        },
        fit_score=0.8,
    )


class AnalyzeStub:
    """Replaces ``app_module.analyze``: records calls; returns or raises as configured."""

    def __init__(
        self, result: AnalysisResult | None = None, error: BaseException | None = None
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def __call__(self, url: str, **kwargs: object) -> AnalysisResult:
        self.calls.append(url)
        self.kwargs.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app_module.app) as test_client:
        yield test_client


def install_stub(monkeypatch: pytest.MonkeyPatch, stub: AnalyzeStub) -> None:
    monkeypatch.setattr(app_module, "analyze", stub)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pass@example.com/",
        "https:///nohost",
        "not a url",
    ],
)
def test_bad_urls_rejected_before_any_render(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    stub = AnalyzeStub(result=fake_result(url))
    install_stub(monkeypatch, stub)
    response = client.post("/analyze", json={"url": url})
    assert response.status_code == 400
    assert stub.calls == []  # validation must fire before analyze is ever awaited


def test_allowlist_enforced_before_any_render(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = AnalyzeStub(result=fake_result("https://other.example/"))
    install_stub(monkeypatch, stub)
    monkeypatch.setattr(app_module, "ALLOWED_HOSTS", frozenset({"example.com"}))
    response = client.post("/analyze", json={"url": "https://other.example/"})
    assert response.status_code == 400
    assert "allowlist" in response.json()["detail"]
    assert stub.calls == []


def test_success_returns_trimmed_palette(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.com/"
    install_stub(monkeypatch, AnalyzeStub(result=fake_result(url)))
    response = client.post("/analyze", json={"url": url})
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == url
    assert body["fit_score"] == 0.8
    primary = body["themes"]["light"]["primary"]
    assert primary == [{"hex": "#336699", "probability": 0.9, "area": 0.6}]
    # Empty roles are present (the library guarantees all five); internals are trimmed.
    assert body["themes"]["light"]["accent"] == []
    assert "evidence" not in str(body)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RenderError("render failed"), 502),
        (RobotsDisallowedError("https://example.com/"), 403),
        (UnsupportedSchemeError("gopher://example.com/"), 400),
        # What analyze(max_total_seconds=...) raises on deadline expiry.
        (AnalysisTimeoutError("https://example.com/", 60.0), 504),
    ],
)
def test_analyze_errors_map_to_http_statuses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: BaseException, status: int
) -> None:
    install_stub(monkeypatch, AnalyzeStub(error=error))
    response = client.post("/analyze", json={"url": "https://example.com/"})
    assert response.status_code == status


def test_endpoint_passes_policy_and_deadline_to_analyze(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The §2 bounds are library knobs now: the endpoint must hand analyze the shared
    # policy (which carries max_concurrent_renders), the overall deadline, and the
    # browser_args V8-heap cap.
    url = "https://example.com/"
    stub = AnalyzeStub(result=fake_result(url))
    install_stub(monkeypatch, stub)
    response = client.post("/analyze", json={"url": url})
    assert response.status_code == 200
    (kwargs,) = stub.kwargs
    assert kwargs["politeness"] is app_module._policy
    assert kwargs["max_total_seconds"] == app_module.ANALYZE_DEADLINE_SECONDS
    assert kwargs["browser_args"] == app_module.BROWSER_ARGS
    assert app_module._policy.max_concurrent_renders == app_module.MAX_CONCURRENT_ANALYSES
    # The default (no COLORSENSE_BROWSER_ARGS override) is the documented V8-heap cap.
    assert app_module.BROWSER_ARGS == ("--js-flags=--max-old-space-size=512",)
