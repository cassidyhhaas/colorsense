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

Per role, each predicted color is matched to the expected set within an OKLab ΔE tolerance
(default 0.06, overridable per site):

- `W` — **won**: an expected color is the role's top-ranked entry.
- `.` — **present**: an expected color appears in the role's list but isn't the winner.
- `X` — **absent**: no expected color appears at all (the real failure mode).
- `!` — **bleed**: a text/link/border winner equals a background-role color (a surface hex
  leaking into an element-color answer).

The aggregate covers `category: quality` sites only. `category: harvest_completeness` sites
(consent/login-walled, too few elements to analyze — e.g. `platform_disco` at ~29 elements)
are reported but excluded from the aggregate; they track the separate "thin harvest" problem.

## Maintaining the panel

- Keep `harvest_panel.py`'s `PANEL` and `ground_truth.yaml`'s `sites` in sync (same keys).
- When a site redesigns enough to invalidate its harvest, re-capture it and re-review its
  ground-truth entry — don't edit expected colors to match new output without checking the
  source, or the eval drifts back toward self-reference.
- Ground-truth values carry a `source` note; keep it accurate when you change a value.
