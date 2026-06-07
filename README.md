# colorsense

Extract the rendered color palette from any website and return a structured, typed result
intended for downstream consumers — including AI models — that build embedded widgets
matching a site's theme.

colorsense renders a page under both light and dark color schemes, harvests its design
tokens and computed element colors, classifies them into a 60/30/10 palette, reconciles
what the site *declares* (CSS custom properties) against what it actually *uses*, and emits
WCAG-safe widget color recommendations per theme.

## Install

```bash
uv sync
uv run playwright install chromium
```

Rendering uses a headless Chromium via Playwright; the `playwright install` step is
required once.

## Usage

```python
from colorsense import analyze

result = analyze("https://example.com")

for theme, palette in result.themes.items():
    rec = palette.recommendation
    print(theme, rec.cta_bg.hex, rec.cta_text.hex)   # WCAG-enforced widget colors

print(result.fit_score)          # how well declared intent matches measured usage
print(result.status_colors)      # success/error/warning colors, kept out of the palette
```

`analyze` returns a fully typed [`AnalysisResult`](src/colorsense/models.py) (a Pydantic
model — `result.model_dump_json()` round-trips). Key fields:

- `themes` — per-theme reconciled `roles` and a `recommendation`. Sites that ignore
  `prefers-color-scheme` (near-identical light/dark renders) collapse to a single theme.
- `tokens` — declared design tokens with their inferred semantic roles.
- `divergence` — declared-but-unused and used-but-undeclared discrepancies.
- `third_party_colors` / `status_colors` — colors deliberately excluded from the palette.
- `fit_score` — agreement between declared intent and measured usage, in `[0, 1]`.

### Options

```python
from colorsense import analyze, PolitenessPolicy
from colorsense.models import Theme, Viewport

result = analyze(
    "https://example.com",
    config_path="config/palette_config.yaml",          # token vocab + classifier weights
    viewport=Viewport(w=1440, h=900, device_scale_factor=2.0),
    themes=(Theme.light,),                              # render a single theme
    politeness=PolitenessPolicy(min_interval=2.0),     # see below
)
```

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

All tunable behavior lives in [`config/palette_config.yaml`](config/palette_config.yaml) —
the single source of truth for the **token vocabulary** (CSS custom-property names →
semantic roles → 60/30/10 palette-role priors) and the **component-classifier** weights
(how rendered elements are scored into headers, cards, CTAs, …). The weights are calibrated
starting points, not ground truth: tune values there against your own labeled sites rather
than hard-coding thresholds in Python.

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
