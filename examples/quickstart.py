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
    """Print the usage view (the primary view) per theme, then the derived roles view."""
    print(result.url)
    for theme, palette in result.themes.items():
        print(f"  [{theme}]")
        # The usage view is the primary output: what colors paint each usage category.
        # usage.mapping always contains every UsageCategory; an empty tuple means nothing
        # was detected for it. Entries are ranked by probability — [0] is the best pick.
        for category, entries in palette.usage.mapping.items():
            if not entries:
                print(f"    {category:<13}(none detected)")
                continue
            top = ", ".join(f"{e.color.hex} ({e.probability:.2f})" for e in entries[:3])
            print(f"    {category:<13}{top}")
        # The roles view is a derived 60/30/10 interpretation; fit_score in [0, 1] says
        # how 60/30/10-like the design is (descriptive, not a quality score).
        print(f"    60/30/10 fit {palette.fit_score:.2f}; best per role:")
        for role, candidates in palette.roles.mapping.items():
            if candidates:
                best = candidates[0]
                print(f"      {role:<14}{best.color.hex}  probability={best.probability:.2f}")


async def main(urls: tuple[str, ...]) -> None:
    # Sequential on purpose: one shared policy paces and caches the fetches; analyze()
    # itself already renders a page's themes concurrently in one shared browser.
    for url in urls:
        result = await analyze(url, politeness=POLICY)
        print_palette(result)


if __name__ == "__main__":
    # Optional override: any URLs passed on the command line replace the defaults.
    asyncio.run(main(tuple(sys.argv[1:]) or DEFAULT_URLS))
