"""Tests for the example FastAPI service — no browser, no network.

``analyze`` is monkeypatched where the endpoint looks it up (``examples.webservice.routes``),
so these tests cover the endpoint's own responsibilities: pre-call validation rejects bad
URLs *before* any render, library errors map to the documented HTTP statuses, and the
response is the trimmed shape. Skips cleanly when fastapi is not installed (it lives in
the ``examples`` dependency group, not ``dev``).
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
    ComponentType,
    PaletteCandidate,
    PaletteRole,
    RenderError,
    RobotsDisallowedError,
    RoleResults,
    Theme,
    ThemePalette,
    UnsupportedSchemeError,
    UsageCategory,
    UsageEntry,
    UsagePalette,
    Viewport,
)
from examples.webservice import main, policy, routes, settings

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


def fake_result(url: str) -> AnalysisResult:
    blue = Color(hex="#336699", lightness=0.5, chroma=0.1, hue=250.0)
    candidate = PaletteCandidate(color=blue, probability=0.9, area=0.6)
    usage = UsagePalette(
        mapping={
            UsageCategory.surface: (
                UsageEntry(
                    color=blue,
                    probability=0.9,
                    area=0.6,
                    # Component evidence must be trimmed from the API response.
                    components={ComponentType.page_bg: 1.0},
                ),
            ),
        }
    )
    return AnalysisResult(
        url=url,
        viewport=VIEWPORT,
        themes={
            Theme.light: ThemePalette(
                theme=Theme.light,
                usage=usage,
                roles=RoleResults(mapping={PaletteRole.primary: (candidate,)}),
                fit_score=0.8,
            )
        },
    )


class AnalyzeStub:
    """Replaces ``routes.analyze``: records calls; returns or raises as configured."""

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
    with TestClient(main.app) as test_client:
        yield test_client


def install_stub(monkeypatch: pytest.MonkeyPatch, stub: AnalyzeStub) -> None:
    # Patch where the endpoint looks the name up: the routes module, not colorsense.
    monkeypatch.setattr(routes, "analyze", stub)


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


def test_oversized_url_rejected_by_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AnalyzeRequest bounds untrusted input: URLs beyond max_length (2083) fail model
    # validation with a 422 before urlsplit/resolution — or any render — is reached.
    stub = AnalyzeStub(result=fake_result("https://example.com/"))
    install_stub(monkeypatch, stub)
    oversized = "https://example.com/" + "a" * 5000
    response = client.post("/analyze", json={"url": oversized})
    assert response.status_code == 422
    assert stub.calls == []


def test_allowlist_enforced_before_any_render(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = AnalyzeStub(result=fake_result("https://other.example/"))
    install_stub(monkeypatch, stub)
    # The endpoint reads the module global at call time, so patch it on routes (where
    # `from ...settings import ALLOWED_HOSTS` bound it), not on settings.
    monkeypatch.setattr(routes, "ALLOWED_HOSTS", frozenset({"example.com"}))
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
    theme = body["themes"]["light"]
    assert theme["fit_score"] == 0.8
    surface = theme["usage"]["surface"]
    assert surface == [{"hex": "#336699", "probability": 0.9, "area": 0.6}]
    primary = theme["roles"]["primary"]
    assert primary == [{"hex": "#336699", "probability": 0.9, "area": 0.6}]
    # Empty categories/roles are present (the library guarantees the full key sets);
    # internals (component evidence, OKLCH coordinates) are trimmed.
    assert theme["usage"]["interactive"] == []
    assert theme["roles"]["accent"] == []
    assert "components" not in str(body)
    assert "page_bg" not in str(body)


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
    assert kwargs["politeness"] is policy.POLICY
    assert kwargs["max_total_seconds"] == settings.ANALYZE_DEADLINE_SECONDS
    assert kwargs["browser_args"] == settings.BROWSER_ARGS
    assert policy.POLICY.max_concurrent_renders == settings.MAX_CONCURRENT_ANALYSES
    # The default (no COLORSENSE_BROWSER_ARGS override) is the documented V8-heap cap.
    assert settings.BROWSER_ARGS == ("--js-flags=--max-old-space-size=512",)


def test_browser_args_default_is_v8_heap_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLORSENSE_BROWSER_ARGS", raising=False)
    assert settings.browser_args_from_env() == ("--js-flags=--max-old-space-size=512",)


def test_browser_args_empty_string_means_no_extra_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLORSENSE_BROWSER_ARGS", "")
    assert settings.browser_args_from_env() == ()


def test_browser_args_whitespace_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "COLORSENSE_BROWSER_ARGS", "--disable-gpu  --js-flags=--max-old-space-size=256"
    )
    assert settings.browser_args_from_env() == (
        "--disable-gpu",
        "--js-flags=--max-old-space-size=256",
    )


def test_browser_args_quoted_flag_with_commas_and_spaces_stays_one_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shlex upgrade exists exactly for this: a flag whose value contains commas and
    # spaces (multiple host-resolver rules) must survive as a single argument.
    monkeypatch.setenv(
        "COLORSENSE_BROWSER_ARGS",
        "--host-resolver-rules='MAP a 1.2.3.4, MAP b 5.6.7.8' --disable-gpu",
    )
    assert settings.browser_args_from_env() == (
        "--host-resolver-rules=MAP a 1.2.3.4, MAP b 5.6.7.8",
        "--disable-gpu",
    )


def test_browser_args_unbalanced_quote_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Loud failure at import/startup for a misconfigured env var, never mangled args.
    monkeypatch.setenv("COLORSENSE_BROWSER_ARGS", "--host-resolver-rules='MAP a 1.2.3.4")
    with pytest.raises(ValueError):
        settings.browser_args_from_env()
