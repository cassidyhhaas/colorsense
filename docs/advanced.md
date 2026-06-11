# Advanced guide

This guide covers the design-token auditing surface and tuning the classifier with a custom
configuration. For options, the result schema, and the fetch policy, see the
[usage guide](usage.md).

## Design-token auditing

Beyond the palette, `analyze` reports what a site's CSS **declares** versus what it
actually **renders** — useful for auditing a design system you own. Both surfaces live on
each theme's `ThemePalette`:

- **`palette.tokens`** — the declared design tokens (CSS custom properties) with their
  resolved color and inferred semantic role (e.g. `--accent-500` read as `brand_accent`).
  Opt-in: pass `include_tokens=True` to `analyze` (otherwise the field is `None`; `()`
  means tokens were requested but none are declared).
- **`palette.divergence`** — discrepancies between intent and usage, keyed by
  `UsageCategory`: **declared but unused** (only *high-intent* tokens — ones classified by
  an explicit name rule or relational pattern; unused shades of a numbered color scale,
  alias followers, and fallbacks deliberately do not fire) and prominent rendered colors
  that are **used but undeclared**.

```python
result = await analyze(url, include_tokens=True)
palette = result.themes[Theme.light]
for token in palette.tokens or ():
    print(token.name, token.color.hex, token.semantic_role)
for item in palette.divergence:
    print(item.category, item.note, item.color.hex)  # e.g. "declared '--brand' unused in render"
```

Each divergence item carries the affected `UsageCategory`, the `Color`, and a
human-readable `note`.

## Custom tuning

[`palette_config.yaml`](../src/colorsense/data/palette_config.yaml) ships bundled with the
package and is loaded automatically. It is the single source of truth for:

- the **token vocabulary** — CSS custom-property names → semantic roles → usage-category
  priors (how a token's color is expected to be used: surface / text / interactive /
  border);
- the **component-classifier weights** — how rendered elements are scored into headers,
  cards, CTAs, and so on. Besides the tag/class/geometry/interactivity rule lists, this
  includes two *presence* feature families keyed on structural facts about an element:
  `border_presence` (votes applied to any element that genuinely paints a border — the
  harvester width-gates the border color) and `text_presence` (votes applied to any
  non-clickable element with direct text content, so plain `<p>`/`<span>` typography is
  measured even when no semantic rule matches). Each is a single `{votes: {component:
  weight}}` mapping; the bundled YAML documents the calibration math next to each weight.

The weights are calibrated starting points, not ground truth. To tune them, copy the
bundled file, edit your copy, and pass its path as `config_path=` to `analyze` (or load it
with `load_config`). To inspect the defaults programmatically:

```python
from colorsense import load_default_config

config = load_default_config()
```

`config_path=` tunes the token vocabulary and the component classifier. The measurement-side
scoring constants are documented in-code, not part of the YAML: the usage view's pruning
threshold, component→category routing, and log-damped vote-mass prominence in
[`palette/usage.py`](../src/colorsense/palette/usage.py) (`MIN_SHARE`, `COMPONENT_USAGE`),
the per-channel perceptual join radii in
[`palette/inventory.py`](../src/colorsense/palette/inventory.py) (`DELTA_E_MATCH_BG`,
`DELTA_E_MATCH_TEXT_BORDER`, `DELTA_E_CLUSTER`),
the declared/measured token-match radii in
[`palette/reconcile.py`](../src/colorsense/palette/reconcile.py) (`DELTA_E_MATCH`,
`DELTA_E_MATCH_MEASURED`),
and the 60/30/10 role-scoring weights in
[`palette/roles.py`](../src/colorsense/palette/roles.py) (e.g. `W_AREA`, `SOFTMAX_T`,
`TARGET_SPLIT`).
