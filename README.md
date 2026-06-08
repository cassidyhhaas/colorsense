# colorsense

Extract the rendered color palette from any website and return a structured, typed result
intended for downstream consumers — including AI models — that need to understand a site's
color identity (for example, to derive their own theme-matched colors).

colorsense renders a page under light (and, on request, dark) color schemes, harvests its
design tokens and computed element colors, classifies them into a 60/30/10 palette, and
reconciles what the site *declares* (CSS custom properties) against what it actually *uses*.
It returns the palette roles and scoring; producing concrete color choices for a given
widget is left to the consumer.

## Install

```bash
pip install colorsense
playwright install chromium
```

Rendering uses a headless Chromium via Playwright. The browser binary is **not** a Python
package, so it cannot be pulled in as a pip dependency — run `playwright install chromium`
once after installing to download it (and `playwright install-deps chromium` on Linux to
pull the OS libraries Chromium needs).

For development from a checkout, use [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run playwright install chromium
```

## Usage

`analyze` is **async-native** (it renders with Playwright's async API and renders the
themes concurrently), so await it from an event loop:

```python
import asyncio
from colorsense import analyze
from colorsense.models import PaletteRole

result = asyncio.run(analyze("https://example.com"))

for theme, palette in result.themes.items():
    roles = palette.roles                            # 60/30/10 palette roles + candidates
    cands = roles.mapping[PaletteRole.primary]       # always present; [] if none detected
    if cands:
        primary = cands[0]                           # top candidate for the primary role
        print(theme, primary.color.hex, primary.probability)

print(result.fit_score)          # how well declared intent matches measured usage
print(result.status_colors)      # success/error/warning colors, kept out of the palette
```

Inside an async application (e.g. a FastAPI `async def` endpoint) just
`result = await analyze(url)` directly — no threadpool hop required.

`analyze` returns a fully typed [`AnalysisResult`](src/colorsense/models.py) (a Pydantic
model — `result.model_dump_json()` round-trips). Key fields:

- `themes` — per-theme reconciled palette `roles` (each role with ranked, scored color
  candidates). Sites that ignore `prefers-color-scheme` (near-identical light/dark renders)
  collapse to a single theme.
- `tokens` — declared design tokens with their inferred semantic roles.
- `divergence` — declared-but-unused and used-but-undeclared discrepancies.
- `third_party_colors` / `status_colors` — colors deliberately excluded from the palette.
- `fit_score` — agreement between declared intent and measured usage, in `[0, 1]`.
- `metadata` — a typed [`RunMetadata`](src/colorsense/models.py): themes requested vs.
  analyzed, whether the run collapsed to a single theme, and the fetch policy in effect.

### Options

```python
import asyncio
from colorsense import analyze, LIGHT_AND_DARK, PolitenessPolicy
from colorsense.models import Viewport

result = asyncio.run(
    analyze(
        "https://example.com",
        config_path="my_palette_config.yaml",          # override the bundled token vocab + weights
        viewport=Viewport(width=1440, height=900, device_scale_factor=2.0),
        themes=LIGHT_AND_DARK,                          # opt in to dark mode; default is light only
        politeness=PolitenessPolicy(min_interval=2.0),  # see below
    )
)
```

By default `analyze` renders **light mode only** — most sites have no dark mode, and a
second theme roughly doubles the render cost. Pass `themes=LIGHT_AND_DARK` (equivalently
`themes=(Theme.light, Theme.dark)`) to also analyze dark mode; near-identical light/dark
renders are collapsed back to a single reported theme.

## Deployment: embedded vs server-side, and authorization

colorsense fetches and renders a third-party page. **Authorization is the consumer's
responsibility** — the library provides *mechanism, not policy*. `PolitenessPolicy`
(in [`net/politeness.py`](src/colorsense/net/politeness.py)) gives you the controls:

- a configurable, identifiable **User-Agent**;
- a **`robots.txt` gate**, on by default (`respect_robots=True`) — a disallow raises
  `RobotsDisallowedError`;
- a per-host **rate limiter** (`min_interval` seconds between same-host fetches);
- a simple URL→render **cache**.

Choose your posture by where colorsense runs:

- **Server-side / batch** (you analyze sites you operate or are authorized to crawl): keep
  `respect_robots=True`, set a conservative `min_interval`, and use an identifiable
  User-Agent so site operators can contact you.

  ```python
  policy = PolitenessPolicy(
      user_agent="MyApp/1.0 (+https://myapp.example/bot)",
      min_interval=2.0,
  )
  ```

- **Embedded / on-demand** (a user pastes a URL into your product to theme a widget): you
  may legitimately analyze a page the user is entitled to view. You still own the decision
  to fetch — gate it on your own authorization, terms of service, and rate limits *before*
  calling `analyze`. Disabling `respect_robots` is an explicit, accountable choice, not a
  default.

colorsense never decides whether a fetch is permitted; it only makes it easy to fetch
considerately once you have decided.

## Configuration

All tunable behavior lives in
[`palette_config.yaml`](src/colorsense/data/palette_config.yaml), which **ships bundled with
the package** and is loaded automatically — the single source of truth for the **token
vocabulary** (CSS custom-property names → semantic roles → 60/30/10 palette-role priors) and
the **component-classifier** weights (how rendered elements are scored into headers, cards,
CTAs, …). The weights are calibrated starting points, not ground truth.

To tune them, copy the bundled file, edit your copy, and pass its path as `config_path=` to
`analyze` (or load it with `load_config`). To inspect the defaults programmatically:

```python
from colorsense import load_default_config

config = load_default_config()
```

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Tests are network-free: live-page work runs against saved fixture HTML under
`tests/fixtures/` served via `file://`. Integration tests in
[`tests/test_integration_sites.py`](tests/test_integration_sites.py) pin golden snapshots of
the analysis; regenerate them after an intentional change with:

```bash
UPDATE_GOLDEN=1 uv run pytest tests/test_integration_sites.py
```
