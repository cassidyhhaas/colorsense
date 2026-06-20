# Re-architecting colorsense: per-(color, role) detection-plus-ranking

**Status:** design proposal
**Audience:** colorsense maintainers
**Scope:** the semantic-assignment half of the pipeline — fusion, the two views, and reconciliation. Harvesting and classification change only at the margins.

---

## 1. Purpose

This document proposes a structural rewrite of how colorsense decides **which colors fill each usage role, in what order**. It follows an audit of the current pipeline and a series of design discussions. The recommendation is not a set of bug fixes; it is a change in the mathematical object the library is built around. The current defects (a `+1/K` smoothing patch, a `K`-dependent erosion of that patch, an overloaded `alpha`, two inconsistent prune sites, and a tempering exponent that prevents pruning only by accident) are all symptoms of one root choice. Fixing them individually does not help, because each is load-bearing for the representation underneath. Replacing the representation removes all of them at once.

---

## 2. What the library is actually for (goals)

These goals are stated explicitly because the correct math depends entirely on getting them right.

1. **Identify how colors are semantically used on a page.** For each usage role (`page`, `surface`, `banner`, `cta`, `action`, `text`, `link`, `border`), produce the set of colors that fill it.
2. **Rank within a role by dominance / likelihood of being primary**, *not* by raw area or raw vote count. The single large hero CTA must outrank a swarm of tiny buttons of another color, even though the swarm has more total instances.
3. **Never prune a less-prevalent but genuinely-present, visually-distinct color** out of a role merely because it is not the most prominent one in that role. This applies to every role, not just `cta`.
4. **Do not require a color to be declared** as a token to be recognized. Declared intent is corroborating evidence, not a gate. A heavily-used but undeclared color (e.g. a link color the authors never wrote as a variable) must survive.
5. **Report divergences** between measured usage and declared intent (used-but-undeclared, declared-but-unused).

Two things that are explicitly **not** goals, because they have driven incorrect design pressure in the past:

- The library does **not** identify "the brand color." Brand is a semantic that cannot be derived from on-page colors alone; not every site maps its brand to its CTA, and many use a neutral CTA. Brand tokens are matched to CTAs only because *declared brand tokens are usually used on CTAs* — the converse does not hold.
- The library does **not** try to maximize the dominant color's share. Dominant-first ordering is required; inflating the leader's probability is not.

---

## 3. The root flaw in the current architecture

The current pipeline answers an **absolute, per-color** question — "is this color genuinely present in this role?" — by routing it through a **relative, normalized, competitive** distribution: per-element softmax, then a log-linear pool of measured and declared distributions, then normalize each role's scores to sum to 1, then prune entries below a relative floor.

The moment a role's scores are normalized to sum to 1 and then thresholded, a color's survival becomes coupled to how many other colors share the role (`K`) and to their magnitudes. But "is this minor CTA color real?" has nothing to do with how many other CTA colors exist or how large they are. Normalization injects exactly the competition the goals say to avoid, and the rest of the machinery exists to fight the competition that normalization created:

- `+1/K` smoothing exists only because multiplying *normalized probabilities* manufactures a veto when intent is zero. Remove normalization and the veto never arises.
- The `(p_measured)^(1 - alpha)` exponent flattens the distribution toward uniform, which incidentally lifts minor entries above the relative prune floor. This is the *only* thing currently protecting a minor-but-present color at the reconciliation prune — and it is welded to `alpha`, so turning intent off (`alpha -> 0`) silently turns the protection off too.
- The relative prune floor and the "minimum exempt vote mass" exemption are a relative rule plus a patch to rescue the colors the relative rule should never have endangered.

The fix is not a better smoothing term. It is: **decide presence on absolute, per-color evidence; rank survivors relatively; normalize only for display.** Threshold first, normalize second — not the reverse.

---

## 4. The three questions, separated

The current design fuses three independent questions into one number. The rewrite keeps them separate and computes them in order.

1. **Presence (detection).** Does this `(color, role)` pair clear an *absolute* evidence floor? Per-pair, `K`-independent, not normalized. This is where goal 3 (never prune a present color) becomes a guarantee by construction.
2. **Ranking (salience).** Among the colors that passed presence, what is the order? Ranking is inherently relative and ordinal, so it is safe to compute among survivors.
3. **Corroboration (intent).** Does declared intent agree, disagree, or add nothing? A bounded read on the evidence terms, not something pre-multiplied into the score before presence can be seen.

---

## 5. Stage-by-stage redesign

The pipeline keeps its overall shape. The table summarizes; the subsections give detail.

| Stage | Today | After |
|---|---|---|
| 1. Harvest | 4 evidence streams | unchanged; stop discarding per-instance prominence |
| 2. Classification | tokens -> roles; elements -> component distributions | unchanged; fix `DECLARE_MIN_WEIGHT` |
| 3. Fusion | clusters with summed mass + normalized mix | canonical color identities + per-`(color, role)` evidence records |
| 4. Two views | color-keyed index + role-keyed projection (normalized) | derived from evidence records *after* detection |
| 5. Reconciliation | log-linear pooling, then ship | **removed**; jobs redistributed into detect / rank / report |

### 5.1 Stage 1 — Harvest: keep, but retain per-instance signal

The four evidence streams (declared tokens, DOM element colors, hover/focus changes, screenshot area bins) are sound and unchanged. The one required change is in *what is carried forward*. Peak-instance ranking (Section 6.2) needs the prominence of individual element instances, so the harvester must not let fusion sum them away prematurely. Each element retains the features that distinguish a prominent instance from a trivial one:

- its own bounding-box area (as a viewport fraction),
- vertical position / above-the-fold-ness,
- size relative to sibling elements,
- contrast against its `effective_bg`.

Most of these are already harvested; the change is to preserve them through fusion rather than collapsing to a single summed mass.

### 5.2 Stage 2 — Classification: keep soft labels, fix the dead knob

- **Element classification** (additive voting, then a per-family softmax, producing a per-element distribution over component types) stays. A soft label such as `{cta_bg: 0.8, button_secondary: 0.2}` is exactly the input ranking needs. The earlier concern that per-family softmax recombination can down-weight a low-mass family no longer propagates into a survival decision, because the output is now a per-element label, not a normalized role distribution.
- **Token classification** (the relational / name-rule / scale / fallback precedence ladder) stays as the producer of declared intent.
- **Fix:** `DECLARE_MIN_WEIGHT = 0.0` with a `token_weight < 0.0` test can never fire, because weights are non-negative by construction (the fallback rung emits weight 0). The operator is almost certainly meant to be `<= 0.0` (to prune zero-weight fallbacks) or a positive floor. Treat this as a defect, not a disabled knob.

### 5.3 Stage 3 — Fusion: establish identity, then accumulate evidence

Fusion keeps its essential job: using the `deltaEOK` radius joins (and the near-white / near-black handling) to group perceptually-near colors into **canonical color identities**. You cannot ask "is *this* color present in role R" until "this color" is defined, and fusion is what defines it. The grouping radii (background 0.10, text/border 0.05, cluster-merge 0.05, token-merge 0.08) stay.

> **Metric caveat (separate work item — investigated, see §9.6 item 6).** The near-extreme switch to CIEDE2000 trusts that metric in a regime where it is itself unreliable — particularly near black, where CIEDE2000 is known to over-report differences between visually indistinguishable colors. The cited near-black case (`#030711` vs `#050505`, CIEDE2000 4.33) may reflect that instability rather than a true perceptual gap. This is orthogonal to the redesign but should be revisited; do not treat CIEDE2000 as ground truth at `L <= 0.15`. **Outcome:** the 3.0 near-black threshold was found panel-neutral and kept — only the disco pair fires the check corpus-wide, every candidate metric agrees on it, and the real over-report (tailwind `#020618`/`#030712`, 3.62) is contained by the CTA/action scoping rather than the threshold. Details and the prepared chroma-plane fallback are in §9.6 item 6.

The **output** of fusion changes. Instead of one summed `component_mass` plus a normalized `component_mix` per cluster, fusion accumulates, for each **`(canonical_color, role)`** pair, an evidence record that preserves the instance distribution:

- `max_salience` — the most prominent single instance routed to this `(color, role)`;
- `corroborating_mass` — the saturating contribution of the remaining instances (Section 6.2);
- `area` — area evidence (the meaningful signal for surface roles);
- `streams` — which evidence streams contributed (used for a confidence read and for divergence logic).

This is the central data-model change. A color that serves multiple roles (a gray used as both `text` and `border`, a color used as both `cta` and `action`) now gets an independent evidence record per role as a first-class object, replacing the current "fuse clusters sharing an exact hex, then split the mix" step.

### 5.4 Stages 4-5 — detect, corroborate, rank, present

The log-linear-pooling reconciliation stage is **removed**. Its three legitimate jobs (corroborate with intent, break ties, report divergences) move into the steps below. The normalized-distribution machinery is not rebuilt.

The five steps are detailed in Section 6.

---

## 6. The core: detection-plus-ranking

All steps operate on the per-`(color, role)` evidence records from fusion.

### 6.1 Measured salience (absolute, per-pair)

For each `(color, role)`, compute one **unnormalized** score that serves as both the detection statistic and the ranking statistic. It never references any other color.

**Element roles** (`cta`, `action`, `text`, `link`, `border`) — peak-dominant with saturating corroboration:

```
S_measured(c, r) = h(max_i sigma_i)  +  lambda * sum_{i != argmax} psi(sigma_i)
```

- `sigma_i` is the salience of element instance `i`: its role-relevant component probability times its instance prominence (area, position, sibling-relative size, contrast).
- `h` is the dominant term, driven by the single most-prominent instance. This is what makes one large hero button outweigh many tiny ones.
- `psi` is concave (e.g. `sqrt`, `log1p`, or `x^beta` with `beta < 1`) and `lambda < 1`, so additional instances add *confidence* with diminishing returns rather than accumulating linearly. Headcount cannot overwhelm peak prominence.

**Surface roles** (`page`, `surface`, `banner`) — area-based, because the page color genuinely is whatever covers the most screen:

```
S_measured(c, r) = area-derived salience (peak or summed area fraction)
```

The point of two formulas is that "primary" means different things per role: most-covering for surfaces, most-prominent-single-instance for elements. This matches the existing instinct (area for surfaces, mass for elements) but replaces *summed* mass — which is linear in instance count and therefore lets a swarm of tiny buttons win — with *peak* salience.

### 6.2 Intent corroboration (bounded, non-negative multiplier)

Match declared tokens to canonical colors using the existing `deltaEOK` radius (0.10). For a match:

```
S_final(c, r) = S_measured(c, r) * f,    f = 1 + alpha * q_intent(c, r),    f in [1, 1 + alpha]
```

- `q_intent(c, r)` is the matched token's usage-intent share for role `r`, in `[0, 1]`.
- No matching token means `f = 1` — neutral, never a penalty.

Consequences:

- Intent can **re-rank** but cannot **veto** (no color is killed by the absence of a token) and cannot **manufacture** a color (a color with no `S_measured` has nothing to multiply, preserving the measured-only invariant).
- This is where the **tie-break** lives: when two colors in a role are co-dominant, the declared one's `f > 1` decides it. Because `f` is bounded, intent only matters near ties and never overrides a clear measured winner.
- `+1/K`, `epsilon`, and the `K`-dependent erosion of intent's protection all cease to exist, because the multiplicative veto they patched never arises. `alpha` now has exactly one meaning: the cap on the intent boost.

### 6.3 Detection (the survival gate)

Per `(color, role)`, keep iff **both** hold:

```
S_measured(c, r) >= theta_noise(r)     # hard, intent-independent: rejects noise and dilution artifacts
S_final(c, r)    >= theta_present(r)    # combined: intent may help a faint-but-real color clear this bar
```

Both thresholds are absolute, in salience units, calibrated to something physical per role ("at least one genuine element," "at least X% area," "mass above the measured noise level"). This gate is where goal 3 becomes a guarantee rather than an accident of `alpha`.

The two-gate structure also makes **intent-as-denoiser** safe. The hard `theta_noise` floor does the real noise rejection, independent of any declaration; intent can only lower the *second* bar, and only for a color that already carries real measurement. So sites that declare no tokens keep full noise rejection, and undeclared-but-real colors still survive.

> **Caution on the denoising benefit.** Past tuning observed that intent "helped identify present vs. noise." Be aware this may be partly because the tuning corpus declared its colors, so intent correlated with realness on those pages. Keep the denoising load on `theta_noise` (measured-only); treat intent strictly as a secondary, bounded corroborator. Do not let intent become load-bearing for noise rejection, or undeclared sites — the ones the goals care about — will regress.

### 6.4 Ranking

Within each role, sort survivors by `S_final` descending. Peak-instance salience yields dominance / primary-likelihood order; the intent multiplier settles near-ties. Goal 2 satisfied.

### 6.5 Presentation (normalize last, for display only)

Only now, if per-role shares are wanted, normalize the survivors' `S_final` to sum to 1:

```
share(c, r) = S_final(c, r) / sum_{c' in survivors(r)} S_final(c', r)
```

This is cosmetic. Detection has already happened, so normalization can no longer delete anything. Be honest about what this number is: a salience share for display, not a calibrated probability.

- **Role-keyed view:** each role -> ordered list of `(color, share)`.
- **Color-keyed index:** the same records transposed — per color, the roles it survived in with its salience. Replace the `0.7 * area_norm + 0.3 * mass_norm` blended prominence (an un-tuned scalar mashing two physically different quantities) with either the color's maximum role-salience or, better, two separately-reported fields (area and mass).

### 6.6 Divergences

Both fall straight out of the evidence terms:

- **Used-but-undeclared:** a color that survived detection with `f = 1` (no token matched) and salience above a reporting floor.
- **Declared-but-unused:** a token whose color produced no surviving `S_measured` in any role. Gate to high-intent origins (`relational`, `name_rule`) as today, to avoid flooding the report with unrendered ramp shades. (This is the path whose `DECLARE_MIN_WEIGHT` operator must be fixed, Section 5.2.)

---

## 7. What is removed, and what each thing was patching

| Removed | Why it existed | Why it is no longer needed |
|---|---|---|
| Log-linear pooling stage | combine measured + intent into one normalized distribution | intent is now a bounded multiplier applied per-pair; no separate stage |
| `+1/K` smoothing | prevent a zero-intent multiplicative veto | no veto: missing intent gives `f = 1` |
| `epsilon = 1e-9` floor | prevent a literal zero in the measured factor | no normalized multiplication to protect |
| `(p_measured)^(1 - alpha)` tempering | (accidentally) lift minor colors above the relative prune floor | presence is decided on absolute evidence; nothing to lift |
| relative prune floor + min-exempt-mass exemption | prune on share, then rescue real accents | absolute-evidence detection is the primary rule; no exemption needed |
| blended `0.7/0.3` prominence | single ranking scalar | per-role salience + separately-reported area/mass |

`alpha` survives with a single, clean meaning. Fusion radii and color-identity machinery survive. The rewrite removes complexity rather than adding it: it deletes the patches and the one stage whose job was to undo its own normalization.

---

## 8. Worked example: hero CTA vs. a swarm of small buttons

A page has one large green hero CTA button and forty tiny gray buttons of a single secondary color.

- **Green, `cta`:** one instance, large area, top-of-page, high `cta_bg` probability -> large `max_i sigma_i` -> high `S_measured(green, cta)`.
- **Gray, `cta`:** forty instances, each tiny and low-prominence -> small `max_i sigma_i`; count enters only through the saturating `psi` -> modest `S_measured(gray, cta)` despite the headcount.
- **Detection:** both clear `theta_noise` and `theta_present`, so **both survive** — the secondary gray action color is not pruned (goal 3).
- **Ranking:** `S_final(green, cta) >> S_final(gray, cta)`, so green ranks first (goal 2). Headcount did not decide the order; peak salience did.
- **Intent:** if green is declared `--brand-primary`, its `f > 1` is irrelevant here — it is already first. The boost would only earn its keep if a second color were neck-and-neck, which is exactly the tie-break case.

The failure mode (the swarm outranking the hero) cannot occur, and no role-wide normalization was ever consulted to prevent it.

---

## 9. Migration and open items

1. **Data model first.** Land the per-`(color, role)` evidence record in fusion (Section 5.3) before touching the views. Everything downstream reads it.
2. **Keep both views running in parallel during migration.** Emit the new detection/ranking view alongside the current reconciled view and diff them on a corpus to catch regressions, especially on busy pages (high `K`), where the old `+1/K` erosion most distorted results.
3. **Calibrate the absolute thresholds.** `theta_noise(r)` and `theta_present(r)` are the new tuning surface. Anchor them to physical quantities (minimum element count / area / mass above noise) rather than to relative shares. This is where most of the empirical work now lives.
4. **Decide the per-instance prominence weighting** (`sigma_i`): how to combine area, position, sibling-relative size, and contrast. If labeled "primary CTA" data exists or can be collected, this and the ranking are a textbook learning-to-rank problem and should be fit, not hand-weighted. Without labels, a peak-dominant monotone score is an acceptable proxy *provided it stays peak-dominant, not sum-dominant.*
   - **Status (done for the aggregation).** `eval/fit_aggregation.py` fits the role-level `(lambda_r, beta_r)` against the labeled must-keep set — on threshold-free NDCG/top-1 ranking quality, with `theta_present` re-fit jointly (Section 7 coupling) and `theta_noise` held fixed. Key subtlety: `sigma_i` are area-fractions `< 1`, so `x^beta` with `beta < 1` *inflates* the tail (`sqrt(1e-4)=1e-2`) — the saturating end of the `beta in (0,1]` box is therefore `beta -> 1`, the fit's tie-breaker. The element roles land at `beta in {0.5, 0.7, 0.8}` (the `text`/`link`/`border` `0.5` is a genuine ranking win: inflating the headcount tail *is* "most-USED color wins"), lifting role winners 74→75 and dropping NOISE 74→69 at held recall.
   - **Finding: the recall regressions are not aggregation-bound.** The fit proved `(lambda_r, beta_r)` cannot move recall — every miss is either *fusion-bound* (no evidence record in the target role: a near-white/near-black cluster merge per item 6, or a mis-classification) or a *single instance below `theta_noise`* (no corroboration tail to reshape, and `theta_noise` is the anchor). The `surface` role's ~70% must-keep recall is thus an inventory / `theta_noise` limitation, not an aggregation one — it belongs with items 3 and 6, not 4.
5. **Fix `DECLARE_MIN_WEIGHT`** (Section 5.2) independently; it is a small, safe change.
6. **Revisit the near-black CIEDE2000 trust** (Section 5.3 caveat) as a separate metric work item. *(This is the lever for the fusion-bound `surface` recall above: the near-white/near-black merge guards collapse distinct dark/light surfaces into one canonical identity, so they never reach the detection stage as separate colors.)*
   - **Status (investigated; no change warranted).** The near-black CTA/action distinctness check (`inventory._is_distinct_near_black_pair`, `NEAR_BLACK_MERGE_MAX_DE2000 = 3.0`) was re-validated on the frozen panel. It is **metric-neutral**: disco's `#030711`/`#050505` is the only near-black pair on the whole corpus that carries CTA/action mass and so actually fires the check, and a raised ΔE2000 threshold, an OKLab-chroma-plane test, and a lightness-Δ-capped split all reproduce the *identical* panel score (recall 218/232, winners 75/77, noise 69; disco recovers `#030711`). So there is no empirical basis to swap the metric, and 3.0 has corpus margin (real should-merge anti-alias pairs ≤ ~2.19 ΔE2000; disco split 4.33). The caveat is nonetheless real and now has a concrete corpus instance — tailwind's `#020618`/`#030712` (one navy surface, OKLab 0.015, equal lightness) over-reports at 3.62 > 3.0 — but it is contained by the **CTA/action scoping**, not the threshold value (that pair carries no CTA mass, so it merges anyway). The prepared fix, if a future page ever puts a CTA on such a navy surface, is an OKLab-chroma-plane distinctness test that splits only on a genuine tint difference. Pinned by `tests/test_palette_inventory.py` (`test_near_black_guard_keeps_corpus_anti_alias_pairs_merged` and the tailwind over-report tests).
   - **Note on `surface` recall.** This check does *not* move the fusion-bound `surface` misses (disco `#050505`, tailwind `#030712`): the near-black guard is deliberately CTA/action-scoped (a global page/surface near-black split was measured at +25 noise), so those surface colors are an inventory / classification placement issue, not one this metric can reach.

---

## 10. Summary

The library has the right ingredients — multi-stream evidence fusion, soft component labels, intent as corroboration, per-role metric selection — assembled around the wrong mathematical object. Presence in a role is an absolute, per-color detection decision; the current design treats it as a share of a normalized, competitive distribution, and every specific defect follows from that. Separate the three questions — **detect on absolute evidence, rank survivors relatively, fold intent in as a bounded multiplier, normalize only for display** — and the patches become unnecessary, the goals become guarantees rather than emergent accidents, and the one knob left (`alpha`) means exactly one thing.
