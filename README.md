# colorsense

Extract the rendered color palette of any website as a structured, typed Python object.

[![CI](https://github.com/cassidyhhaas/colorsense/actions/workflows/ci.yml/badge.svg)](https://github.com/cassidyhhaas/colorsense/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/colorsense)](https://pypi.org/project/colorsense/)
[![Python](https://img.shields.io/pypi/pyversions/colorsense)](https://pypi.org/project/colorsense/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/cassidyhhaas/colorsense/blob/main/LICENSE)

colorsense renders a page in a headless browser, harvests its design tokens and computed
element colors, and classifies them into a 60/30/10 palette — primary, secondary, accent,
and neutrals, each with ranked, scored candidates. The result is a Pydantic model, ready
for downstream consumers (including AI models) that need to understand a site's color
identity.

## Installation

```bash
pip install colorsense
playwright install chromium
```

Requires Python 3.12+. Rendering uses headless Chromium via [Playwright](https://playwright.dev/python/);
the browser binary is not a pip dependency, so run `playwright install chromium` once after
installing (on Linux, also `playwright install-deps chromium` for the OS libraries).

## Quick start

`analyze` is async — await it from an event loop, or wrap it with `asyncio.run`:

```python
import asyncio
from colorsense import analyze, PaletteRole

result = asyncio.run(analyze("https://example.com"))

for theme, palette in result.themes.items():
    candidates = palette.roles.mapping[PaletteRole.primary]
    if candidates:  # every role is present; empty tuple when none detected
        best = candidates[0]
        print(theme, best.color.hex, best.probability)
```

Each role — `primary`, `secondary`, `accent`, `neutral_light`, `neutral_dark` — maps to a
probability-ranked tuple of candidates; take `[0]` for the best pick. Inside an async
application (e.g. a FastAPI endpoint), just `result = await analyze(url)`.

See the [usage guide](https://github.com/cassidyhhaas/colorsense/blob/main/docs/usage.md) for the full result schema, options, and fetch policy.

## Features

- **60/30/10 palette classification** — five roles, each with ranked candidates carrying a
  confidence (`probability`) and page-area dominance (`area`).
- **Typed, serializable results** — `analyze` returns a frozen Pydantic `AnalysisResult`;
  `result.model_dump_json()` round-trips.
- **OKLCH out of the box** — every `Color` carries an sRGB `hex` plus cached OKLCH
  coordinates, so you can derive theme-matched tints, shades, and contrast without
  re-parsing hex strings.
- **Light and dark themes** — opt into dark mode rendering; near-identical light/dark
  renders collapse to a single reported theme.
- **Declared vs. rendered reconciliation** — reports the site's CSS custom properties and
  discrepancies between what is declared and what is actually used.
- **Status-color filtering** — success/error/warning colors are detected and kept out of
  the palette, so an error banner never masquerades as a brand accent.
- **Polite, controllable fetching** — configurable User-Agent, `robots.txt` gate with
  `Crawl-delay` support, per-host rate limiting, render caching, and a per-request egress
  filter.
- **Async-native and concurrent** — themes render concurrently in one shared browser; CPU
  work is offloaded so the event loop stays responsive.

## Security

colorsense fetches and fully renders third-party pages. If untrusted or user-supplied URLs
can reach `analyze` from a server, treat it as an SSRF surface: validate hosts before
calling, and use `PolitenessPolicy(request_filter=...)` to gate every request the rendered
page makes. The threat model and required controls are documented in
[SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) — **read it before exposing `analyze` to untrusted input.**

## Documentation

- [Usage guide](https://github.com/cassidyhhaas/colorsense/blob/main/docs/usage.md) — options, the result schema, errors, and fetch policy.
- [Advanced guide](https://github.com/cassidyhhaas/colorsense/blob/main/docs/advanced.md) — design-token auditing and custom tuning.
- [SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) — threat model and consumer responsibilities.
- [CONTRIBUTING.md](https://github.com/cassidyhhaas/colorsense/blob/main/CONTRIBUTING.md) — development setup and contribution workflow.
- [CHANGELOG.md](https://github.com/cassidyhhaas/colorsense/blob/main/CHANGELOG.md) — release history.

## Support & license

Bug reports and feature requests: [GitHub Issues](https://github.com/cassidyhhaas/colorsense/issues).
Licensed under the [MIT License](https://github.com/cassidyhhaas/colorsense/blob/main/LICENSE).
