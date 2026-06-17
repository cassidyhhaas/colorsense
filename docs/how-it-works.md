# How it works

This page walks through the full analysis pipeline, step by step, with the actual logic
and calculations spelled out. It assumes no familiarity with the codebase — just Python
and a rough idea of how web pages are built.

The one-line version: `analyze(url)` renders the page in headless Chromium, **harvests**
four kinds of raw evidence from it, **classifies** the declared design tokens and the
visible DOM elements, fuses everything into a perceptually clustered **color inventory**,
builds the **color-keyed index** ("how each color is used") and the **role-keyed usage
projection** ("which colors paint each role" — page, surface, banner, cta, action, text,
link, border), and **reconciles** the latter against what the site's CSS declared.
Everything after the harvest is deterministic, pure CPU work.

Perceptual color distance appears throughout. It is always the same function: Euclidean
distance in the OKLab color space ("ΔE", `deltaEOK`), whose units are small — in this
codebase 0.05 is the radius at which two colors are treated as near-identical, and 0.10
is a deliberately generous "same paint" radius. Every threshold below is tuned to that
scale.

## 1. Rendering and harvesting

A render opens a fresh browser context at a fixed viewport (1280×800 by default) with the
requested `prefers-color-scheme` (light by default), navigates, waits for the `load`
event plus a short, capped network-idle wait, injects CSS that disables all transitions
and animations (so computed colors are stable, not mid-fade — best-effort: the injection
is retried once and, if it still fails, skipped with a `RuntimeWarning` rather than
failing the render, since on busy pages Playwright can spuriously reject it over an
unrelated CSP-violation console error), step-scrolls the full page
height (up to 20 viewport-steps) to trigger lazily loaded content, and detects
cookie-consent / overlay banners so they can be masked out later.

From that one live page, four harvests run:

**Visible DOM elements** (`harvest/dom.py`). An in-page script walks every element and
records its computed `background-color`, `color`, and `border-color`, its bounding
rectangle, CSS `position`, and its smallest corner radius (`min_corner_radius`), its
tag / ARIA role / id / class tokens, and structural flags: is it clickable, does it have
a box shadow, does it have *direct* text content (descendant text deliberately doesn't
count, or every wrapper of any text would carry the flag), is it an
iframe / cross-origin / shadow host / known third-party vendor widget.
Hidden, zero-area, and `aria-hidden` elements are excluded. One subtlety: a border color
is only reported when the element actually paints a border (`border-top-width > 0`) —
the computed border color resolves for *every* element regardless of width, so an ungated
read would report a meaningless (usually black) "border color" on virtually everything.
A related subtlety covers gradient buttons: a call-to-action painted with a CSS gradient
(`background-image: linear-gradient(...)`) has a *transparent* computed `background-color`,
so reading only that would miss its brand colors. When a clickable, pill-shaped element's
only fill is such a gradient, the script also records the gradient's color stops — the
button's real brand colors — while leaving out decorative gradient panels, non-clickable
dividers, and any gradient that fades to a fully-transparent stop (glows and halos always
do), so none of them masquerade as buttons.

**Declared design tokens** (`harvest/tokens.py`). The CSSOM is enumerated for CSS custom
properties (`--*`) across all same-origin stylesheets (cross-origin sheets throw on
access and are skipped), recursing into `@media` rules. Each declaration is captured with
its raw value, its scope selector, and the value resolved against the rendered `:root`,
parsed into a color when possible. When a token's raw value is a `var(--x)` reference,
that alias edge is recorded — the classifier uses it later.

**Hover/focus probes** (`harvest/states.py`). For up to 80 clickable elements, the
`:hover`/`:focus` pseudo-classes are *forced* through the Chrome DevTools Protocol
(`CSS.forcePseudoState`) and the computed background re-read; if it changed, the element
is marked as having a hover color change. Forcing the pseudo-state instead of moving a
real mouse is roughly 5–75× faster on real pages (no actionability checks, no menus
accidentally opened that block the next hover) and works even when the stylesheet is
cross-origin; the one thing it cannot see is purely JS-driven hover (a class toggled on
`mouseenter`).

**Screenshot quantization** (`harvest/screenshot.py`). A full-page screenshot is taken
(as high-quality JPEG — the image is about to be downscaled and quantized anyway, so
PNG's lossless fidelity would be thrown away; capture dimensions are capped so a
pathologically tall or wide page is clipped rather than decoded into gigabytes). The
detected consent-banner rectangles — together with the bounding boxes of raster
photographic content (`<img>`, `<video>`, `<canvas>`, `<picture>`, and elements with a
`url(...)` background image) — are zeroed out of a boolean keep-mask, so neither a cookie
banner nor a product photo can pollute the palette. CSS gradients and inline `<svg>` are
deliberately left in: a gradient is brand design, and a logo is usually vector. This mask is
a best-effort *quality* filter, not a guarantee: it catches the ordinary ways a page shows a
photo, but obscure techniques (a raster painted through `border-image`/`mask-image`, a photo
embedded inside an inline `<svg>`, or one tiled out of many solid-color elements) can slip
past it. The cost of a miss is only a slightly noisier palette — the same result you would
get without the mask — never a broken or unsafe one. The image is then
downscaled to at most 256 px on its
longest edge (nearest-neighbor, which keeps colors crisp rather than blending them) and
quantized with Pillow's median-cut algorithm into at most 16 palette buckets. Each bucket
becomes a `ScreenshotBin`: a color plus the fraction of sampled (non-masked) pixels it
covers. Bins under 0.5% of sampled pixels are dropped as noise. These `area_fraction`s
are the pipeline's ground truth for "how much of the page is this color".

When both light and dark themes are requested, each theme gets its own render. If the
site ignores `prefers-color-scheme`, the two renders come back near-identical; they are
detected by comparing the top 4 screenshot bins of each render — if every dominant bin in
one has a perceptual match within ΔE 0.06 in the other, *symmetrically*, the themes
collapse and only the primary one is reported.

## 2. Classifying tokens and components

Two classifiers run on the harvest. All of their weights, vocabularies, and priors live
in one YAML file (`src/colorsense/data/palette_config.yaml`) — nothing is hard-coded in
the classifier code.

### Token classification (`classify/tokens.py`)

Each declared token name is matched in strict precedence order:

1. **Relational** — names like `--on-primary` or `--card-foreground` match a relational
   pattern: the token is a *text* color paired with a base surface, classified `text_on`.
2. **Name rule** — a direct vocabulary match on the namespace-stripped name
   (`--bs-primary` → `primary` → `brand_primary`; `--border` → `border`; `--gray` →
   `neutral`; and so on), each rule carrying a weight.
3. **Scale detection** — a numbered family like `blue-500` or `gray-100`: chromatic
   families classify as brand/accent (with a confidence boost for "anchor" steps like
   Tailwind's 500–700), neutral-named families as neutral.
4. **Fallback** — `ignore`, weight zero.

A final pass lets a token that fell through to `ignore` *inherit* the classification of
the token its `var(--x)` value points at, following the alias chain transitively (with
cycle protection). Each classified token records which path produced it — its **origin**
(`relational`, `name_rule`, `scale`, `alias`, or `fallback`) — which matters later:
reconciliation treats only `relational` and `name_rule` classifications as direct
evidence of author intent.

The classified role is then mapped (via the YAML's `semantic_role_to_usage_intent_or_channel` table) to its
usage intent — a distribution over the eight usage roles expressing where the color is
expected to be used, inferred from the token's name before the page is measured. E.g.
`brand_accent` leans cta/link/action (its old "interactive" mass, now split across those
three roles), while `neutral` spreads across the background roles (page/surface/banner),
text, and border. The distributions were derived from the previous 4-category table by
splitting each old category's
mass across the roles it became (old `surface` → page/surface/banner, old `interactive` →
cta/link/action, `text` → text, `border` → border), weighted toward the most-likely role
per semantic.

### Component classification (`classify/components.py`)

Each harvested element is scored into a probability distribution over component types
(`page_bg`, `header_bg`, `card_bg`, `cta_bg`, `link`, `border`, `page_text`, …,
`third_party`). The scoring is additive voting across eight feature families — semantic
tags and ARIA roles, geometry (a full-width element near the top of the viewport votes
`header_bg`; a fully-rounded, short, text-bearing pill that paints a fill votes `badge`, as
does a small clickable circular chip that recurs as a structurally-similar group — an
icon-only corner badge), class/id token
substrings (`"navbar"` votes `nav_bg`), interactivity, border presence, text presence,
repetition (three or more siblings sharing a tag and class token, each with a
shadow/border/background, vote `card_bg` — the card detector, which skips pill shapes *and*
small circles so repeated chips and dots aren't read as tiny cards), and third-party origin
signals. One fallback runs before the families: on sites whose `<html>`/`<body>`/`<main>`
all paint no opaque background (a common utility-CSS pattern), the largest viewport-spanning
opaque element near the top of the page *whose color matches the independently-derived page
color* is taken as the page canvas and votes `page_bg`, so the page color still surfaces in
the `page` role (the color match keeps a brand-colored hero from being mistaken for the
canvas). Then
multiplicative suppressors apply (`aria-hidden` and hidden elements are zeroed;
brand-component votes on third-party widgets are damped to 5%), and the surviving positive
votes become the element's probability distribution **one color channel at a time**. An
element can paint up to three colors — its text, its background, and its border — so the
votes are first partitioned by the channel each component is measured from: `*_text`
components and `link` are text-channel votes (a link paints its typography, not its usually
transparent background), `border` is the border channel, and everything else is a background
vote. Within each painted channel the votes go through a softmax at temperature 0.5, entries
below probability 0.05 are pruned, and the survivors are renormalized; the per-channel
results are then combined, each channel weighted by its share of the element's total vote
mass. Normalizing per channel keeps an element's own colors from competing with each other:
a filled, clickable button's strong background (interactive) vote no longer crowds out the
smaller evidence for its border or text color, so colors a single shared softmax would have
starved still reach the palette.

Two of those families are worth working through, because their single vote weight sets how
large a share of an element's evidence a secondary channel claims (the YAML keeps a short
note next to each weight; the full derivation lives here):

**Border presence** — any element that genuinely paints a border gets a `border: 2.5`
vote. Because the border is its own channel, it is never crushed by a strong background vote
the way it would be in a single shared pool; the 2.5 instead sets how large a share of the
element's evidence its border color claims, since each channel is weighted by its vote mass:

- A bordered card (`card_bg: 3.0` from its class token) splits its evidence between a
  background channel carrying 3.0 and a border channel carrying 2.5, so the border keeps
  about `2.5 / (3.0 + 2.5) ≈ 0.45` of the distribution — comfortably measured. Same split
  for a bordered text input (`input_bg: 3.0`).
- A bordered *submit* input paints three channels at once. Its background channel carries
  `cta_bg: 7.0` (semantic `input[submit]` 3.5 + clickable 1.5 + the `input[submit|button]`
  interactivity vote 2.0) **plus** `input_bg: 3.0` — every `<input>` also matches the
  bare-tag rule — for 10.0; the `clickable` rule additionally casts `link: 1.0` on the text
  channel; and the border adds 2.5. Against the element's total evidence of 13.5 the
  interactive background dominates at `10.0 / 13.5 ≈ 0.74`, as it should, while the border
  still keeps a measured `2.5 / 13.5 ≈ 0.19` (and the text channel a slim `1.0 / 13.5 ≈
  0.07`) instead of vanishing. A bordered CTA button, whose class tokens push its `cta_bg`
  higher still, tilts further toward interactive in the same way.

So the single 2.5 weight keeps a painted border measurable wherever it appears, while a
strongly interactive element still keeps the bulk of its evidence on its interactive color
— the border is attributed in proportion to how much of the element it is, not
all-or-nothing. This family exists because of
a real failure: with only the `<input>` rule voting `border`, pages without classified
inputs measured zero border mass anywhere (github.com's `#d1d9e0` borders were simply
absent from the result, while never-rendered border *tokens* flooded the category — see
§5).

**Text presence** — any *non-clickable* element with direct text content gets a
`page_text: 2.0` vote. A bare `<p>` with no other votes gets `page_text` probability 1.0;
a text-bearing card splits its evidence between a background channel (`card_bg: 3.0`) and a
text channel (`page_text: 2.0`), so the text color keeps `2.0 / (3.0 + 2.0) = 0.40` —
measured, without displacing `card_bg`. The 2.0 deliberately stays below every semantic
`*_text` vote (e.g. `body`'s `page_text: 4.0`), so semantic rules dominate the text channel
where they apply. Clickable elements are excluded on purpose: their typography is
interactive by definition and already routed through the link rules — letting them vote
`page_text` would leak link colors into the text category. This family also fixed a real
gap: typography in plain `<p>`/`<span>` content was previously never measured
(github.com's muted `#59636e` was absent from `usage.text`).

**CTA-label contrast relabel** — the generic `clickable` rule casts a `link: 1.0` vote on
*every* clickable, so a button's text color lands in the `link` role. For a genuine inline
anchor that is correct; for a button *label* — the `<span>`/`<svg>`/`<div>` descendants of a
CTA that carry the button's text color — it is noise (vercel.com's white button labels showed
up as a white "link"). Anchors are already split by the `a & button_surface` rule, but the
non-anchor descendants are not. The distinguishing signal is *theme/contrast-relative*: a
genuine inline link's text is legible against the page's own reading surface, whereas a CTA
label's text is legible only against the button it sits on. The harvester records each
element's composited **effective background** (the first fully-opaque background up its
ancestor chain) and whether that background is itself clickable; the classifier then relabels
a non-anchor clickable's `link` mass to `cta_text` (the unrouted button-label sink) when its
text sits on a *distinct interactive fill* (effective background from a clickable ancestor,
perceptually apart from the page canvas — a color-identity test, so CIEDE2000, which stays
accurate near the white/black where canvases live, unlike OKLab ΔE), is **legible on that
fill** (WCAG contrast ≥ 4.5),
and is **illegible on the page canvas** (contrast < 4.5). Each clause guards a real case: the
"from clickable" test keeps a link inside a passive dark hero; the legible-on-fill test keeps
a brand-colored link on a soft tinted card (stripe.com's orange `#ff6118`, contrast ~2.4 on
its peach fill — decorative styling, not a readable label); the illegible-on-canvas test keeps
dark text that would also read as ordinary body text. The relabel *moves* the vote within the
text channel rather than deleting it, so the per-channel recombination weights are unchanged
and no mass shifts onto the background channel.

## 3. Building the color inventory

`palette/inventory.py` fuses the screenshot's *area truth* with the elements' *semantic
truth* into `ColorCluster`s. The work happens in **three separate pools, one per property
family** — background, text, and border — and a color never crosses between them:

1. **Seed** the *background* pool with one working entry per screenshot bin, carrying its
   authoritative `area_fraction` and an empty component mix. The *text* and *border* pools
   start empty: text and border colors paint no screenshot area, so there is nothing to
   seed them with.
2. **Attribute element semantics.** Each classified element's component distribution is
   split by *color channel*: `*_text` components and `link` route to the element's text
   color (a link paints its typography, not its usually-transparent background), `border`
   to its border color, everything else to its background color. Each channel's votes land
   in **that channel's family pool only**. A channel whose measured
   color is fully transparent (`alpha == 0` — e.g. the default
   `background-color: transparent`) paints nothing and donates no votes; without that
   gate, every transparent-background element would pile votes onto a phantom black
   zero-area cluster. The background channel can carry more than one fill: when a clickable
   pill's background is a gradient, every harvested stop is attributed, the channel's vote
   mass split evenly across them, so a two-stop button donates the same total background
   evidence as a solid one. (These stops are opaque by construction — a gradient with any
   fully-transparent stop is treated as decorative and dropped back at harvest.)
   Independently, the background channel's vote mass is scaled by each fill's *opacity*,
   which is what matters for a translucent *solid* background: a faint tint such as
   `bg-primary/10` votes its intended saturated color in proportion to how little it paints,
   rather than at full strength.
   Each fill's vote mass is added to the nearest existing entry **in that channel's family
   pool** within the channel's **join radius** — or, if nothing is close enough, a new entry
   with `area_weight = 0` is created in that pool so the semantics aren't lost.
3. **Cluster each pool independently.** Within a pool, entries within ΔE 0.05 of each other
   are merged transitively (union-find: if A is near B and B is near C, all three become one
   cluster). Each group becomes one `ColorCluster`. The representative color is chosen by
   what is authoritative for that family: **background** picks the largest *area weight*;
   **text** and **border** pick the largest *in-family vote mass* (they paint no screenshot
   area), ties broken by hex. `area_weight` is the group sum (zero for text/border), and the
   component votes are kept both raw (`component_mass` — cross-cluster magnitudes matter
   later) and normalized (`component_mix`). The three pools' clusters are returned as one
   flat list (each cluster's `component_mass` holds only its own family's components, so the
   downstream usage/reconcile stages need no family bookkeeping).

Segregating the pools is what stops a low-area text or border color from being swallowed by a
high-area background bin of a near-identical hue and then *reported as the bin's hex* — the
family-bleed that put a page-background color in the `border` slot, or a card surface in the
`link` slot. Because text and border colors now cluster only against their own kind, the
representative reported for each role is a real color of that family.

### Why two join radii?

The bg channel joins at **0.10**, the text and border channels at the tighter **0.05**,
and the two limits guard against opposite failure modes:

- **Backgrounds match loosely (0.10).** The screenshot side of the join is a quantizer
  bin: median-cut quantization, downscaling, and anti-aliasing all smear large surfaces,
  so the bin color can sit a noticeable distance from the exact computed background. A
  generous radius is what ties element backgrounds back to their area-truth bins at all.
- **Text and borders match tightly (0.05).** These come from computed style — exact
  values, not quantized pixels — and dark colors sit perceptually close together in
  OKLab, so a loose radius would fold a page's near-black text into adjacent dark
  *text* colors and blur its text hierarchy. The tight radius bounds that absorption; it
  does not eliminate it. Two genuinely-near colors *of the same family* still merge — a
  `#1f2328` and a `#002a36` body text sitting at ΔE ≈ 0.041 become one text cluster — a
  known limitation of clustering in OKLab. (A near-black *text* color and a near-black
  *surface* no longer merge, though: they live in different family pools.)

#### The near-white guard

OKLab ΔE is materially *non-uniform* near the lightness extremes: up at white, the 0.05
radius balloons to ~6.5–8.5 in the perceptually-uniform CIEDE2000 units. So the tight 0.05
join silently swallows clearly-distinct near-white text colors. The canonical case is
GitHub's logged-out homepage, whose dominant body text is pure `#ffffff` while Primer's
`--fgColor-default` paints a near-white `#f0f6fc`: OKLab puts them ΔE 0.031 apart (they
merge), but CIEDE2000 puts them at 4.0 (plainly different) — and because the `#f0f6fc`
entry forms first, the white text never surfaces in the `text`/`link` roles at all.

So in the **text and border pools only**, two *near-white* entries (lightness ≥ 0.90) merge —
both at the join above and at the cluster step — only if they are also within **3.0 ΔE2000**
(`NEAR_WHITE_MERGE_MAX_DE2000`), measured with the accurate-near-white CIEDE2000 metric. That
3.0 is a *denoising* radius, deliberately looser than the 1.0 ΔE2000 identity floor: anti-alias
variants (~1–3 ΔE2000 from their canonical color) still collapse, while genuinely-distinct
tokens like `#ffffff`/`#f0f6fc` stay apart. The **background** pool keeps the pure OKLab radius —
there its coarseness usefully denoises quantized screenshot bins, and that is the regime OKLab
is being relied on for. (The near-black analogue is left to the family-pool split above.)

### The cross-OS quantizer incident

The loose bg radius has a knock-on effect on reconciliation (§5), pinned down by a real
cross-platform bug. Reconciliation needs to decide whether a *measured* usage color
matches a *declared* token color. A measured entry's representative color is a screenshot
quantizer bin whenever the cluster matched one — and quantizer output is
platform-dependent (anti-aliasing and font rendering differ across OSes). Concretely: an
amber CTA (declared `#f59e0b`) sitting over a blue hero quantized to `#c4a571` on Linux —
more than ΔE 0.08 from the declared color, but within 0.10. At a tight 0.08 match radius,
a pixel-perfect rendered token *failed its own intent match* on one OS: the posterior
winner flipped and a false "declared unused in render" divergence appeared, on Linux
only. The fix is structural: since an element may join a bin up to the bg radius (0.10)
away, the measured-vs-declared match radius (`DELTA_E_MATCH_MEASURED`) must be **at
least** that — so it is defined as equal to `DELTA_E_MATCH_BG`, and can never silently
fall below it.

## 4. The usage views

`palette/usage.py` turns the clusters into two complementary views, keyed off one fixed
code-level convention: the **role→component collapse** (`ROLE_COMPONENTS`). Each component
type belongs to exactly one of eight developer-facing **usage roles** — `page_bg` → `page`;
`card_bg`/`modal_bg`/`hero_bg`/`input_bg` → `surface`; `header_bg`/`nav_bg`/`footer_bg` →
`banner`; `cta_bg` → `cta`; `button_secondary`/`badge` → `action`; the `*_text` components →
`text`; `link` → `link`; `border` → `border`. Two components route nowhere on purpose:
`cta_text` (the button-label sink — a button-styled element's text color is part of the CTA,
not an independent palette role, so it is emitted but carries no usage) and `third_party`
(vendor-widget colors are excluded and surface separately on the result). The inverse map is built once and asserted
to partition every routed component to exactly one role. This taxonomy is the redesign's
core: it splits the two axes the old 4-value usage taxonomy conflated — *which CSS property
paints the color* (a `property_family` rollup: background / text / border) versus *what kind
of element it is* — so a link's color and a CTA button's background no longer share one slot.

### The role-keyed projection (`build_usage`)

For each usage role, a probability-ranked list of colors. A color used in multiple ways,
like the same gray as both text and border, correctly appears in multiple roles.
**Prominence** — how clusters rank within a role — depends on whether the role names a
*structural surface* or an *element color*:

- **Structural-surface roles (page/surface/banner) are ranked by screenshot area.** Area is
  the authoritative signal for surfaces, and vote *counts* would actively mislead: a page
  with 30 repeated cards produces 30 card-background votes, while the page background —
  covering, say, 86% of every pixel — is one `<body>` element with one vote. Ranking by votes
  would crown the cards; ranking by area correctly crowns the background. A dominant-area
  cluster anchors each of these roles, so the winner is also stable across operating systems.
  (Only clusters with nonzero vote mass in the role participate at all — area alone doesn't
  prove a color *is* used that way.)

- **Element-color roles (cta/action/text/link/border) are ranked by `log1p` of vote mass.**
  These paint negligible screenshot area, so area would be wrong twice over. It would be
  *incorrect*: the page background out-areas every button, so an area-ranked `cta` collapses
  to the page-background hex and the real brand CTA is pruned below `MIN_SHARE` — on
  github.com the `cta` role reported the dark page background and the green "Sign up" button
  vanished. And it would be *non-deterministic*: which of two near-zero-area buttons forms its
  own median-cut screenshot bin (and so gets area) flips between macOS and Linux on identical
  input — the cross-OS coin flip the golden tests kept catching. Vote mass is DOM-derived
  (computed colors, not rendered pixels), so it ranks the real element color — and the primary
  button over the secondary — correctly and *stably*. `log1p` damps it *sub-linearly* so that
  element count can't drown high-confidence evidence: it compresses big masses
  (`log1p(93) ≈ 4.5` vs `log1p(1.0) ≈ 0.69`) without changing the *ordering* (the logarithm is
  monotonic), while genuinely tiny masses still prune (`log1p(0.05) ≈ 0.05`).

This is why each interactive color also gets its own role rather than one shared slot: on
github.com ~200 link votes (clusters with mass 93 / 55 / 48) and the lone green CTA once
competed for a single `interactive` slot, dropping the CTA below the pruning floor. With
links in `link` and the CTA mass-ranked in `cta`, the brand green surfaces (and on a page
with two hero buttons, the higher-mass primary button wins the role on every OS). The
cta/action `property_family` stays `background` for the family rollups and the color-keyed
index below; only their *ranking signal* is vote mass.

Within each role the prominence scores are normalized to probabilities, entries below
`MIN_SHARE` (0.02) are pruned, and survivors are renormalized. If pruning would empty a
non-empty role, the single argmax entry is kept at probability 1.0 instead — a role that
measured *something* never reports nothing.

`MIN_SHARE` is a *relative* threshold, so when a role accumulates many colors every
entry's share shrinks and a genuine low-mass color can drop below 0.02 purely from
dilution. To stop that, an element-color entry whose raw in-role vote mass clears
`MIN_MASS` (≈ one element's worth of confident vote) is exempt from the share prune — it
has independent, DOM-derived evidence that it genuinely paints the role. The exemption is
element-color-only; the area-ranked structural-surface roles rank by screenshot area and
stay on pure share.

### The color-keyed index (`build_color_index`)

The same clusters, re-projected as the canonical, color-first answer to "how is each color
used?". Because the inventory now clusters per family, one color can arrive as several
clusters (the same gray as a text cluster *and* a border cluster, or `#ffffff` as both a
background bin and a text color), so clusters are first grouped by **exact hex** and merged
into one atom per color — exact equality is deliberate, since it keeps family-distinct hexes
like a near-white `#e5e5ea` border and the `#ffffff` page apart while collapsing only what is
truly the same value. Each merged atom becomes a `ColorUsage`: its `usages` are one slot per
role it participates in (the role's routed mass over the color's total routed mass — slots
sum to ~1 — plus normalized per-component evidence and the `property_family` rollup), and its
overall `prominence` blends the atom's normalized screenshot area with its normalized
`log1p` of total routed vote mass. The blend (`PROMINENCE_AREA_WEIGHT`, default 0.7 toward
area) is a **first-cut heuristic** — area-truth primary so dominant backgrounds rank high,
vote-mass secondary so zero-area brand accents (CTA/link colors) are not buried — and is
flagged in-code as worth later empirical tuning the way the role taxonomy was. The tuple is
sorted by `prominence` descending; third-party-dominated clusters are excluded (as in the
role view).

## 5. Reconciling with declared intent

`palette/reconcile.py` operates on the role-keyed projection. It fuses two independent
signals about each usage role: the **measured** usage probabilities from §4 ("what actually
rendered") and the **declared** token intent from §2 ("what the author said"). First,
declared token colors are grouped with each other at a tight ΔE 0.08 (both sides are exact
computed values), accumulating each group's weighted usage intent; measured entries then
match declared groups at the looser 0.10 radius from §3.

### Log-linear pooling, unpacked

For each candidate color in a role, the two probabilities are combined as a
**weighted geometric mean**:

```
posterior ∝ p_usage^(1 − α) × (p_intent + 1/K)^α
```

over the K measured entries in the role, then all candidates are normalized to
sum to 1. Reading the formula:

- **The candidates are the measured entries only.** Declared intent re-weights colors
  that actually rendered; a declared color with no measured match never enters the
  posterior (it surfaces through the divergence report instead). This is what makes the
  contract guarantee structural: every posterior entry inherits its measured entry's
  area and non-empty component breakdown.
- **α (alpha) is the weight on intent**, default 0.4 and clamped to [0, 1]. At `α = 0`
  the intent factor collapses to `x^0 = 1` and the posterior is pure measurement; at
  `α = 1` it's pure declared intent. At 0.4, measurement leads but strong declared intent
  can shift the ranking.
- **Why a *geometric* mean** (multiplying powers) rather than a weighted average? A
  geometric mean rewards agreement: a color must score in *both* signals to score high,
  and a near-zero on either side drags the product toward zero. A weighted average would
  let a barely-rendered color coast on intent alone.
- **The `1/K` term is uniform smoothing** on the intent side: a color with no token
  match within ΔE 0.10 still gets the uniform pseudo-intent `1/K`, so lacking a token
  costs at most a bounded, universe-scaled factor of `(K + 1)^α` (≈1.6× at K = 2, ≈2.6×
  at K = 10 for the default α) — a penalty, never a veto. An absolute floor (the
  pre-0.4.0 `EPS = 10⁻⁹`) made the same term a ~4000× multiplier that let one minor
  declared color erase a 95%-dominant undeclared one from the posterior entirely.

Posterior entries below 0.02 are pruned and survivors renormalized (argmax kept if
pruning empties the role). Every entry keeps its measured area and component breakdown.

### The empty-role gate

A role with **no measured usage at all yields an empty posterior.** Declared-only
colors never enter any posterior, and the original motivation was a live failure in
exactly this case: when token-only colors were still injected, zero measurement gave
every one the *same* floor usage factor, so the posterior collapsed to `intent^α` — a
near-uniform spread where everything survives pruning. On github.com that meant
`usage.border` reported **16 never-rendered theme tokens**, every entry with empty
components — pure noise presented as measurement. Honest emptiness beats intent-only
noise. Declared intent for an unmeasured role can still surface through the
divergence report — but only when the declared color has no perceptual match (within
0.10) among measured colors in *any* role; a near-white border token on a
white-surfaced page reads as "used" and stays silent.

### Divergence reporting

Two kinds of discrepancy are reported:

- **Declared but unused** — a declared color with no perceptual match (within 0.10)
  among measured usage in *any* role. This is gated to **high-intent** origins only:
  tokens classified by an explicit name rule or relational pattern. The gate exists
  because of another live failure: on token-heavy sites, every unused shade of every
  numbered color scale is technically "declared", and the report was 100% noise — **54
  out of 54 items on github.com** were unused scale shades. Scale members, alias
  followers, and fallbacks therefore never fire this item. Name-rule tokens report under
  their intent's strongest role; relational tokens (`--on-primary`-style foreground
  colors, which carry no role prior) report under `text`.
- **Used but undeclared** — a measured entry with probability ≥ 0.15 whose color matches
  no *declared* color at all. Membership is tested against every resolved token color —
  including relational, status, scale, and fallback classifications that carry no intent
  mass — because "undeclared" is a statement about the stylesheet: a page rendering
  exactly its declared `--on-primary` text color is not undeclared. A prominent rendered
  color the design system doesn't name.

## 6. Concurrency and safety

A few structural guarantees hold across the pipeline; this section explains what they are
and why they hold, not the line-by-line mechanics.

**Everything downstream of the harvest is pure.** Networking lives entirely behind
`PolitenessPolicy` / `harvest_page`; given a `Harvest`, the classify/inventory/usage/
reconcile chain does no I/O and shares no mutable state. That is why the per-theme
CPU work (which includes O(n²) perceptual clustering) can be pushed onto worker threads
with `asyncio.to_thread` — the event loop stays responsive while themes are analyzed
concurrently — and why the whole downstream pipeline is testable without a network or a
browser.

**Renders are coalesced, cached, and bounded.** `PolitenessPolicy.fetch` is the single
gate. Concurrent fetches for the same URL + theme + viewport are *single-flighted*: the
first caller becomes the leader and runs the one throttle → robots-check → render
sequence; everyone else becomes a follower awaiting the leader's future. Failures fan out
to followers (but are never cached). Cancellation is the subtle case: if the *leader's*
task is cancelled (a deadline expired, an HTTP client disconnected), that cancellation
belongs to the leader's caller — followers must not inherit it. Instead the shared future
is cancelled, the shielded followers detect that, and they **re-elect**: exactly one
follower becomes the new leader and re-runs the sequence, the rest follow it, and if the
render somehow completed first the re-check loop serves it from the cache. The fetch
path's check sequence (cache, then in-flight table) contains no `await`, so it is
race-free under the single-threaded event loop.

**The rate limiter reserves under the lock and sleeps outside it.** Per-host pacing works
by stamping the *projected* next-fetch time (`last + interval`) inside an `asyncio.Lock`,
then releasing the lock and sleeping out the wait. Reserving under the lock means two
same-host callers arriving together chain correctly (each waits a full interval after the
previous) instead of both computing a zero wait from a stale timestamp; sleeping outside
it means a caller waiting out a long `Crawl-delay` never blocks fetches to *other* hosts
through the same mutex. Relatedly, when `max_concurrent_renders` is set, its semaphore
wraps strictly the render itself — never the throttle/robots wait — so a slot is only
held while a browser is genuinely rendering.

**The SSRF guard resolves DNS off the event loop and fails closed.**
`block_private_networks()` returns an async predicate applied to every HTTP(S) URL the
browser requests (navigation and all sub-resources) and to the policy's own `robots.txt`
GET (including each redirect hop); the paths route interception cannot see are closed
outright — WebSocket connections are refused whenever a filter is configured, and service
workers are always blocked at context creation. On a cache miss, the blocking `getaddrinfo` runs on a
small thread pool the predicate itself owns — never the loop's shared default
`to_thread` executor, so guard lookups cannot starve the pipeline's CPU phase or an
embedding application's own thread-pool work — capped by a fail-closed per-lookup
timeout; verdicts land in a per-hostname TTL+LRU cache (negative verdicts too —
re-resolving a hostile hostname on every request would hand the page an amplifier),
concurrent misses for one host coalesce into a single lookup, and fan-out to distinct
slow hostnames beyond the pool size queues inside the guard's own pool rather than
pinning a thread per host. Every failure mode — malformed URL, resolution failure,
resolution timeout, empty resolution, a raising predicate — **fails closed**: the
request is aborted, never waved through. A
hostname passes only if *all* of its resolved addresses are public (one public plus one
internal A record is exactly the split-horizon shape an attacker would use). The
predicate's single-flight futures are loop-bound, so each guard instance serves **one
event loop at a time**: sequential reuse across loops re-binds when idle and keeps the
verdict cache, while concurrent cross-loop use raises (which the filter seam turns into
fail-closed aborts). One limitation of this is that a URL-string predicate cannot
fully defeat DNS rebinding — network isolation of the browser environment remains the
primary control, per `SECURITY.md`.

## Performance

What dominates the cost of an `analyze()` call, and the knobs that exist:

- **Rendering dominates; a second theme roughly doubles it.** Each requested theme is a
  whole extra headless render — which is why the default is light-only and dark is
  opt-in. Themes render concurrently, and near-identical renders collapse to one reported
  theme (the render cost is still paid; the collapse saves only the duplicate CPU
  analysis and result noise).
- **One browser launch per call.** All themes of a call share one lazily launched
  Chromium (`SharedBrowser`), each render in its own browser context — a multi-theme
  analysis pays a single launch, and a run whose fetches are all cache hits pays none.
  The browser is closed as soon as the renders finish, before the CPU phase.
- **The render cache.** `PolitenessPolicy` caches full `Harvest` objects keyed by
  **URL + theme + viewport geometry** (width, height, device scale). Cache hits return
  immediately — no robots check, no throttle, no render. The cache is LRU-bounded
  (default 256 entries; these are the largest objects the policy retains). Reusing one
  policy across calls is how you benefit from it.
- **`max_concurrent_renders`** caps simultaneous renders through a policy (unbounded by
  default). Cache hits and single-flight followers never take a slot, and the slot wraps
  only the actual render, so the cap bounds Chromium load without serializing the
  politeness waits.
- **CPU work stays off the event loop.** The per-theme classification and clustering runs
  in worker threads (`asyncio.to_thread`), so an `analyze` awaited inside a server
  endpoint doesn't stall the loop while it crunches.
- **Small render-path savings, measured against real sites** (from the code's own
  calibration notes): capturing the screenshot as quality-92 JPEG instead of PNG saves
  ~0.3–0.65 s/render with bin shifts ≤ 0.012 ΔE (below cross-platform rendering drift),
  and capping the post-`load` network-idle wait at 1 s saves ~1.5 s/render with unchanged
  output — the step-scroll still triggers genuinely lazy content.
