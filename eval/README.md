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
   *independently* of the algorithm — but from the **page itself** (the harvest's declared
   design-token values and the measured computed color of every element), reviewed by a human.
   That independence is what makes the score meaningful: unlike goldens, it cannot rubber-stamp
   the algorithm with its own output. Crucially it is *page*-sourced, not *brand*-sourced: it
   records what the page actually paints and how, never what a brand "should" use. A color a
   brand declares but the page does not render in a role is not in the GT — holding the
   algorithm (which sees only the page) to brand knowledge it can't see would make the eval
   wrong, not the algorithm. Published guidelines may *label* a color already on the page, never
   add one. There is no primary/secondary/accent notion here, by design.

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

uv run python eval/probe.py github '#08872b'  # authoring aid: is this color real? (see below)
uv run python eval/harvest_panel.py         # re-capture frozen harvests (needs network + Chromium)

uv run python eval/calibrate_thresholds.py  # sweep theta_present per role; report the knee
uv run python eval/fit_aggregation.py       # learning-to-rank fit of per-role (lambda, beta)
uv run python eval/fit_aggregation.py --tail-report   # + the beta tail-inflation diagnostic
```

`calibrate_thresholds.py` and `fit_aggregation.py` are **in-sample** calibration harnesses over
the 10 quality sites (the goldens regression + `score.py` are the out-of-sample check). The first
fits `theta_present` at fixed aggregation; the second fits the aggregation `(lambda, beta)` on
ranking quality and re-fits `theta_present` jointly, holding `theta_noise` as the physical anchor.

`score.py` always exits 0 — it is a **report for human review on palette-affecting PRs**,
not a CI gate. Run it on `main` for a baseline and on your branch, and compare by eye. Making
it a hard CI gate would recreate the "generated to pass" trap (ΔE thresholds and "looks
right" are judgment calls), so determinism/churn stays the goldens' job and quality stays a
reviewed signal.

## Reading the scorecard

Every one of the eight roles is scored (not just those with expectations), and each predicted
color is matched to the GT within a perceptual **ΔE2000** tolerance (default 1.0, overridable
per site — see `colormetric.py` for why CIEDE2000 rather than OKLab, and the cross-OS jitter
measurement behind the 1.0).
Per output entry:

- `W` — **won**: this entry is an expected color *and* the role's top-ranked one.
- `+` — **present**: an expected color, but not the winner.
- `!` — **NOISE**: the entry matches no expected color for this role — a color bucketed
  where it doesn't belong (or into a role the GT says should be empty). This is the
  precision signal a role-subset GT cannot give.
- `X missing` — an expected color absent from the role's output list (the recall failure).
  Recall is computed per *expected* color (does any entry match it?), so it is overlap-safe
  and independent of the order colors are listed in the YAML.
- `comp+=…` — the entry's component evidence includes a type the GT doesn't list for it
  (an unscored diagnostic — see below).
- `BLEED` — a text/link/border *winner* equals a known background color that the GT does **not**
  also sanction for that element role (a surface hex leaking into an element answer). A neutral
  the GT legitimately lists as both text and a dark surface is not a bleed.

Below the per-role lines, **colors-index disagreements** report the same facts from the color
axis (a GT color whose `usages` roles in the color-keyed index don't match the GT).

The headline metrics: **recall** (expected colors present), **role winners correct** (the top
entry of a role is an expected color — a page whose CTAs are white correctly "wins" `cta` with
white; there is no notion of a brand color that "should" lead), **NOISE** (wrongly-bucketed
entries), and **family-bleed**. Component mismatches are printed as an *unscored diagnostic*:
the expected component lists can only come from the algorithm's own output, so scoring them
would be self-referential. The aggregate covers `category: quality` sites only; `harvest_completeness` sites
(consent/login-walled, **< 100 elements** — e.g. `platform_disco` at ~29) are reported but
excluded. That threshold is computed from the harvest, so a large site cannot be hand-tagged
`harvest_completeness` to dodge a bad aggregate (the scorer rejects the mis-tag at load).

## Authoring ground truth (how to stay independent)

The whole value of this eval is that the ground truth is sourced *independently* of the
algorithm but entirely from the **page** — never from off-page brand knowledge the algorithm
can't see. To keep authoring from collapsing back into "copy the output," the rules are
operationalized in [`probe.py`](probe.py) — run `uv run python eval/probe.py <site> '#hex' …`
and it computes, from the frozen harvest, the evidence for each candidate color:

- **Real vs phantom.** How many *elements* paint the color on each channel (bg/text/border).
  Zero elements does **not** automatically mean "omit": the probe also reports the color's
  screenshot **area** and whether it sits between two real colors (a quantizer **blend** =
  phantom). A zero-element color with real area that is *not* a blend is an `AREA-ONLY` color
  (gradient/background-image/body bg) — keep it once you've confirmed it's a real rendered
  surface (not an artifact). Omitting these silently is the failure mode that would make the
  eval drive the algorithm to *delete* real colors.
- **Real use for cta/action/link.** The probe reports the **clickable** share of the painting
  elements; a single non-clickable status dot does not earn a CTA role.
- **One resolution.** The probe matches at the same tolerance the scorer uses, so what you author
  is what gets scored. Two same-role colors closer than the tolerance are one color (the scorer
  rejects the YAML otherwise).
- **Role and hex both come from the page.** Which elements paint the color (and whether they're
  clickable) decides the role; the rendered cluster color is the hex. A declared token *name*
  may help label the role, but actual element usage is the truth — a token a brand declares but
  the page does not paint earns no role. Published brand guidelines may only help *label* a
  color already on the page; they never add or require one that isn't there.

Record where each value came from in the `source:` note. The component lists beside each role
are informational (unscored), so author them from the dump for documentation, not for scoring.

## Maintaining the panel

- Keep `harvest_panel.py`'s `PANEL` and `ground_truth.yaml`'s `sites` in sync (same keys).
- When a site redesigns enough to invalidate its harvest, re-capture it and re-review its
  ground-truth entry — don't edit expected colors to match new output without checking the
  source, or the eval drifts back toward self-reference.
- Ground-truth values carry a `source` note; keep it accurate when you change a value.
