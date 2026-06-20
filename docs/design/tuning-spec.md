# colorsense tuning spec: instance salience and detection thresholds

**Status:** tuning specification (companion to *Re-architecting colorsense: per-(color, role) detection-plus-ranking*)
**Audience:** colorsense maintainers doing the empirical calibration
**Scope:** the new tuning surface introduced by the redesign — the per-instance salience `sigma_i`, the role-level aggregation `(lambda_r, beta_r)`, and the two absolute detection thresholds `theta_noise(r)` and `theta_present(r)`. Everything here lives in Section 6 of the redesign doc; this document makes it concrete enough to implement and fit.

All quantities are unitless and **anchored to viewport fraction**, not pixels, so thresholds are resolution-independent. Document the unit convention in code; the thresholds below are meaningless without it.

---

## 0. Notation recap

For an element instance `i` carrying color `c`, classified into a component-type distribution:

```
sigma_i        per-instance salience of i toward role r
S_measured(c,r) role-level measured salience (aggregated over instances of c)
f              intent multiplier, f = 1 + alpha * q_intent(c, r),  f in [1, 1+alpha]
S_final(c,r)   = S_measured(c, r) * f
theta_noise(r)   hard, intent-independent artifact floor
theta_present(r) role-membership / reporting floor
```

Detection keeps `(c, r)` iff `S_measured >= theta_noise(r)` **and** `S_final >= theta_present(r)`.

---

## 1. Per-instance salience `sigma_i`

```
sigma_i = p_role(i, r) * pi_i
```

### 1.1 Role attribution `p_role(i, r)`

The share of instance `i`'s component-type distribution that maps to role `r` under the existing component-type -> usage-role mapping. Already computed today; reuse it. Example: a `<button>` classified `{cta_bg: 0.8, button_secondary: 0.2}` contributes `p_role = 0.8` to `cta` and `0.2` to `action`. Range `[0, 1]`.

### 1.2 Instance prominence `pi_i`

Area is the magnitude carrier; position, sibling-relative size, and contrast are **bounded modulators** that nudge but cannot manufacture prominence. A tiny element must stay low-salience even if high-contrast and centered.

```
pi_i = a_i * m_pos(i) * m_sib(i) * m_con(i)
```

| Factor | Definition | Starting function | Range |
|---|---|---|---|
| `a_i` | instance bounding-box area as fraction of viewport | raw | `(0, 1]` |
| `m_pos` | vertical position; rewards above-the-fold | `clamp(1.3 - 0.6 * y_frac, 0.7, 1.3)`, where `y_frac` = element center as fraction of **first-viewport** height (clamped to 1 below the fold) | `[0.7, 1.3]` |
| `m_sib` | size vs. sibling interactive elements | `clamp((a_i / median_sibling_area)^0.25, 0.7, 1.5)` | `[0.7, 1.5]` |
| `m_con` | contrast against `effective_bg` | `clamp(0.85 + 0.05 * (cr - 3), 0.85, 1.3)`, `cr` = WCAG contrast ratio | `[0.85, 1.3]` |

Notes:

- The `0.25` exponent on `m_sib` keeps sibling-size influence gentle; raise it only if ranking under-weights "this button is much bigger than its neighbors."
- `m_con` is deliberately centered near 1 around the AA threshold (`cr = 4.5 -> ~0.93`); it is a tiebreak-strength signal, not a primary one. Note `contrast` is already used in classification (link vs. CTA-text); here it is reused as a salience signal only.
- **For surface roles** (`page`, `surface`, `banner`), set all modulators to `1`: area *is* the prominence, and position/contrast are not meaningful for "which color covers the most screen." `pi_i = a_i`.

### 1.3 Guardrail

Keep every modulator bounded and centered near 1 so that `a_i` dominates the magnitude of `pi_i`. The explicit failure to avoid: a 16x16 px icon button being promoted to "primary CTA" because it is centered and high-contrast. With the ranges above, the maximum modulator product is `1.3 * 1.5 * 1.3 ~= 2.5`, far too small to lift a tiny element past a hero button.

---

## 2. Role-level aggregation: peak vs. aggregate

"Primary" means different things per role, so aggregation is role-parameterized. Sort a color's instances by salience descending (`sigma_(1) >= sigma_(2) >= ...`):

```
S_measured(c, r) = sigma_(1)  +  lambda_r * sum_{i >= 2} sigma_(i) ^ beta_r
```

- `sigma_(1)` is the **peak** instance.
- `lambda_r in [0, 1]` weights corroboration from additional instances.
- `beta_r in (0, 1]` makes corroboration **concave** (diminishing returns), so headcount cannot overwhelm peak prominence.

Three regimes, set by `(lambda_r, beta_r)`:

| Role | Regime | Why | `lambda_r` | `beta_r` |
|---|---|---|---|---|
| `page`, `surface`, `banner` | area-sum | the page color is whatever covers the most screen | 1.0 | 1.0 |
| `cta` | peak-dominant | the primary CTA is one prominent button, not a headcount | 0.2 | 0.5 |
| `action` | aggregate | the action color is the most-repeated secondary button color | 0.7 | 0.8 |
| `text`, `link`, `border` | aggregate | the body text/link color is the most-*used* one; a single huge headline must not outrank body text | 1.0 | 0.9 |

This is the concrete generalization of the redesign's two-way (peak vs. area) split. The `cta` peak-dominance is what solves the hero-vs-swarm case; the `text` near-sum behavior is what prevents a large hero headline from being mistaken for the primary body-text color.

Calibrate `(lambda_r, beta_r)` against ranking labels (Section 5). They are the knobs most likely to need per-corpus adjustment.

---

## 3. Intent multiplier `f`

```
f = 1 + alpha * q_intent(c, r),   q_intent in [0, 1],   f in [1, 1 + alpha]
```

`alpha` is the **only** thing `alpha` now controls: the maximum intent boost. Default `alpha = 0.4`. `q_intent(c, r)` is the matched token's usage-intent share for role `r`. No matching token -> `q_intent = 0` -> `f = 1` (neutral). Intent re-ranks and rescues at the margin; it never vetoes (a missing token is not a penalty) and never manufactures (no `S_measured` means nothing to multiply).

---

## 4. The two thresholds

### 4.1 `theta_noise(r)` — artifact floor (physical anchor, no data needed)

`theta_noise` rejects sub-perceptual slivers and classification noise. Anchor it to the `sigma` of a **reference minimum credible instance** with neutral modulators (`m = 1`), so it recomputes automatically if conventions change:

```
theta_noise(r) = p_min * a_min
```

- **Element roles:** define the smallest element worth calling a real role member, e.g. `20x20 CSS px` at a `1280x800` reference viewport and `p_min = 0.25`:
  `a_min = 400 / 1,024,000 ~= 3.9e-4`, so `theta_noise ~= 0.25 * 3.9e-4 ~= 1.0e-4`.
- **Surface roles:** reuse the existing screenshot noise floor: `theta_noise = 0.005` (0.5% area).

`theta_noise` is intentionally **declaration-independent** (no `f`). This is what keeps noise rejection equally strong on sites that declare no tokens — protecting goal 4 (undeclared colors must survive on their own merits).

### 4.2 `theta_present(r)` — role-membership floor (statistical anchor, fit from labels)

`theta_present` is the bar for "report this color as filling role `r`." Set it from a labeled corpus (Section 5): the largest value at which recall of human-labeled **must-keep** colors stays at ~100% while false positives are minimized. Sweep `theta_present` and take the knee of the recall/precision curve, per role.

Starting point before fitting: `theta_present(r) = 1.4 * theta_noise(r)` to `3 * theta_noise(r)` for element roles; `theta_present = theta_noise` for surface roles (report any surface above the noise floor).

### 4.3 How the two thresholds and `alpha` interact — the rescue band

A color is reported in two ways:

- **On measurement alone** (`f = 1`): requires `S_measured >= theta_present`.
- **Rescued by intent** (`f` up to `1 + alpha`): a color with `S_measured < theta_present` is still reported iff `S_measured * (1 + alpha) >= theta_present` and `S_measured >= theta_noise`.

So the **intent rescue band** is:

```
[ max(theta_noise, theta_present / (1 + alpha)),  theta_present )
```

Two policy regimes follow directly from where `theta_present` sits relative to `(1 + alpha) * theta_noise`:

- **Narrow gap, `theta_present <= (1 + alpha) * theta_noise`:** the band is `[theta_noise, theta_present)`. Intent can rescue *any* above-noise declared color up to the reporting bar. Friendliest to declared-but-marginal colors; undeclared colors in the band are dropped. (With `alpha = 0.4`, this requires `theta_present <= 1.4 * theta_noise`.)
- **Wide gap, `theta_present > (1 + alpha) * theta_noise`:** the band is `[theta_present / (1 + alpha), theta_present)`, sitting strictly above `theta_noise`. Real colors in `[theta_noise, theta_present / (1 + alpha))` are dropped even with full intent — a "dead band" of rendered-but-too-weak colors. Stricter reporting.

**Invariant to preserve:** the rescue band never dips below `theta_noise`. The `max(theta_noise, ...)` lower edge enforces this — intent can never rescue a color the artifact floor rejected. Verify this holds after any retune of `alpha` or the thresholds.

**Recommended policy.** Fit `theta_present` from must-keep labels so that *every* color goal 3 requires keeping clears it **on `S_measured` alone** (the gray-swarm secondary color, the heavily-used undeclared link, etc. — these are not marginal and pass easily). The rescue band is then reserved for genuinely ambiguous single-instance cases, where letting a *declared* color through and dropping an *undeclared* one is the desired asymmetry (intent as a marginal denoiser / tie-breaker, exactly as observed in past tuning).

---

## 5. Calibration procedure

1. **Build a labeled corpus.** Per page: human-labeled `(color, role)` memberships (the must-keep set) and a per-role primary ranking. A few dozen diverse pages is enough to start; over-sample busy / high-`K` pages, which is where the old pipeline failed.
2. **Compute features.** Run harvest + classification + fusion; compute `sigma_i` for every instance and `S_measured(c, r)` for every pair.
3. **Fit `sigma_i` and `(lambda_r, beta_r)` to the ranking labels.** Optimize a ranking metric (per-role NDCG and top-1 accuracy) over the modulator weights and the aggregation parameters. If labels are scarce, hand-set from the starting tables and validate; if plentiful, this is a learning-to-rank fit — prefer it.
4. **Anchor `theta_noise`** from the reference instance (Section 4.1). No fitting.
5. **Fit `theta_present`** per role from the must-keep set (Section 4.2): sweep, maximize must-keep recall at the knee against labeled-noise precision.
6. **Set the rescue-band policy** (Section 4.3): pick `theta_present` vs. `(1+alpha)*theta_noise`, then confirm declared-marginal colors are rescued and undeclared-noise is not.
7. **Regression-diff** the new view against the current pipeline on a larger unlabeled corpus. Triage divergences, with special attention to high-`K` pages (where `+1/K` erosion most distorted the old output) and undeclared-token sites (goal 4).

---

## 6. Metrics to track

| Metric | Targets which goal | Watch for |
|---|---|---|
| Per-role top-1 ranking accuracy (labeled primary ranked first) | goal 2 | the hero-vs-swarm regression |
| Must-keep recall per role | goal 3 | any present, distinct color dropped |
| False-positive rate (noise reported) | precision | `theta` set too low |
| Accuracy vs. `K` (number of colors competing in a role) | the old failure mode | degradation on busy pages |
| Undeclared-color survival rate | goal 4 | over-reliance on intent for presence |
| Rescue-band activations (declared vs. undeclared) | intent asymmetry | undeclared colors being rescued (should be rare) or noise rescued (should be zero) |

---

## 7. Guardrails — things not to do

- **Do not set `theta_present` as a relative share.** A per-role percentage reintroduces the `K`-coupling the redesign removed. Both thresholds are absolute, in `sigma` units.
- **Do not fold intent into `theta_noise`.** Noise rejection must be declaration-independent, or undeclared sites regress.
- **Do not let modulators carry magnitude.** Keep them bounded near 1; `a_i` carries prominence.
- **Recalibrate `theta_*` whenever `sigma_i` changes.** They are expressed in `sigma` units, so any change to the salience definition shifts them. Treat `sigma_i`, `(lambda_r, beta_r)`, and the thresholds as one coupled calibration, re-fit together.
- **Re-verify the rescue-band invariant** (`band lower edge >= theta_noise`) after any change to `alpha` or the thresholds.

---

## 8. Default constants (pre-fit starting values)

| Constant | Default | Source |
|---|---|---|
| `alpha` (intent boost cap) | 0.4 | carried from prior design; cap only |
| `m_pos` range | `[0.7, 1.3]` | Section 1.2 |
| `m_sib` range | `[0.7, 1.5]`, exponent 0.25 | Section 1.2 |
| `m_con` range | `[0.85, 1.3]`, slope 0.05/contrast-unit | Section 1.2 |
| `(lambda, beta)` page/surface/banner | `(1.0, 1.0)` | Section 2 |
| `(lambda, beta)` cta | `(0.2, 0.5)` | Section 2 |
| `(lambda, beta)` action | `(0.7, 0.8)` | Section 2 |
| `(lambda, beta)` text/link/border | `(1.0, 0.9)` | Section 2 |
| `theta_noise` element roles | `~1.0e-4` (`p_min=0.25`, `20x20px` @ `1280x800`) | Section 4.1 |
| `theta_noise` surface roles | `0.005` (0.5% area) | Section 4.1 |
| `theta_present` element roles | `1.4x` to `3x` `theta_noise`, then fit | Section 4.2 |
| `theta_present` surface roles | `= theta_noise` | Section 4.2 |

Every number above is a starting point for the fit in Section 5, not a tuned value. The two with real physical grounding are the `theta_noise` anchors and the modulator ranges; the rest should move under calibration.
