# Real-site quality eval

This directory is colorsense's **quality** signal: it measures whether the analyzer picks
the *right* colors on real websites. It is deliberately separate from the golden snapshots
in [`tests/golden/`](../tests/golden/).

## Why this exists (and why goldens aren't enough)

The golden snapshots guard **determinism and churn** on small synthetic fixtures: they are
regenerated from the analyzer's own output (`UPDATE_GOLDEN=1`), so they can only catch a
change in behavior, never tell you whether the behavior is *correct*. A run can be perfectly
green and still pick the wrong brand color on every real site.

This eval closes that gap with two design choices:

1. **Frozen real-site harvests** (`harvests/*.json.gz`). Live rendering drifts page-to-page
   and sites redesign; pinning the harvested input isolates *code* changes from *page*
   changes (the "harvest-once / classify-many" technique) and lets the eval run offline and
   deterministically, matching the repo's network-free testing ethos.
2. **A human-reviewed ground truth** (`ground_truth.yaml`). Expected colors are sourced
   *independently* of the algorithm — from each site's own declared design tokens, confirmed
   against published brand guidelines ("tokens-primary, guidelines-confirm") — and reviewed
   by a human. That independence is what makes the score meaningful: unlike goldens, it
   cannot rubber-stamp the algorithm with its own output.

The ground truth is the **full expected shape**, not a subset of roles: each site is authored
once as a canonical **color-keyed** table (`color -> roles -> components`, mirroring the
library's color-keyed index). From that single table the scorer derives and checks BOTH views
the library emits — the role-keyed `usage` view and the color-keyed `colors` view — so it
surfaces not just "is the right color present" but also **mis-bucketing / noise** (a color
landing in a role it shouldn't, including roles that should be empty) and **component
mis-attribution**, which a role-subset ground truth is blind to.

## Usage

```bash
uv run python eval/score.py                 # full panel scorecard
uv run python eval/score.py stripe github   # named sites
uv run python eval/score.py --json          # machine-readable

uv run python eval/harvest_panel.py         # re-capture frozen harvests (needs network + Chromium)
```

`score.py` always exits 0 — it is a **report for human review on palette-affecting PRs**,
not a CI gate. Run it on `main` for a baseline and on your branch, and compare by eye. Making
it a hard CI gate would recreate the "generated to pass" trap (ΔE thresholds and "looks
right" are judgment calls), so determinism/churn stays the goldens' job and quality stays a
reviewed signal.

## Reading the scorecard

Every one of the eight roles is scored (not just those with expectations), and each predicted
color is matched to the GT within an OKLab ΔE tolerance (default 0.06, overridable per site).
Per output entry:

- `W` — **won**: this entry is an expected color *and* the role's top-ranked one.
- `+` — **present**: an expected color, but not the winner.
- `!` — **NOISE**: the entry matches no expected color for this role — a color bucketed
  where it doesn't belong (or into a role the GT says should be empty). This is the
  precision signal a role-subset GT cannot give.
- `X missing` — an expected color absent from the role's output list (the recall failure).
- `comp+=…` — the entry's component evidence includes a type the GT doesn't list for it.
- `BLEED` — a text/link/border winner equals a background-role color (a surface hex leaking
  into an element-color answer).

Below the per-role lines, **colors-index disagreements** report the same facts from the
color axis: a GT color whose `usages` roles in the color-keyed index don't match the GT
(`missing`/`extra` roles, or `absent` from the index entirely).

The headline metrics: **recall** (expected colors present), **role winners correct**,
**NOISE** (count of wrongly-bucketed entries), **component mis-attributions**, and
**family-bleed**. The aggregate covers `category: quality` sites only. `category: harvest_completeness` sites
(consent/login-walled, too few elements to analyze — e.g. `platform_disco` at ~29 elements)
are reported but excluded from the aggregate; they track the separate "thin harvest" problem.

## Maintaining the panel

- Keep `harvest_panel.py`'s `PANEL` and `ground_truth.yaml`'s `sites` in sync (same keys).
- When a site redesigns enough to invalidate its harvest, re-capture it and re-review its
  ground-truth entry — don't edit expected colors to match new output without checking the
  source, or the eval drifts back toward self-reference.
- Ground-truth values carry a `source` note; keep it accurate when you change a value.
