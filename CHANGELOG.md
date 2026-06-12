# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking — usage-keyed result contract

The public result is re-keyed around **usage** (what colors paint surfaces / text /
interactive elements / borders); the 60/30/10 roles taxonomy becomes a derived, per-theme
view. On neutral-layered designs (e.g. a GitHub repo page) the old shape lost the design's
actual structure — the gray text/border hierarchy appeared nowhere and the "secondary"
slot collected noise. The library has no consumers yet; breaking now is deliberate.

Migration table (old → new):

| Old | New |
| --- | --- |
| `ThemePalette.roles` (reconciled) | `ThemePalette.usage` — the primary, reconciled view: `UsagePalette` mapping each `UsageCategory` (`surface`/`text`/`interactive`/`border`) to ranked `UsageEntry`s (`color`, `probability`, `area`, `components`). `ThemePalette.roles` remains, but as a **measured-only** derived 60/30/10 view, no longer reconciled against tokens. |
| `AnalysisResult.fit_score` | `ThemePalette.fit_score` (per theme; descriptive "how 60/30/10-like", not a quality score) |
| `AnalysisResult.divergence` | `ThemePalette.divergence` (per theme) |
| `DivergenceItem.role: PaletteRole` | `DivergenceItem.category: UsageCategory` |
| `AnalysisResult.tokens: tuple[ClassifiedToken, ...]` | `ThemePalette.tokens: tuple[DesignToken, ...] \| None` — **opt-in** via `analyze(..., include_tokens=True)`; `None` = not requested, `()` = requested but none declared. `DesignToken` carries `name`, resolved `color`, `semantic_role`. |
| `AnalysisResult.status_colors` | Removed. Status tokens stay excluded from the palette views and surface in the opt-in token list with `semantic_role=status`. |
| `PaletteCandidate.evidence` | Removed (internal scoring-term names are not contract). `color`/`probability`/`area` remain. |
| `RunMetadata.single_theme` | Removed; use `len(metadata.themes_analyzed) == 1`. |
| `ClassifiedToken`, `TokenRecord` (public exports) | Internal-only; removed from the public API. The public token projection is `DesignToken`. |
| — | New public exports: `UsageCategory`, `UsageEntry`, `UsagePalette`, `DesignToken`, `ComponentType` (keys `UsageEntry.components`). |

Other changes riding the redesign:

- **Divergence noise fix:** declared-but-unused items are now gated to *high-intent*
  tokens (classified by an explicit name rule or relational pattern). Unused shades of
  numbered color scales, alias followers, and fallbacks no longer fire — on token-heavy
  sites the old report was 100% noise (54/54 items on github.com).
- `analyze()` gains keyword-only `include_tokens: bool = False`; the CLI gains a matching
  `--tokens` flag. The flag gates only output assembly — classification and
  reconciliation always run, so all other fields are identical either way.
- The CLI's human-readable output (unstable by design) now leads with the usage view per
  theme, then the roles summary + fit score, divergence, and (with `--tokens`) the token
  list.
- Config YAML: `role_to_palette_prior` is renamed **`role_to_usage_prior`** and its
  distributions are now over the four usage categories; custom config files must be
  updated. The token classifier's neutral light/dark special-case is gone (the usage
  taxonomy has no light/dark neutral split).
- Inventory channel routing: `link` component mass now routes to the element's **text**
  color (a link paints its typography, not its usually-transparent background), and
  fully-transparent (`alpha == 0`) channel colors no longer donate vote mass (previously
  they piled votes onto a phantom `#000000` zero-area cluster).
- Component classifier calibration: the `input` semantic rule's `border` vote is raised
  2.0 → 2.5 so input borders survive softmax pruning (at 2.0 the usage view's border
  category was structurally empty on input-bearing pages). (Superseded in the same
  release by the `border_presence` family below, which generalizes the vote to every
  element that actually paints a border.)

### Fixed — pre-release review follow-up

- **Dead data path removed: `ClassifiedToken.text_on_base`.** The relational classifier
  resolved each `--on-<base>` / `--<base>-foreground` token's base surface to a semantic
  role and threaded it through the classification tuple and alias inheritance — but
  nothing consumed it after the 0.4.0 contract change made `ClassifiedToken` internal-only
  (the public `DesignToken` projection never carried it, and reconciliation's relational
  divergence pass uses only origin, resolved color, weight, and name). The field, its
  threading, and the base-role lookup are gone; the relational *classification* itself
  (`text_on` role, weight, `relational` origin) is unchanged, as is the YAML
  `relational_modifiers` schema — patterns still capture `base`, since the name match
  depends on it. The bundled YAML's stale comment claiming the pairing "is surfaced on
  the classified token for consumers" is corrected. No behavior change (golden snapshots
  untouched).

- **Dead classifier knobs removed; the config loader now rejects unknown dispatch
  names.** The bundled YAML shipped two knobs the classifier could never act on: the
  `has_focus_ring` interactivity rule (no focus-ring signal exists on harvested elements)
  and the `consent_masked_region` suppressor (consent rects are consumed by screenshot
  masking and never reach the classification layer) — both hard-returned False in
  `classify/components.py`, contradicting the "nothing hard-coded, the YAML is the single
  source of truth" contract and inviting consumers to tune values that did nothing. Both
  entries and their dead code branches are gone. To keep removed (or misspelled) names
  from becoming silent no-ops in custom `config_path=` YAMLs, `Config` now validates
  dispatch names against the closed sets the classifier implements: unknown
  interactivity/geometry `when:` predicates, suppressor keys, and suppressor `applies_to`
  scopes fail loudly at load time. Bundled-config behavior is unchanged (golden snapshots
  untouched).

- **The egress gate now covers WebSockets and service workers.** The `request_filter`
  route handler is installed via Playwright's `context.route`, which never sees WebSocket
  opening handshakes and (by default) service-worker-originated requests — so a hostile
  rendered page could issue `new WebSocket('ws://169.254.169.254/')`, a real blind GET to
  an internal host the filter never vetted, despite the docs claiming coverage of "every
  URL the browser requests". Both unrouted paths are now closed outright rather than
  filtered: browser contexts are always created with `service_workers="block"` (service
  workers are irrelevant to color harvesting), and when a `request_filter` is configured a
  `context.route_web_socket` handler refuses every WebSocket connection — it never
  connects upstream, so no handshake leaves the browser and the page just observes a dead
  socket, harmless for palette extraction. The declared playwright floor rises
  `>=1.40` → `>=1.48` (where `route_web_socket` landed; the lockfile already resolved
  1.60.0). `SECURITY.md` §1 and the `block_private_networks` docs now state the coverage
  precisely.
- **Relational and status tokens are visible to divergence again.** Empty-prior tokens
  (relational `--on-primary`-style foregrounds; status tokens excluded from the palette)
  were invisible to reconciliation: a page rendering exactly its declared
  `--on-primary` color was falsely reported "used but undeclared" (the canonical
  shadcn/Material `*-foreground` pattern), and the documented relational arm of the
  declared-but-unused gate was unreachable. Used-but-undeclared membership now tests
  against every resolved declared color, and unused relational tokens report
  declared-but-unused under `text` via a dedicated pass.

- **Reconciliation pooling no longer vetoes undeclared colors.** The log-linear pool's
  intent factor is now uniform-smoothed (`+ 1/K` over the category's K measured entries)
  instead of floored at `EPS = 1e-9`: lacking a token match costs a bounded,
  universe-scaled penalty (`(K + 1)^alpha`, ~1.6x at K=2 for the default `alpha=0.4`)
  rather than a ~4000x multiplier. Previously, on a partially-tokenized page, one minor
  declared color could erase a 95%-dominant undeclared color from `usage` entirely (a
  95%-white page whose surface palette contained no white).
- **The "`components` is never empty" guarantee is now structural.** The pooling
  universe is restricted to the measured usage entries; declared-only colors never enter
  the posterior in any category (previously they were injected and only pruned by a
  numeric coincidence of `EPS`/`alpha`/`MIN_POSTERIOR_PROB`, and the reconcile docstring
  contradicted the documented guarantee). Declared-but-unused intent surfaces through
  `divergence`, as before.
- **Guard DNS lookups no longer share the loop's default executor.** Each
  `block_private_networks()` predicate now owns a small dedicated thread pool
  (`GUARD_RESOLVER_MAX_WORKERS = 8`, created lazily, living as long as the predicate)
  instead of dispatching resolutions via `asyncio.to_thread`. Single-flight coalescing is
  per-host, so a hostile page fanning requests at many *distinct* slow hostnames could
  previously pin one default-executor thread per hostname — the same pool the pipeline's
  per-theme CPU phase and any embedding application use, a cross-request DoS vector in
  multi-tenant deployments. Excess distinct-host lookups now queue inside the guard's own
  bounded pool. Each lookup is additionally capped by a new fail-closed
  `resolve_timeout` parameter (default `DEFAULT_GUARD_RESOLVE_TIMEOUT_SECONDS = 10.0`);
  on expiry the URL is rejected and the negative verdict cached like any other. Also
  fixes a marginal cache detail: the TTL expiry is now stamped *after* resolution
  completes, so a lookup slower than the TTL no longer produces a born-expired entry.

### Fixed — measurement-layer gaps (live-probe follow-up)

A live acceptance probe of the usage-keyed redesign against github.com exposed
measurement gaps the fixtures had masked; all are now encoded as offline fixture tests
(`tests/fixtures/repo_probe_site.html`):

- **Empty-category gate in reconciliation**: a usage category with zero *measured*
  candidates now yields an empty posterior instead of a near-uniform flood of token-only
  colors (github.com's `usage.border` was 16 never-rendered theme tokens, every entry
  with empty `components`). Honest emptiness beats intent-only noise; declared intent for
  an unmeasured category can still surface through `divergence` (when its color has no
  perceptual match among measured usage).
- **`border_presence` feature family** (config YAML): any element whose harvested border
  is genuinely painted (width-gated) now votes `border`. Previously only the `<input>`
  semantic rule voted `border`, so pages without classified inputs measured zero border
  mass. The `input` rule's own border vote moved into this family, and `border` joined
  the third-party-damped `brand_components` so vendor widgets don't feed the border
  palette.
- **`text_presence` feature family** (config YAML) + `HarvestedElement.has_text`:
  non-clickable elements with direct (non-descendant) text content now vote `page_text`,
  so plain `<p>`/`<span>` typography is measured (github.com's muted `#59636e` was absent
  from `usage.text`). Clickable elements are excluded — their typography is interactive
  and already routed via the link rules. Relatedly, the repetition detector's
  `distinct_bg_from_parent` proxy no longer counts fully-transparent (`alpha == 0`)
  backgrounds, which had turned repeated text spans into false-positive "cards" whose
  votes crushed the new text votes.
- **Per-channel inventory join radii**: element text/border colors now match existing
  entries at the tight cluster radius (0.05 deltaEOK) instead of the loose background
  radius (0.10), so a near-black body text (`#1f2328`) forms its own usage entry instead
  of being absorbed into an adjacent dark surface bin.
- **Log-damped vote-mass prominence** in the usage view: text/interactive/border entries
  are ranked by `log1p(vote mass)` rather than raw mass. Ordering is unchanged
  (monotonic), but element *count* no longer drowns high-confidence single-element
  evidence — github.com's lone green CTA (`#1f883d`) survives against ~200 link votes
  instead of pruning below the share floor.
- **Measured-vs-declared match radius in reconciliation**: a measured usage entry now
  matches a declared token color within the inventory's background join radius (0.10
  deltaEOK) instead of the tight 0.08 used for grouping declared colors with each other.
  A measured entry's representative is a screenshot-quantizer bin whenever the cluster
  matched one, and an element joins a bin up to 0.10 away — at 0.08 a pixel-perfect
  rendered token could fail its own intent match purely from quantizer blending
  (platform-dependent anti-aliasing), flipping posterior winners across OSes and emitting
  false "declared unused in render" / "used but undeclared" divergence pairs.
- **`input[submit]` no longer matches every `<input>`**: the harvester now captures the
  input's lowercased `type` attribute (new internal `HarvestedElement.input_type` field;
  `None` for non-inputs and untyped inputs), and both the `input[submit]` semantic rule
  and the `input[submit|button]` interactivity predicate match only button-like input
  types (`submit`/`button`/`image`/`reset`). Search/text inputs — and text inputs styled
  with `cursor: pointer` — no longer receive spurious `cta_bg` votes that leaked their
  backgrounds into the `interactive` usage category. Aligning the harvest with that set,
  `<input type="image">` (a graphical submit button) is now also harvested as
  `clickable`.

### Added

- **Documentation site** at <https://cassidyhhaas.github.io/colorsense/> — MkDocs Material
  + mkdocstrings, built from the existing guides plus a generated API reference, deployed
  to GitHub Pages from `main` (`.github/workflows/docs.yml`; PRs get a strict build check).
  Build locally with `uv sync --group docs` and `uv run mkdocs serve`.
- **"How it works" documentation page** (`docs/how-it-works.md`) — a plain-prose
  walkthrough of every pipeline stage with the actual logic and calculations (harvesting,
  classification, inventory clustering and the ΔE join radii, usage-keyed prominence
  scoring, log-linear reconciliation, the 60/30/10 roles view, concurrency/safety
  guarantees, and performance notes). The incident narratives and worked calibration
  derivations formerly embedded in source comments and the bundled YAML now live there;
  the code keeps one-line invariants pointing at the page.
- **`request_filter` seams accept async predicates.** `PolitenessPolicy`, `harvest_page`,
  and `RenderSession` now take a synchronous *or* asynchronous `url -> bool` predicate;
  the new public **`RequestFilter`** type alias (exported from the package root and
  `colorsense.harvest`) names the union. Sync predicates keep working unchanged but run
  inline on the event loop, so they must not block; async predicates are awaited, and
  raising — sync or async — still fails closed.

### Changed

- **Breaking:** `block_private_networks()` now returns an **async** predicate
  (`await guard(url)`; only usable under a running event loop, as the `request_filter`
  seams are). Its blocking DNS resolution runs off the event loop on a worker thread via
  `asyncio.to_thread` on a cache miss, with per-host single-flight coalescing (N concurrent
  requests to one slow novel hostname dispatch one lookup, not N) — so a slow nameserver no
  longer stalls the whole loop, notably on the robots-fetch redirect path, where
  attacker-influenced redirect hostnames could previously trigger up to 21 on-loop lookups
  per fetch. The injectable `Resolver` seam stays synchronous (it now runs inside the
  worker thread); the TTL+LRU verdict cache and the DNS-rebinding caveat are unchanged.
- `block_private_networks()` predicates now document and enforce (best-effort) a
  single-event-loop-*at-a-time* contract. Sequential reuse across loops — e.g.
  back-to-back `asyncio.run` calls — keeps working as before: the predicate re-binds to
  the new loop when idle and keeps its verdict cache across runs. *Concurrent* use from
  multiple event loops, which was never supported, now raises `RuntimeError` instead of
  corrupting the loop-bound single-flight state; through the `request_filter` seam the
  raise is swallowed fail-closed (requests from the other loop are aborted), so only
  direct callers see the error. Create a separate predicate per event loop for concurrent
  use.
- Docstring cross-references converted from Sphinx reST roles (`:class:`, `:func:`, ...)
  — which the docs site rendered as literal text — to mkdocstrings autorefs links for
  public API objects and plain code for internal ones. Duplicated rationale across
  comments/docstrings collapsed to one canonical home per topic. No behavior change;
  analysis output is byte-identical.
- The `examples/webservice/` reference implementation is restructured from a single
  `app.py` into an idiomatic mini FastAPI layout (`main.py`, `settings.py`, `policy.py`,
  `schemas.py`, `routes.py`; `url_guard.py` unchanged). No behavior or security-control
  change. The uvicorn entry point is now `examples.webservice.main:app` (was
  `examples.webservice.app:app`).

## [0.3.0] - 2026-06-10

Ships the safe-consumption controls for server-side use — the `block_private_networks()`
egress filter, render-concurrency cap, and overall deadline — plus the `colorsense` CLI,
and hardens the policy's own `robots.txt` fetch (per-hop egress filtering, redirect and
body-size caps).

### Added

- **`colorsense` command-line interface** — a Typer-based console script wrapping
  `analyze`: one or more URLs analyzed sequentially through a shared `PolitenessPolicy`,
  with flags for dark mode, viewport/scale, config path, overall deadline, rate limiting,
  User-Agent, the `block_private_networks()` egress filter, robots opt-out (warned on
  stderr), and `--json` output (the `AnalysisResult` schema; the human-readable summary is
  not a stable format). Adds a runtime dependency on `typer>=0.12`.
- **`block_private_networks()`** — a library-shipped egress `request_filter` factory (new
  public export) that resolves each hostname and rejects URLs resolving to any non-public
  address (loopback, RFC 1918, link-local/cloud-metadata, CGNAT, multicast, reserved, and
  IPv6 equivalents), failing closed, with a per-hostname TTL+LRU verdict cache and an
  optional narrowing `allowed_hosts` allowlist. Implements the SECURITY.md §1 egress-filter
  item; does not fully defeat DNS rebinding — network isolation remains the primary control.
- **`PolitenessPolicy(max_concurrent_renders=...)`** — an opt-in semaphore bounding
  simultaneous renders through a policy (unbounded by default). Cache hits and coalesced
  duplicate fetches never take a slot, and rate-limit waits happen outside the slot.
- **`analyze(..., max_total_seconds=...)`** — an opt-in overall deadline on the whole call
  (renders plus classification) via `asyncio.timeout`; expiry cancels in-flight renders,
  closes the shared browser, and raises the new **`AnalysisTimeoutError`** (a public
  export subclassing the builtin `TimeoutError`, carrying the url and budget).
- **`analyze(..., browser_args=...)` / `--browser-arg`** — extra Chromium launch arguments
  appended to the library's own and passed verbatim to every render of the call (the
  themes share one browser launched with them); `harvest_page`, `SharedBrowser`, and
  `RenderSession` accept the same knob for direct use. Canonical use case:
  `browser_args=("--js-flags=--max-old-space-size=512",)` caps each renderer's V8 heap
  (JS heap only — hard per-render memory/CPU caps stay container-level by design; see
  SECURITY.md §2). Non-string entries raise `TypeError` before any render.

### Security

- The policy's `robots.txt` fetch now applies the configured `request_filter` to the
  robots URL **and** every redirect hop (redirects are followed manually, capped at 20
  hops, each `Location` vetted before being requested). This closes a server-side SSRF
  bypass where a hostile `robots.txt` redirect could reach private/metadata addresses
  unfiltered — the robots GET is `httpx`, not the browser, so the browser-route filter
  never saw it. A rejected hop aborts the fetch, which fails open as "no rules" while the
  navigation stays gated browser-side.
- The policy's `robots.txt` fetch now caps the response body at 512 KiB (Google's
  documented robots.txt processing limit), read in a streaming fashion: a declared
  `Content-Length` over the cap aborts before the body is read, and a body streaming past
  the cap aborts mid-read. Previously the entire body was materialized in memory and the
  httpx timeout is per-read (not total), so a hostile or misconfigured server could
  stream an arbitrarily large body to the server-side loader — outside the browser's
  resource caps. An oversized body is treated like any other fetch failure (no rules;
  fails open).
- `block_private_networks()` now classifies IPv4-mapped IPv6 addresses
  (`::ffff:a.b.c.d`) by their embedded IPv4 address, so a resolver returning the mapped
  form of a private address is rejected like the bare IPv4 form.

### Changed

- The `examples/webservice/` reference implementation now uses the new library knobs
  (`block_private_networks`, `max_concurrent_renders`, `max_total_seconds`) instead of
  hand-rolled equivalents; only the pre-call navigation-URL validation remains app code.
- **Breaking for injected loaders:** the `RobotsLoader` seam now receives the policy's
  `request_filter` as a third argument — `(robots_url, user_agent, request_filter)`.
  Custom loaders must adopt the new signature and are responsible for applying the filter
  to the robots URL and every redirect hop they follow.
- `DEFAULT_USER_AGENT`'s version token now reflects the installed package version
  (was hardcoded `colorsense/0.1`, two releases stale), matching how the CLI's UA is
  already derived.
- The webservice example parses `COLORSENSE_BROWSER_ARGS` with `shlex.split`
  (whitespace-separated, shell-style quoting; was comma-split), so flags containing
  commas or spaces are expressible by quoting — unbalanced quotes raise `ValueError` at
  startup. Its `AnalyzeRequest.url` is now bounded to 2083 characters.

### Fixed

- Single-flight render coalescing no longer propagates a cancelled leader's
  `CancelledError` to concurrent callers of the same URL (triggered by e.g. the leader's
  `analyze(max_total_seconds=...)` deadline expiring, or its HTTP client disconnecting in
  a server). Followers now re-elect a new leader and re-render instead; a caller's *own*
  cancellation still raises normally.
- The robots loader's failure handling now also catches `httpx.InvalidURL` (which
  subclasses neither `HTTPError` nor `ValueError`), so a redirect `Location` that a
  stricter future httpx refuses to parse fails open as "no rules" instead of propagating
  out of the loader. Not reachable with the current httpx (it parses leniently); pinned
  by a regression test via the transport seam.

## [0.2.0] - 2026-06-09

First release to include the SSRF hardening work; also the first to ship the restructured
documentation set.

### Added

- **SSRF controls.** A URL scheme gate — only `http(s)` is fetched by default; `file://`
  requires an explicit `PolitenessPolicy(allow_file_urls=True)` opt-in, and every other
  scheme raises the new `UnsupportedSchemeError` (now a public export). A new
  `request_filter` predicate gates **every** request the browser makes (the navigation and
  the page's own sub-resources), the in-library defense against sub-resource SSRF. (#14)
- **`SECURITY.md`** documenting the threat model — SSRF, resource exhaustion / DoS, and the
  fail-open `robots.txt` gate — and the controls consumers must enforce. (#13)
- Capped `robots.txt` `Crawl-delay` honoring via a new `max_crawl_delay` policy knob (30 s
  default) so a hostile or typo'd directive cannot stall the pipeline. (#14)
- Screenshot capture safeguards: dimension caps and a decode pixel cap that rejects
  decompression-bomb captures. (#14)
- Dependabot for GitHub Actions and Python dependencies. (#12)

### Changed

- Themes now render concurrently through a single shared headless Chromium launch instead
  of one browser per theme. (#14)
- The configured User-Agent is now sent on the page render itself, not just the
  `robots.txt` GET, so the render is attributable to the same identity. (#14)
- Documentation restructured into a slim README plus `docs/usage.md`, `docs/advanced.md`,
  `CONTRIBUTING.md`, and this changelog. (#15)

### Removed

- Dead code paths in the harvest / screenshot layers. (#14)

## [0.1.0] - 2026-06-09

Initial public release.

- `analyze(url)` — async pipeline that renders a page in headless Chromium, harvests
  design tokens and computed element colors, and classifies them into a 60/30/10 palette
  with ranked, scored candidates per role.
- Typed, frozen Pydantic result (`AnalysisResult`) with per-theme palettes, OKLCH-bearing
  colors, declared-vs-rendered token divergence, status-color filtering, fit scoring, and
  run metadata.
- Optional dark-mode analysis (`themes=LIGHT_AND_DARK`) with single-theme collapse for
  sites that ignore `prefers-color-scheme`.
- `PolitenessPolicy` — configurable User-Agent, `robots.txt` gate, per-host rate limiting,
  and an LRU render cache.
- Bundled, overridable palette configuration (`config_path=` / `load_config`).
- Fully typed (`py.typed`), Python 3.12+.

[Unreleased]: https://github.com/cassidyhhaas/colorsense/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/cassidyhhaas/colorsense/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cassidyhhaas/colorsense/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cassidyhhaas/colorsense/releases/tag/v0.1.0
