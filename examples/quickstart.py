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
    """Print the best candidate per role for every analyzed theme, plus the fit score."""
    # fit_score in [0, 1]: how closely the page matches the canonical 60/30/10 split —
    # a quick quality signal for the analysis as a whole.
    print(f"{result.url}  (fit score {result.fit_score:.2f})")
    for theme, palette in result.themes.items():
        print(f"  [{theme}]")
        # roles.mapping always contains every PaletteRole; an empty tuple means the role
        # was not detected. Candidates are ranked by probability — [0] is the best pick.
        for role, candidates in palette.roles.mapping.items():
            if not candidates:
                print(f"    {role:<14}(none detected)")
                continue
            best = candidates[0]
            print(
                f"    {role:<14}{best.color.hex}"
                f"  probability={best.probability:.2f}  area={best.area:.2f}"
            )


async def main(urls: tuple[str, ...]) -> None:
    # Sequential on purpose: one shared policy paces and caches the fetches; analyze()
    # itself already renders a page's themes concurrently in one shared browser.
    for url in urls:
        result = await analyze(url, politeness=POLICY)
        print_palette(result)


if __name__ == "__main__":
    # Optional override: any URLs passed on the command line replace the defaults.
    asyncio.run(main(tuple(sys.argv[1:]) or DEFAULT_URLS))
