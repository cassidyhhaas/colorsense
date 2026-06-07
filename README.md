# colorsense

Extracts the rendered color palette from any website and returns a structured, typed
result intended for downstream consumers (including AI models) that build embedded widgets
matching the site's theme.

> **Status:** under construction. The scaffold (WP0) and frozen shared contracts (WP1) are
> in place; the analysis pipeline is being built work-package by work-package.

## Install

```bash
uv sync
uv run playwright install chromium
```

## Usage

```python
from colorsense import analyze  # available once WP11 lands

result = analyze("https://example.com")
```

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Configuration lives in `config/palette_config.yaml` — the single source of truth for the
token vocabulary and component-classifier weights. Tune values there rather than
hard-coding thresholds in Python.
