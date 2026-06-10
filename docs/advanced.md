# Advanced guide

This guide covers the design-token auditing surface and tuning the classifier with a custom
configuration. For options, the result schema, and the fetch policy, see the
[usage guide](usage.md).

## Design-token auditing

Beyond the palette, `analyze` reports what a site's CSS **declares** versus what it
actually **renders** — useful for auditing a design system you own:

- **`result.tokens`** — the declared design tokens (CSS custom properties) with their
  inferred semantic roles (e.g. `--accent-500` read as `brand_accent`), for the primary
  theme.
- **`result.divergence`** — discrepancies between intent and usage: brand colors
  **declared but unused** in the render, and prominent rendered colors that are **used but
  undeclared**.

```python
for item in result.divergence:
    print(item.note, item.color.hex)  # e.g. "declared '--brand' unused in render"
```

Each divergence item carries the affected `PaletteRole`, the `Color`, and a human-readable
`note`.

## Custom tuning

[`palette_config.yaml`](../src/colorsense/data/palette_config.yaml) ships bundled with the
package and is loaded automatically. It is the single source of truth for:

- the **token vocabulary** — CSS custom-property names → semantic roles → 60/30/10
  palette-role priors;
- the **component-classifier weights** — how rendered elements are scored into headers,
  cards, CTAs, and so on.

The weights are calibrated starting points, not ground truth. To tune them, copy the
bundled file, edit your copy, and pass its path as `config_path=` to `analyze` (or load it
with `load_config`). To inspect the defaults programmatically:

```python
from colorsense import load_default_config

config = load_default_config()
```

`config_path=` tunes the token vocabulary and the component classifier. The usage-side
role-scoring weights are documented in-code constants in
[`palette/roles.py`](../src/colorsense/palette/roles.py) (e.g. `W_AREA`, `SOFTMAX_T`,
`TARGET_SPLIT`), not part of the YAML.
