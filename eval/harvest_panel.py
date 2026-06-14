"""Re-capture the frozen panel harvests in ``eval/harvests/`` from the live sites.

The eval (``eval/score.py``) runs offline against *frozen* harvests so that code changes
are isolated from page drift. Those frozen harvests still need to be refreshed when sites
redesign meaningfully — that is this script's job. It launches real Chromium (Playwright),
so it needs network and ``uv run playwright install chromium``; it is NOT part of the test
suite and is run by hand from a network-connected environment.

    uv run python eval/harvest_panel.py            # refresh the whole panel
    uv run python eval/harvest_panel.py github stripe   # refresh named sites

The panel maps the format-spanning corpus Cass curated (component frameworks, dark-first
sites, e-commerce, a token-rich SPA, consent/login-walled controls). Keep the names in
sync with ``eval/ground_truth.yaml``. Harvests are written gzipped (~40 KB/site) so the
whole corpus stays well under 1 MB in-repo.
"""

from __future__ import annotations

import asyncio
import gzip
import sys
from pathlib import Path

from colorsense.config import load_default_config
from colorsense.harvest import harvest_page
from colorsense.models import Theme, Viewport

OUT_DIR = Path(__file__).parent / "harvests"

# name -> live URL. Names are the eval keys (also the ground_truth.yaml keys).
PANEL: dict[str, str] = {
    "shadcn": "https://ui.shadcn.com",
    "vercel": "https://vercel.com",
    "stripe": "https://stripe.com",
    "github": "https://github.com",
    "tailwindcss": "https://tailwindcss.com",
    "supabase": "https://supabase.com",
    "resend": "https://resend.com",
    "linear": "https://linear.app",
    "notion": "https://www.notion.com",
    "disconetwork": "https://disconetwork.com",
    # Harvest-completeness controls (consent/login-walled): kept to track thin harvests.
    "klarna": "https://www.klarna.com",
    "platform_disco": "https://platform.disconetwork.com",
}

VIEWPORT = Viewport(width=1280, height=800, device_scale_factor=1.0)


async def _capture(name: str, url: str) -> None:
    config = load_default_config()
    harvest = await harvest_page(url, Theme.light, config, VIEWPORT)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json.gz"
    path.write_bytes(gzip.compress(harvest.model_dump_json().encode()))
    print(
        f"{name:14} {len(harvest.elements):4d} elements, "
        f"{len(harvest.screenshot_bins):2d} bins -> {path.name}",
        flush=True,
    )


async def main(names: list[str]) -> None:
    selected = names or list(PANEL)
    for name in selected:
        if name not in PANEL:
            print(f"warning: {name} not in PANEL; skipping", file=sys.stderr)
            continue
        try:
            await _capture(name, PANEL[name])
        except Exception as exc:  # report and continue the panel
            print(f"{name:14} FAILED: {exc!r}"[:160], file=sys.stderr, flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
