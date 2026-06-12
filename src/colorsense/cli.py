"""``colorsense`` command line — a convenience entry point over
[`colorsense.analyze`][colorsense.analyze].

The typed API ([`colorsense.analyze`][colorsense.analyze]) is the contract. The human-readable
output of this command is **not stable** and may change between releases; ``--json`` emits the
[`AnalysisResult`][colorsense.AnalysisResult] schema (``model_dump_json``), which follows the
library's compatibility story. Installed as the ``colorsense`` console script.

stdout carries data only (the palette summary, or JSON documents under ``--json``);
warnings and per-URL errors go to stderr.
"""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Annotated

import typer

from colorsense import (
    DEFAULT_VIEWPORT,
    LIGHT_AND_DARK,
    AnalysisResult,
    AnalysisTimeoutError,
    PolitenessPolicy,
    RenderError,
    RobotsDisallowedError,
    Theme,
    UnsupportedSchemeError,
    Viewport,
    analyze,
    block_private_networks,
)

DEFAULT_THEMES: tuple[Theme, ...] = (Theme.light,)

CLI_USER_AGENT = (
    f"colorsense-cli/{_package_version('colorsense')} (+https://github.com/cassidyhhaas/colorsense)"
)
"""Default User-Agent for CLI runs: identifies the tool and a way to reach the project.

Deliberately distinct from the library default so site operators can tell an interactive
CLI invocation apart from an embedding application; pass ``--user-agent`` to identify
*your* application instead.
"""

_NO_ROBOTS_WARNING = (
    "warning: --no-robots disables the robots.txt check (and its Crawl-delay honoring). "
    "Only do this for sites you own or are explicitly authorized to crawl — see SECURITY.md."
)

app = typer.Typer(add_completion=False)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"colorsense {_package_version('colorsense')}")
        raise typer.Exit()


def _parse_viewport(value: str, scale: float) -> Viewport:
    """Parse ``WxH`` (plus the ``--scale`` factor) into a [`Viewport`][colorsense.Viewport]."""
    width_text, sep, height_text = value.lower().partition("x")
    if not sep or not width_text.strip().isdigit() or not height_text.strip().isdigit():
        raise typer.BadParameter(
            f"expected WIDTHxHEIGHT (e.g. 1280x800), got {value!r}", param_hint="'--viewport'"
        )
    width, height = int(width_text), int(height_text)
    if width <= 0 or height <= 0:
        raise typer.BadParameter("width and height must be positive", param_hint="'--viewport'")
    if scale <= 0:
        raise typer.BadParameter("must be positive", param_hint="'--scale'")
    return Viewport(width=width, height=height, device_scale_factor=scale)


def _print_palette(result: AnalysisResult) -> None:
    """Human summary per theme: the usage view first, then roles + fit score, divergence,
    and (when requested) the declared tokens."""
    typer.echo(result.url)
    for theme, palette in result.themes.items():
        typer.echo(f"  [{theme}]")

        typer.echo("    usage:")
        for category, entries in palette.usage.mapping.items():
            if not entries:
                typer.echo(f"      {category:<13}(none detected)")
                continue
            typer.echo(f"      {category}:")
            for entry in entries:
                typer.echo(
                    f"        {entry.color.hex}"
                    f"  probability={entry.probability:.2f}  area={entry.area:.2f}"
                )

        typer.echo(f"    roles (60/30/10 view, fit score {palette.fit_score:.2f}):")
        for role, candidates in palette.roles.mapping.items():
            if not candidates:
                typer.echo(f"      {role:<14}(none detected)")
                continue
            best = candidates[0]
            typer.echo(
                f"      {role:<14}{best.color.hex}"
                f"  probability={best.probability:.2f}  area={best.area:.2f}"
            )

        if palette.divergence:
            typer.echo("    divergence:")
            for item in palette.divergence:
                typer.echo(f"      {item.category:<13}{item.color.hex}  {item.note}")

        if palette.tokens is not None:
            typer.echo("    tokens:")
            if not palette.tokens:
                typer.echo("      (no usable color tokens)")
            for token in palette.tokens:
                typer.echo(f"      {token.name}  {token.color.hex}  {token.semantic_role}")


async def _run(
    urls: list[str],
    *,
    policy: PolitenessPolicy,
    themes: tuple[Theme, ...],
    viewport: Viewport,
    config_path: Path | None,
    max_total_seconds: float | None,
    browser_args: tuple[str, ...],
    include_tokens: bool,
    json_output: bool,
) -> int:
    """Analyze ``urls`` sequentially through one shared policy; return the failure count."""
    failures = 0
    results: list[AnalysisResult] = []
    for url in urls:
        try:
            result = await analyze(
                url,
                viewport=viewport,
                themes=themes,
                politeness=policy,
                config_path=config_path,
                max_total_seconds=max_total_seconds,
                browser_args=browser_args,
                include_tokens=include_tokens,
            )
        except (
            RobotsDisallowedError,
            UnsupportedSchemeError,
            RenderError,
            AnalysisTimeoutError,
        ) as exc:
            typer.echo(f"error: {url}: {exc}", err=True)
            failures += 1
            continue
        if json_output:
            results.append(result)
        else:
            _print_palette(result)
    if json_output:
        # stdout is always exactly one valid JSON document, success or failure, so piping
        # into e.g. ``jq`` never breaks: one URL -> the result object (``null`` when it
        # failed), several URLs -> the array of successful results (``[]`` when all failed).
        if len(urls) == 1:
            typer.echo(results[0].model_dump_json(indent=2) if results else "null")
        else:
            typer.echo(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
    return failures


@app.command()
def main(
    urls: Annotated[
        list[str],
        typer.Argument(metavar="URL...", help="One or more http(s) URLs to analyze."),
    ],
    dark: Annotated[
        bool,
        typer.Option("--dark/--no-dark", help="Also render and analyze dark mode."),
    ] = False,
    viewport: Annotated[
        str,
        typer.Option(metavar="WxH", help="Render viewport, width x height in pixels."),
    ] = f"{DEFAULT_VIEWPORT.width}x{DEFAULT_VIEWPORT.height}",
    scale: Annotated[
        float,
        typer.Option(metavar="FLOAT", help="Device scale factor for the viewport."),
    ] = DEFAULT_VIEWPORT.device_scale_factor,
    config: Annotated[
        Path | None,
        typer.Option(metavar="PATH", help="Palette config YAML overriding the bundled default."),
    ] = None,
    max_total_seconds: Annotated[
        float | None,
        typer.Option(
            metavar="FLOAT",
            help="Overall deadline per URL (renders plus classification); no deadline if unset.",
        ),
    ] = None,
    browser_arg: Annotated[
        list[str] | None,
        typer.Option(
            "--browser-arg",
            metavar="TEXT",
            help="Extra Chromium launch argument, passed verbatim; repeatable. "
            "E.g. --browser-arg='--js-flags=--max-old-space-size=512' caps each "
            "renderer's V8 heap (JS heap only — container limits remain the hard bound; "
            "see SECURITY.md).",
        ),
    ] = None,
    min_interval: Annotated[
        float,
        typer.Option(metavar="FLOAT", help="Minimum seconds between same-host fetches."),
    ] = 1.0,
    user_agent: Annotated[
        str,
        typer.Option(metavar="TEXT", help="User-Agent sent on the wire (robots GET and render)."),
    ] = CLI_USER_AGENT,
    block_private: Annotated[
        bool,
        typer.Option(
            "--block-private-networks",
            help="Reject requests resolving to private/loopback/metadata addresses "
            "(installs block_private_networks() as the egress filter).",
        ),
    ] = False,
    tokens: Annotated[
        bool,
        typer.Option(
            "--tokens",
            help="Include the declared design tokens (CSS custom properties) per theme.",
        ),
    ] = False,
    no_robots: Annotated[
        bool,
        typer.Option(
            "--no-robots",
            help="Disable the robots.txt check. Only for sites you own or are authorized "
            "to crawl; see SECURITY.md.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the full AnalysisResult as JSON: one object for a single URL "
            "(null if it failed), an array of successful results for multiple URLs.",
        ),
    ] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the installed colorsense version and exit.",
        ),
    ] = False,
) -> None:
    """Analyze each URL's rendered color palette and print it.

    URLs are fetched sequentially through one shared politeness policy.

    Exit status: 0 when every URL succeeded, 1 when any failed, 2 on bad arguments.
    """
    parsed_viewport = _parse_viewport(viewport, scale)
    if no_robots:
        typer.echo(_NO_ROBOTS_WARNING, err=True)
    policy = PolitenessPolicy(
        user_agent=user_agent,
        respect_robots=not no_robots,
        request_filter=block_private_networks() if block_private else None,
        min_interval=min_interval,
    )
    try:
        failures = asyncio.run(
            _run(
                urls,
                policy=policy,
                themes=LIGHT_AND_DARK if dark else DEFAULT_THEMES,
                viewport=parsed_viewport,
                config_path=config,
                max_total_seconds=max_total_seconds,
                browser_args=tuple(browser_arg or ()),
                include_tokens=tokens,
                json_output=json_output,
            )
        )
    except KeyboardInterrupt:  # Ctrl-C: no traceback, conventional 130.
        raise typer.Exit(130) from None
    if failures:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
