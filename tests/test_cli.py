"""Tests for the ``colorsense`` console script — no browser, no network.

``analyze`` is monkeypatched on the CLI module, so these tests cover the command's own
responsibilities: option parsing reaches the library call, output goes to the right
stream in the right shape, and per-URL errors map to the documented exit codes.
"""

from __future__ import annotations

import inspect
import json
from importlib.metadata import version as package_version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from colorsense import (
    LIGHT_AND_DARK,
    AnalysisResult,
    Color,
    DesignToken,
    PaletteCandidate,
    PaletteRole,
    PolitenessPolicy,
    RenderError,
    RoleResults,
    Theme,
    ThemePalette,
    TokenSemanticRole,
    UsageCategory,
    UsageEntry,
    UsagePalette,
    Viewport,
)
from colorsense import cli as cli_module

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


def make_runner() -> CliRunner:
    """A runner with stdout and stderr separated, across click/typer versions.

    click < 8.2 mixes the streams unless ``mix_stderr=False`` is passed; click >= 8.2
    removed the parameter and always captures them separately.
    """
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    return CliRunner()


runner = make_runner()


def fake_result(url: str, *, include_tokens: bool = False) -> AnalysisResult:
    blue = Color(hex="#336699", lightness=0.5, chroma=0.1, hue=250.0)
    candidate = PaletteCandidate(color=blue, probability=0.9, area=0.6)
    usage = UsagePalette(
        mapping={
            UsageCategory.surface: (UsageEntry(color=blue, probability=0.9, area=0.6),),
        }
    )
    tokens = (
        (DesignToken(name="--brand", color=blue, semantic_role=TokenSemanticRole.brand_primary),)
        if include_tokens
        else None
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
                tokens=tokens,
            )
        },
    )


class AnalyzeStub:
    """Replaces ``cli.analyze``: records calls; returns or raises as configured."""

    def __init__(self, errors: dict[str, BaseException] | None = None) -> None:
        self.errors = errors or {}
        self.calls: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def __call__(self, url: str, **kwargs: object) -> AnalysisResult:
        self.calls.append(url)
        self.kwargs.append(dict(kwargs))
        if url in self.errors:
            raise self.errors[url]
        return fake_result(url, include_tokens=bool(kwargs.get("include_tokens")))


def install_stub(monkeypatch: pytest.MonkeyPatch, stub: AnalyzeStub) -> None:
    monkeypatch.setattr(cli_module, "analyze", stub)


def test_human_output_leads_with_usage_then_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub(monkeypatch, AnalyzeStub())
    result = runner.invoke(cli_module.app, ["https://example.com"])
    assert result.exit_code == 0
    # The usage view comes first, then the roles summary with the fit score.
    assert "usage:" in result.stdout
    assert "surface" in result.stdout
    assert result.stdout.index("usage:") < result.stdout.index("roles")
    assert "fit score 0.80" in result.stdout
    assert "#336699" in result.stdout
    assert "primary" in result.stdout
    assert "probability=0.90" in result.stdout
    # Undetected roles/categories are still listed (the mappings carry every key).
    assert "accent" in result.stdout
    assert "interactive" in result.stdout
    # Tokens were not requested: no tokens section.
    assert "tokens:" not in result.stdout


def test_tokens_flag_wires_include_tokens_and_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, ["https://example.com", "--tokens"])
    assert result.exit_code == 0
    (kwargs,) = stub.kwargs
    assert kwargs["include_tokens"] is True
    assert "tokens:" in result.stdout
    assert "--brand" in result.stdout
    assert "brand_primary" in result.stdout


def test_include_tokens_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, ["https://example.com"])
    assert result.exit_code == 0
    (kwargs,) = stub.kwargs
    assert kwargs["include_tokens"] is False


def test_human_output_prints_every_url(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    urls = ["https://one.example", "https://two.example"]
    result = runner.invoke(cli_module.app, urls)
    assert result.exit_code == 0
    assert stub.calls == urls
    for url in urls:
        assert url in result.stdout


def test_json_single_url_is_one_parseable_document(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub(monkeypatch, AnalyzeStub())
    result = runner.invoke(cli_module.app, ["https://example.com", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)  # the whole stream must be one JSON document
    assert body["url"] == "https://example.com"
    theme = body["themes"]["light"]
    assert theme["fit_score"] == 0.8
    surface = theme["usage"]["mapping"]["surface"]
    assert surface[0]["color"]["hex"] == "#336699"
    primary = theme["roles"]["mapping"]["primary"]
    assert primary[0]["color"]["hex"] == "#336699"


def test_json_multiple_urls_is_an_array(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stub(monkeypatch, AnalyzeStub())
    urls = ["https://one.example", "https://two.example"]
    result = runner.invoke(cli_module.app, [*urls, "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert [item["url"] for item in body] == urls


def test_json_single_failing_url_emits_null(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even on failure, --json stdout must be exactly one valid JSON document so a
    # ``colorsense URL --json | jq`` pipeline never breaks; the error rides on stderr.
    stub = AnalyzeStub(errors={"https://bad.example": RenderError("boom")})
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, ["https://bad.example", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) is None
    assert "boom" in result.stderr


def test_options_reach_analyze(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    result = runner.invoke(
        cli_module.app,
        [
            "https://example.com",
            "--dark",
            "--viewport",
            "1440x900",
            "--scale",
            "2.0",
            "--max-total-seconds",
            "45",
            "--min-interval",
            "3.5",
            "--user-agent",
            "MyApp/1.0",
            "--config",
            "my_palette.yaml",
        ],
    )
    assert result.exit_code == 0
    (kwargs,) = stub.kwargs
    assert kwargs["config_path"] == Path("my_palette.yaml")
    assert kwargs["themes"] == LIGHT_AND_DARK
    assert kwargs["viewport"] == Viewport(width=1440, height=900, device_scale_factor=2.0)
    assert kwargs["max_total_seconds"] == 45.0
    policy = kwargs["politeness"]
    assert isinstance(policy, PolitenessPolicy)
    assert policy.min_interval == 3.5
    assert policy.user_agent == "MyApp/1.0"
    assert policy.respect_robots is True
    assert policy.request_filter is None


def test_browser_arg_repeatable_reaches_analyze(monkeypatch: pytest.MonkeyPatch) -> None:
    # --browser-arg is repeatable and maps to the analyze browser_args tuple verbatim,
    # in order; without the flag the tuple is empty (no behavior change).
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    result = runner.invoke(
        cli_module.app,
        [
            "https://example.com",
            "--browser-arg",
            "--js-flags=--max-old-space-size=512",
            "--browser-arg",
            "--disable-dev-shm-usage",
        ],
    )
    assert result.exit_code == 0
    (kwargs,) = stub.kwargs
    assert kwargs["browser_args"] == (
        "--js-flags=--max-old-space-size=512",
        "--disable-dev-shm-usage",
    )


def test_browser_args_default_to_empty_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, ["https://example.com"])
    assert result.exit_code == 0
    (kwargs,) = stub.kwargs
    assert kwargs["browser_args"] == ()


def test_one_shared_policy_across_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, ["https://one.example", "https://two.example"])
    assert result.exit_code == 0
    first, second = stub.kwargs
    assert first["politeness"] is second["politeness"]


def test_block_private_networks_installs_request_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, ["https://example.com", "--block-private-networks"])
    assert result.exit_code == 0
    (kwargs,) = stub.kwargs
    policy = kwargs["politeness"]
    assert isinstance(policy, PolitenessPolicy)
    assert callable(policy.request_filter)


def test_no_robots_disables_check_and_warns_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, ["https://example.com", "--no-robots"])
    assert result.exit_code == 0
    (kwargs,) = stub.kwargs
    policy = kwargs["politeness"]
    assert isinstance(policy, PolitenessPolicy)
    assert policy.respect_robots is False
    assert "robots" in result.stderr
    assert "robots" not in result.stdout


@pytest.mark.parametrize("value", ["garbage", "1280", "x800", "12.5x800", "-1280x800"])
def test_bad_viewport_exits_2(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    stub = AnalyzeStub()
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, ["https://example.com", "--viewport", value])
    assert result.exit_code == 2
    assert stub.calls == []  # rejected before any analyze call


def test_failed_url_reports_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    bad, good = "https://bad.example", "https://good.example"
    stub = AnalyzeStub(errors={bad: RenderError("render failed")})
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, [bad, good])
    assert result.exit_code == 1
    assert stub.calls == [bad, good]  # the second URL is still processed
    assert bad in result.stderr
    assert "render failed" in result.stderr
    assert good in result.stdout
    assert bad not in result.stdout


def test_json_mode_keeps_errors_off_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    bad, good = "https://bad.example", "https://good.example"
    stub = AnalyzeStub(errors={bad: RenderError("render failed")})
    install_stub(monkeypatch, stub)
    result = runner.invoke(cli_module.app, [bad, good, "--json"])
    assert result.exit_code == 1
    body = json.loads(result.stdout)  # stdout stays pure JSON despite the failure
    assert [item["url"] for item in body] == [good]
    assert bad in result.stderr


def test_version_prints_installed_version() -> None:
    result = runner.invoke(cli_module.app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"colorsense {package_version('colorsense')}"
