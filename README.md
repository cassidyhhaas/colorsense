# colorsense

Extract the rendered color palette of any website as a structured, typed Python object.

[![CI](https://github.com/cassidyhhaas/colorsense/actions/workflows/ci.yml/badge.svg)](https://github.com/cassidyhhaas/colorsense/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/colorsense)](https://pypi.org/project/colorsense/)
[![Python](https://img.shields.io/pypi/pyversions/colorsense)](https://pypi.org/project/colorsense/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/cassidyhhaas/colorsense/blob/main/LICENSE)

**Documentation:** [cassidyhhaas.github.io/colorsense](https://cassidyhhaas.github.io/colorsense/)

colorsense renders a page in a headless browser, harvests its design tokens and computed
element colors, and classifies them by **usage role** — page, surface, banner, cta, action,
text, link, border — across two complementary indexes: a color-keyed canonical index ("how
each color is used") and a role-keyed projection ("which colors paint each role"). The
result is a frozen Pydantic model, ready for downstream consumers (including AI models) that
need to understand a site's color identity.

```python
import asyncio
from colorsense import Theme, UsageRole, analyze

result = asyncio.run(analyze("https://example.com"))
ctas = result.themes[Theme.light].usage.mapping[UsageRole.cta]
print(ctas[0].color.hex)  # ranked entries; empty tuple when none detected
```

## Installation

```bash
pip install colorsense
playwright install chromium
```

Requires Python 3.12+. Rendering uses headless Chromium via [Playwright](https://playwright.dev/python/);
the browser binary is not a pip dependency, so run `playwright install chromium` once
after installing (on Linux, also `playwright install-deps chromium` for the OS libraries).

## Quick start

`analyze` is async — await it from an event loop, or wrap it with `asyncio.run`:

```python
import asyncio
from colorsense import analyze

result = asyncio.run(analyze("https://example.com"))

for theme, palette in result.themes.items():
    # "How is each color used?" — the canonical color-keyed index, ranked by prominence.
    for color in palette.colors:
        roles = ", ".join(f"{u.role}={u.weight:.2f}" for u in color.usages)
        print(theme, color.color.hex, roles)
    # "Which colors paint each role?" — the role-keyed projection.
    for role, entries in palette.usage.mapping.items():
        if entries:  # every role is present; empty tuple when none detected
            best = entries[0]
            print(theme, role, best.color.hex, best.probability)
```

Two complementary views describe usage. `palette.colors` is the **canonical, color-keyed
index**: each measured `ColorUsage` carries its `prominence` ranking and the `Usage` roles
it appears in (with a `property_family` rollup: background / text / border).
`palette.usage.mapping` is the **role-keyed projection**: each `UsageRole` — `page`,
`surface`, `banner`, `cta`, `action`, `text`, `link`, `border` — maps to a
probability-ranked tuple of entries; take `[0]` for the best pick. Inside an async
application (e.g. a FastAPI endpoint), just `result = await analyze(url)`.

See the [usage guide](https://github.com/cassidyhhaas/colorsense/blob/main/docs/usage.md) for the full result schema, options, and fetch policy.

### Command line

`pip install colorsense` also ships a `colorsense` command, so a first look needs no code:

```bash
colorsense https://example.com
colorsense https://example.com --dark --json > palette.json
```

The default output is a human-readable palette summary; `--json` emits the full
`AnalysisResult` schema. All flags are documented in the
[usage guide](https://github.com/cassidyhhaas/colorsense/blob/main/docs/usage.md#command-line).

## Features

- **Color-keyed index** — every measured color with the usage roles it appears in (and a
  background/text/border `property_family` rollup), ranked by an overall `prominence`.
  Answers "how is each color used?".
- **Role-keyed usage projection** — what colors paint each usage role (page, surface,
  banner, cta, action, text, link, border), each entry carrying a confidence
  (`probability`), page-area dominance (`area`), and the component types it came from
  (`components`). Splitting CTA backgrounds from link text (and the page canvas from raised
  surfaces and chrome bars) preserves structure a 4-value taxonomy lost.
- **Typed, serializable results** — `analyze` returns a frozen Pydantic `AnalysisResult`;
  `result.model_dump_json()` round-trips.
- **OKLCH out of the box** — every `Color` carries an sRGB `hex` plus cached OKLCH
  coordinates, so you can derive theme-matched tints, shades, and contrast without
  re-parsing hex strings.
- **Light and dark themes** — opt into dark mode rendering; near-identical light/dark
  renders collapse to a single reported theme.
- **Declared vs. rendered reconciliation** — declared design-token intent is pooled into
  the usage view, with per-theme `divergence` reporting high-intent tokens declared but
  unused and prominent colors used but undeclared. Opt into the token list itself with
  `analyze(url, include_tokens=True)`.
- **Status-color filtering** — success/error/warning tokens are detected and kept out of
  the palette views, so an error banner never masquerades as a brand accent (they surface
  in the opt-in token list with `semantic_role=status`).
- **Polite, controllable fetching** — configurable User-Agent, `robots.txt` gate with
  `Crawl-delay` support, per-host rate limiting, render caching, and a per-request egress
  filter that gates every browser request and the policy's own `robots.txt` fetch.
- **Server-grade guard rails** — a built-in private-network egress filter
  (`block_private_networks`) covering browser requests and the robots fetch alike, plus
  opt-in bounds on render concurrency (`max_concurrent_renders`) and total call time
  (`max_total_seconds`), and a browser launch-arg pass-through (`browser_args`) for e.g.
  capping the V8 heap per renderer.
- **Async-native and concurrent** — themes render concurrently in one shared browser; CPU
  work is offloaded so the event loop stays responsive.

## How it compares

Most palette tools quantize the pixels of an image or screenshot and return dominant
colors with no semantics. colorsense renders the live page, reads computed styles and
declared design tokens, and classifies colors by how they are used, with confidence
scores — you get "this is the CTA/link color", not "this orange is common". If you just need dominant
colors from an image, an image-quantization tool is simpler and the right choice.

## Security

colorsense fetches and fully renders third-party pages. If untrusted or user-supplied URLs
can reach `analyze` from a server, treat it as an SSRF surface: validate hosts before
calling, and use `PolitenessPolicy(request_filter=block_private_networks())` to gate every
request the rendered page makes — and the policy's own `robots.txt` fetch, including its
redirect hops; bound abuse with `max_concurrent_renders` and `max_total_seconds`. The
threat model and required controls are documented in
[SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) — **read it before exposing `analyze` to untrusted input.**

## Examples

The [`examples/`](https://github.com/cassidyhhaas/colorsense/tree/main/examples) directory has two runnable starting points:
[`quickstart.py`](https://github.com/cassidyhhaas/colorsense/blob/main/examples/quickstart.py) for trusted, hardcoded URLs, and
[`webservice/`](https://github.com/cassidyhhaas/colorsense/tree/main/examples/webservice) — a FastAPI service that is a reference
implementation of the SECURITY.md controls for untrusted, user-supplied URLs.

## Documentation

- [Documentation site](https://cassidyhhaas.github.io/colorsense/) — everything below, plus the API reference, in one place.
- [Usage guide](https://github.com/cassidyhhaas/colorsense/blob/main/docs/usage.md) — options, the result schema, errors, and fetch policy.
- [Advanced guide](https://github.com/cassidyhhaas/colorsense/blob/main/docs/advanced.md) — design-token auditing and custom tuning.
- [SECURITY.md](https://github.com/cassidyhhaas/colorsense/blob/main/SECURITY.md) — threat model and consumer responsibilities.
- [CONTRIBUTING.md](https://github.com/cassidyhhaas/colorsense/blob/main/CONTRIBUTING.md) — development setup and contribution workflow.
- [CHANGELOG.md](https://github.com/cassidyhhaas/colorsense/blob/main/CHANGELOG.md) — release history.

## Support & license

Bug reports and feature requests: [GitHub Issues](https://github.com/cassidyhhaas/colorsense/issues).
Want to contribute? Start with [CONTRIBUTING.md](https://github.com/cassidyhhaas/colorsense/blob/main/CONTRIBUTING.md).
Licensed under the [MIT License](https://github.com/cassidyhhaas/colorsense/blob/main/LICENSE).
