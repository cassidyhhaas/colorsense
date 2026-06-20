"""Quickstart: analyze one or more TRUSTED, hardcoded URLs and print their palettes.

This is the easy path. The URLs below are chosen by *you*, not by your users, so per
SECURITY.md most of its checklist is moot — no SSRF surface, no untrusted input. What
remains is being a polite fetcher, which is the ``PolitenessPolicy`` below: an
identifiable User-Agent (so site operators can attribute and contact), robots.txt
respected (the default), and a conservative gap between same-host fetches.

Run (after `pip install colorsense && playwright install chromium`, or from this repo
`uv sync --group dev && uv run playwright install chromium`):

    python examples/quickstart.py
    python examples/quickstart.py https://your-site.example

For the hardened, untrusted-URL story instead, see examples/webservice/.
"""

from __future__ import annotations

import asyncio
import sys

from colorsense import AnalysisResult, PolitenessPolicy, analyze

# Hardcoded, trusted targets — replace with your own sites. An argv URL overrides them.
DEFAULT_URLS = ("https://example.com",)

# Mechanism, not policy: the library fetches however you tell it to, so tell it to be
# considerate. Edit the UA to identify *your* app and a way to reach you.
POLICY = PolitenessPolicy(
    user_agent="colorsense-quickstart/0.1 (+https://github.com/cassidyhhaas/colorsense)",
    min_interval=2.0,  # at least 2s between fetches to the same host
)


def print_palette(result: AnalysisResult) -> None:
    """Print the color-keyed index and the role-keyed usage view.

    Args:
        result: The analysis result to render to stdout.

    """
    print(result.url)
    for theme, palette in result.themes.items():
        print(f"  [{theme}]")
        # The canonical color-keyed index: every measured color, ranked by prominence,
        # with the usage roles it appears in. Answers "how is each color used?".
        print("    colors (how each color is used):")
        for cu in palette.colors[:6]:
            roles = ", ".join(f"{u.role}={u.weight:.2f}" for u in cu.usages)
            print(f"      {cu.color.hex}  prominence={cu.prominence:.2f}  [{roles}]")
        # The role-keyed projection: which colors paint each usage role. usage.mapping
        # always contains every UsageRole; an empty tuple means nothing was detected for
        # it. Entries are ranked by probability — [0] is the best pick.
        print("    usage (which colors paint each role):")
        for role, entries in palette.usage.mapping.items():
            if not entries:
                print(f"      {role:<8}(none detected)")
                continue
            top = ", ".join(f"{e.color.hex} ({e.probability:.2f})" for e in entries[:3])
            print(f"      {role:<8}{top}")


async def main(urls: tuple[str, ...]) -> None:
    """Analyze each URL in turn with one shared policy and print its palette.

    Args:
        urls: The URLs to analyze, in order.

    """
    # Sequential on purpose: one shared policy paces and caches the fetches; analyze()
    # itself already renders a page's themes concurrently in one shared browser.
    for url in urls:
        result = await analyze(url, politeness=POLICY)
        print_palette(result)


if __name__ == "__main__":
    # Optional override: any URLs passed on the command line replace the defaults.
    asyncio.run(main(tuple(sys.argv[1:]) or DEFAULT_URLS))
