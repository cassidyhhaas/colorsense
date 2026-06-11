# colorsense

Extract the rendered color palette of any website as a structured, typed Python object.

colorsense renders a page in headless Chromium ([Playwright](https://playwright.dev/python/)),
harvests its design tokens and computed element colors, and classifies them by **usage** —
what colors paint the page's surfaces, text, interactive elements, and borders — plus a
derived 60/30/10 roles view. The usage-keyed palette is the primary result, returned as a
frozen Pydantic `AnalysisResult`.

## Requirements

- Python 3.12+
- Playwright's Chromium browser binary (installed separately, below)

## Installation

```bash
pip install colorsense
playwright install chromium
```

The browser binary is not a pip dependency, so run `playwright install chromium` once after
installing (on Linux, also `playwright install-deps chromium` for the OS libraries).

## Quick start

`analyze` is async — await it from an event loop, or wrap it with `asyncio.run`:

```python
import asyncio
from colorsense import Theme, UsageCategory, analyze

result = asyncio.run(analyze("https://example.com"))

surfaces = result.themes[Theme.light].usage.mapping[UsageCategory.surface]
print(surfaces[0].color.hex)  # ranked entries; empty tuple when none detected
```

Each usage category — `surface`, `text`, `interactive`, `border` — maps to a
probability-ranked tuple of entries; take `[0]` for the best pick. A `colorsense` command
ships too, for a first look with no code:

```bash
colorsense https://example.com
```

## Where next

- [Usage guide](usage.md) — options, the full result schema, errors, and fetch policy.
- [Advanced guide](advanced.md) — design-token auditing and custom tuning.
- [API reference](api.md) — the public API, generated from the docstrings.
- [Security](security.md) — threat model and consumer responsibilities; **read it before
  exposing `analyze` to untrusted input**.
- [Changelog](changelog.md) — release history.
