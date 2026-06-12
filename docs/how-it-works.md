# How it works

This page walks through the full analysis pipeline, step by step, with the actual logic
and calculations spelled out. It assumes no familiarity with the codebase — just Python
and a rough idea of how web pages are built.

The one-line version: `analyze(url)` renders the page in headless Chromium, **harvests**
four kinds of raw evidence from it, **classifies** the declared design tokens and the
visible DOM elements, fuses everything into a perceptually clustered **color inventory**,
builds the **usage-keyed palette** (what colors paint surfaces, text, interactive
elements, and borders), **reconciles** that against what the site's CSS declared, and
derives a **60/30/10 roles** view. Everything after the harvest is deterministic, pure
CPU work.

Perceptual color distance appears throughout. It is always the same function: Euclidean
distance in the OKLab color space ("ΔE", `deltaEOK`), whose units are small — in this
codebase 0.05 is the radius at which two colors are treated as near-identical, and 0.10
is a deliberately generous "same paint" radius. Every threshold below is tuned to that
scale.

## 1. Rendering and harvesting

A render opens a fresh browser context at a fixed viewport (1280×800 by default) with the
requested `prefers-color-scheme` (light by default), navigates, waits for the `load`
event plus a short, capped network-idle wait, injects CSS that disables all transitions
and animations (so computed colors are stable, not mid-fade), step-scrolls the full page
height (up to 20 viewport-steps) to trigger lazily loaded content, and detects
cookie-consent / overlay banners so they can be masked out later.

From that one live page, four harvests run:

**Visible DOM elements** (`harvest/dom.py`). An in-page script walks every element and
records its computed `background-color`, `color`, and `border-color`, its bounding
rectangle and CSS `position`, its tag / ARIA role / id / class tokens, and structural
flags: is it clickable, does it have a box shadow, does it have *direct* text content
(descendant text deliberately doesn't count, or every wrapper of any text would carry the
flag), is it an iframe / cross-origin / shadow host / known third-party vendor widget.
Hidden, zero-area, and `aria-hidden` elements are excluded. One subtlety: a border color
is only reported when the element actually paints a border (`border-top-width > 0`) —
the computed border color resolves for *every* element regardless of width, so an ungated
read would report a meaningless (usually black) "border color" on virtually everything.

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
detected consent-banner rectangles are zeroed out of a boolean keep-mask, so a cookie
banner cannot pollute the palette. The image is then downscaled to at most 256 px on its
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

The classified role is then mapped (via the YAML's `role_to_usage_prior` table) to a
prior distribution over the four usage categories — e.g. `brand_accent` is expected to
render 90% interactive / 10% surface, while `neutral` spreads across surface, text, and
border.

### Component classification (`classify/components.py`)

Each harvested element is scored into a probability distribution over component types
(`page_bg`, `header_bg`, `card_bg`, `cta_bg`, `link`, `border`, `page_text`, …,
`third_party`). The scoring is additive voting across eight feature families — semantic
tags and ARIA roles, geometry (a full-width element near the top of the viewport votes
`header_bg`), class/id token substrings (`"navbar"` votes `nav_bg`), interactivity,
border presence, text presence, repetition (three or more siblings sharing a tag and
class token, each with a shadow/border/background, vote `card_bg` — the card detector),
and third-party origin signals. Then multiplicative suppressors apply (`aria-hidden` and
hidden elements are zeroed; brand-component votes on third-party widgets are damped to
5%), and finally the surviving positive votes go through a softmax at temperature 0.5,
entries below probability 0.05 are pruned, and the survivors are renormalized.

Two of those families are worth working through, because their single vote weight was
calibrated against the softmax explicitly (the YAML keeps a short note next to each
weight; the full derivation lives here):

**Border presence** — any element that genuinely paints a border gets a `border: 2.5`
vote. With temperature 0.5, a vote `v` contributes `e^(v/0.5)` before normalization:

- A bordered card (`card_bg: 3.0` from its class token) computes
  `e^6 ≈ 403` vs `e^5 ≈ 148`; the softmax divides each by their sum, so `border` keeps
  probability `148 / (403 + 148) ≈ 0.27` — comfortably above the 0.05 pruning floor.
  Same numbers for a bordered text input (`input_bg: 3.0`).
- A bordered *submit* input accumulates `cta_bg: 7.0` (semantic `input[submit]` 3.5 +
  clickable 1.5 + the `input[submit|button]` interactivity vote 2.0): `e^14 ≈ 1.2×10⁶`
  dwarfs `e^5`, so its border share is ~10⁻⁴ and prunes — button-like inputs stay
  interactive, not borders. A bordered CTA button (`cta_bg ≥ 9`) prunes the same way.

So the single 2.5 weight makes borders on *structural* elements measurable while borders
on *interactive* elements stay attributed to interactivity. This family exists because of
a real failure: with only the `<input>` rule voting `border`, pages without classified
inputs measured zero border mass anywhere (github.com's `#d1d9e0` borders were simply
absent from the result, while never-rendered border *tokens* flooded the category — see
§5).

**Text presence** — any *non-clickable* element with direct text content gets a
`page_text: 2.0` vote. A bare `<p>` with no other votes gets `page_text` probability 1.0;
a text-bearing card (`card_bg: 3.0`) computes `e^6 ≈ 403` vs `e^4 ≈ 55`, so `page_text`
keeps `55 / (403 + 55) ≈ 0.12` — measured, without displacing `card_bg`. The 2.0 deliberately stays below
every semantic `*_text` vote (e.g. `body`'s `page_text: 4.0`), so semantic rules dominate
where they apply. Clickable elements are excluded on purpose: their typography is
interactive by definition and already routed through the link rules — letting them vote
`page_text` would leak link colors into the text category. This family also fixed a real
gap: typography in plain `<p>`/`<span>` content was previously never measured
(github.com's muted `#59636e` was absent from `usage.text`).

## 3. Building the color inventory

`palette/inventory.py` fuses the screenshot's *area truth* with the elements' *semantic
truth* into `ColorCluster`s:

1. **Seed** one working entry per screenshot bin, carrying its authoritative
   `area_fraction` and an empty component mix.
2. **Attribute element semantics.** Each classified element's component distribution is
   split by *color channel*: `*_text` components and `link` route to the element's text
   color (a link paints its typography, not its usually-transparent background), `border`
   to its border color, everything else to its background color. A channel whose measured
   color is fully transparent (`alpha == 0` — e.g. the default
   `background-color: transparent`) paints nothing and donates no votes; without that
   gate, every transparent-background element would pile votes onto a phantom black
   zero-area cluster. Each channel's vote mass is added to the nearest existing entry
   within the channel's **join radius** — or, if nothing is close enough, a new entry
   with `area_weight = 0` is created so the semantics aren't lost.
3. **Cluster.** Entries within ΔE 0.05 of each other are merged transitively
   (union-find: if A is near B and B is near C, all three become one cluster). Each group
   becomes one `ColorCluster`: the representative color is the member with the largest
   area weight, `area_weight` is the group sum, and the component votes are kept both raw
   (`component_mass` — cross-cluster magnitudes matter later) and normalized
   (`component_mix`).

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
  surface bins and erase its text hierarchy from the usage view. The tight radius
  bounds that absorption; it does not eliminate it. Colors genuinely within 0.05 of
  each other still merge — `#1f2328` body text and a `#002a36` surface sit at
  ΔE ≈ 0.041 and become one cluster — a known limitation of clustering in OKLab.

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

## 4. The usage-keyed palette

`palette/usage.py` turns the clusters into the primary result: for each usage category —
`surface`, `text`, `interactive`, `border` — a probability-ranked list of colors. Each
component type routes to one category (backgrounds → surface, typography → text, links
and CTAs → interactive, borders → border; `third_party` routes nowhere — vendor-widget
colors are excluded and surface separately on the result). A color used in multiple ways,
like the same gray as both text and border, correctly appears in multiple categories.

The interesting part is **prominence** — how clusters are ranked within a category —
which is deliberately scored differently per category:

- **Surfaces are ranked by screenshot area.** Area is the authoritative signal for
  surfaces, and vote *counts* would actively mislead: a page with 30 repeated cards
  produces 30 card-background votes, while the page background — covering, say, 86% of
  every pixel — is one `<body>` element with one vote. Ranking by votes would crown the
  cards; ranking by area correctly crowns the background. (Only clusters with nonzero
  surface vote mass participate at all — area alone doesn't prove a color *is* a
  surface.)

- **Text, interactive, and border are ranked by `log1p` of vote mass.** These paint
  negligible screenshot area, so area can't rank them; how many elements use the color
  can — but only *sub-linearly*. Raw linear mass lets element count drown high-confidence
  single-element evidence. The motivating case: on github.com, roughly 200 link votes
  (clusters with mass 93 / 55 / 48) pushed the page's one green CTA — vote mass 1.0 — down
  to a 0.005 share of the `interactive` category, below the pruning floor, and the brand
  accent vanished from the palette. Taking `log1p(mass)` compresses the big masses
  (`log1p(93) ≈ 4.5` vs `log1p(1.0) ≈ 0.69`) without changing the *ordering* (the
  logarithm is monotonic): the CTA's share becomes ≈ 0.04 and survives, while genuinely
  tiny masses still prune (`log1p(0.05) ≈ 0.05`).

Within each category the prominence scores are normalized to probabilities, entries below
`MIN_SHARE` (0.02) are pruned, and survivors are renormalized. If pruning would empty a
non-empty category, the single argmax entry is kept at probability 1.0 instead — a
category that measured *something* never reports nothing.

## 5. Reconciling with declared intent

`palette/reconcile.py` fuses two independent signals about each usage category: the
**measured** usage probabilities from §4 ("what actually rendered") and the **declared**
token intent from §2 ("what the author said"). First, declared token colors are grouped
with each other at a tight ΔE 0.08 (both sides are exact computed values), accumulating
each group's weighted usage priors; measured entries then match declared groups at the
looser 0.10 radius from §3.

### Log-linear pooling, unpacked

For each candidate color in a category, the two probabilities are combined as a
**weighted geometric mean**:

```
posterior ∝ p_usage^(1 − α) × (p_intent + 1/K)^α
```

over the K measured entries in the category, then all candidates are normalized to
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
pruning empties the category). Every entry keeps its measured area and component
breakdown.

### The empty-category gate

A category with **no measured usage at all yields an empty posterior.** Declared-only
colors never enter any posterior, and the original motivation was a live failure in
exactly this case: when token-only colors were still injected, zero measurement gave
every one the *same* floor usage factor, so the posterior collapsed to `intent^α` — a
near-uniform spread where everything survives pruning. On github.com that meant
`usage.border` reported **16 never-rendered theme tokens**, every entry with empty
components — pure noise presented as measurement. Honest emptiness beats intent-only
noise. Declared intent for an unmeasured category can still surface through the
divergence report — but only when the declared color has no perceptual match (within
0.10) among measured colors in *any* category; a near-white border token on a
white-surfaced page reads as "used" and stays silent.

### Divergence reporting

Two kinds of discrepancy are reported:

- **Declared but unused** — a declared color with no perceptual match (within 0.10)
  among measured usage in *any* category. This is gated to **high-intent** origins only:
  tokens classified by an explicit name rule or relational pattern. The gate exists
  because of another live failure: on token-heavy sites, every unused shade of every
  numbered color scale is technically "declared", and the report was 100% noise — **54
  out of 54 items on github.com** were unused scale shades. Scale members, alias
  followers, and fallbacks therefore never fire this item. Name-rule tokens report under
  their intent's strongest category; relational tokens (`--on-primary`-style foreground
  colors, which carry no category prior) report under `text`.
- **Used but undeclared** — a measured entry with probability ≥ 0.15 whose color matches
  no *declared* color at all. Membership is tested against every resolved token color —
  including relational, status, scale, and fallback classifications that carry no intent
  mass — because "undeclared" is a statement about the stylesheet: a page rendering
  exactly its declared `--on-primary` text color is not undeclared. A prominent rendered
  color the design system doesn't name.

## 6. The 60/30/10 roles view

`palette/roles.py` derives a second, opinionated view: the classic 60/30/10 interior-
design split mapped to five roles — **primary** (the dominant, usually neutral surface,
~60%), **secondary** (structural color: cards/headers/nav, ~30%), **accent** (the
action/brand "pop", ~10%), plus **neutral_light** and **neutral_dark**. Unlike the usage
view it is *measured-only* — never reconciled against tokens.

Each cluster gets a score per role from weighted features: primary rewards area,
neutrality (a smooth `1 − chroma/0.10` signal), and page-background component votes;
accent rewards chroma, contrast against the provisional primary, and action-component
votes — with only a small area term, so a tiny but vivid CTA can win; secondary rewards
area and structural-surface votes (the "card exception"); the neutrals score
`neutrality × lightness` (or `× (1 − lightness)`) with a small area floor. Per role, the
scores go through a softmax (temperature 0.25), pruning, and renormalization into ranked
candidates.

`fit_score` then measures how 60/30/10-like the page actually is: take the top
candidate's area for primary/secondary/accent, normalize the triple to sum to 1, and
compare against (0.6, 0.3, 0.1):

```
fit = 1 − 0.5 × Σ |measured_i − target_i|
```

The 0.5 maps the maximum possible L1 distance between two distributions (2.0) onto
[0, 1], so 1.0 means a textbook 60/30/10 split and 0.0 means nothing measurable. It is
descriptive, not a quality grade.

## 7. Concurrency and safety

A few structural guarantees hold across the pipeline; this section explains what they are
and why they hold, not the line-by-line mechanics.

**Everything downstream of the harvest is pure.** Networking lives entirely behind
`PolitenessPolicy` / `harvest_page`; given a `Harvest`, the classify/inventory/usage/
reconcile/roles chain does no I/O and shares no mutable state. That is why the per-theme
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
fail-closed aborts). Honest limit, stated in the code too: a URL-string predicate cannot
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
