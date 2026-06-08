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

## Quickstart

`analyze` is **async-native** (it renders with Playwright's async API and renders the
themes concurrently), so await it from an event loop. The result's `themes` map each color
scheme to its **60/30/10 palette** — five roles, each with ranked, scored candidate colors:

```python
import asyncio
from colorsense import analyze, PaletteRole

result = asyncio.run(analyze("https://example.com"))

for theme, palette in result.themes.items():
    # mapping always contains every role; () when none was detected
    candidates = palette.roles.mapping[PaletteRole.primary]
    if candidates:
        primary = candidates[0]               # top candidate for the role
        print(theme, primary.color.hex, primary.probability)
```

Each role — `primary`, `secondary`, `accent`, `neutral_light`, `neutral_dark` — maps to a
probability-ranked tuple of candidates. Take `[0]` for the best pick.

Inside an async application (e.g. a FastAPI `async def` endpoint) just
`result = await analyze(url)` directly — no threadpool hop required.

## The result

`analyze` returns a fully typed `AnalysisResult` (a Pydantic model —
`result.model_dump_json()` round-trips). The fields most consumers use:

**`themes`** — the payload: each `Theme` mapped to its reconciled palette `roles`. You walk
`palette.roles.mapping[role]` to a tuple of candidates, where each candidate carries:

- `color` — a `Color`: an sRGB `hex` string plus cached **OKLCH** coordinates (`lightness`,
  `chroma`, `hue`) of the composited color, and the source `alpha`. `hex` is what you paint
  with; the OKLCH coordinates make it easy to derive your own theme-matched colors — sort by
  perceptual lightness, build accessible tints/shades, or compute contrast — without
  re-parsing the hex.
- `probability` — confidence this color fills the role (candidates within a role rank by it).
- `area` — the fraction of page area the color covers, i.e. its 60/30/10 dominance.

Sites that ignore `prefers-color-scheme` (near-identical light/dark renders) collapse to a
single reported theme.

**`fit_score`** — how well the measured palette matches the canonical 60/30/10 split, in
`[0, 1]`. A quick quality signal for the analysis as a whole.

**`status_colors`** — success/error/warning colors detected and deliberately **kept out** of
the palette, so a red error banner doesn't masquerade as a brand accent.

**`metadata`** — a typed `RunMetadata`: which themes were requested versus actually analyzed,
whether the run collapsed to a single theme, and the fetch policy in effect. Useful for
logging and for detecting the single-theme collapse.

## Options

```python
import asyncio
from colorsense import analyze, LIGHT_AND_DARK, PolitenessPolicy, Viewport

result = asyncio.run(
    analyze(
        "https://example.com",
        viewport=Viewport(width=1440, height=900, device_scale_factor=2.0),
        themes=LIGHT_AND_DARK,                          # opt in to dark mode; default is light only
        politeness=PolitenessPolicy(min_interval=2.0),  # see "Fetching responsibly" below
        config_path="my_palette_config.yaml",           # advanced; see "Custom tuning" below
    )
)
```

By default `analyze` renders **light mode only** — most sites have no dark mode, and a
second theme roughly doubles the render cost. Pass `themes=LIGHT_AND_DARK` (equivalently
`themes=(Theme.light, Theme.dark)`) to also analyze dark mode; near-identical light/dark
renders are collapsed back to a single reported theme. A custom `viewport` captures a
different layout (e.g. mobile), which can yield a different palette.

## Fetching responsibly: politeness, authorization & security

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

**Security (SSRF + local-file reads).** `analyze` fetches and renders whatever URL it is
given, so passing **untrusted** URLs exposes a server-side request forgery and local-file-read
surface. `file://` URLs read arbitrary local files (intentional, for the test fixtures), and
`http(s)://` URLs can reach internal hosts and cloud metadata endpoints (e.g.
`169.254.169.254`, `localhost`). This is by design — the politeness controls above gate
*network* schemes for robots/rate-limiting, but nothing validates the destination host. If
you accept user-supplied URLs, validate the scheme and host **before** calling `analyze`:
allowlist public hosts, and reject `file://` and private / link-local IP ranges. As above,
this is the consumer's responsibility — the library provides mechanism, not policy.

## Advanced

### Design-token auditing

Beyond the palette, `analyze` reports what the site's CSS **declares** versus what it
actually **renders** — useful for auditing a design system you own:

- **`tokens`** — the declared design tokens (CSS custom properties) with their inferred
  semantic roles (e.g. `--accent-500` read as `brand_accent`), for the primary theme.
- **`divergence`** — discrepancies between intent and usage: brand colors **declared but
  unused** in the render, and prominent rendered colors that are **used but undeclared**.

```python
for item in result.divergence:
    print(item.note, item.color.hex)     # e.g. "declared '--brand' unused in render"
```

### Custom tuning

[`palette_config.yaml`](src/colorsense/data/palette_config.yaml) **ships bundled with the
package** and is loaded automatically. It is the single source of truth for the **token
vocabulary** (CSS custom-property names → semantic roles → 60/30/10 palette-role priors) and
the **component-classifier** weights (how rendered elements are scored into headers, cards,
CTAs, …). The weights are calibrated starting points, not ground truth.

To tune them, copy the bundled file, edit your copy, and pass its path as `config_path=` to
`analyze` (or load it with `load_config`). To inspect the defaults programmatically:

```python
from colorsense import load_default_config

config = load_default_config()
```

`config_path=` tunes the token vocabulary and the component classifier. The usage-side
role-scoring weights are documented in-code constants in
[`palette/roles.py`](src/colorsense/palette/roles.py) (e.g. `W_AREA`, `SOFTMAX_T`,
`TARGET_SPLIT`), not part of the YAML.

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
